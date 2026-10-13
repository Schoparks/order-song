import { useCallback, useEffect, useMemo, useRef, useState, type CSSProperties } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Heart, History, ListMusic, WandSparkles } from "lucide-react";
import { api, ApiError, defaultConfig, mergeConfig, withBase } from "./lib/api";
import { trackKey } from "./lib/track";
import {
  numericStorageValue,
  readInitialRoomId,
  roomStorageKey,
  SESSION_USER_ID_KEY,
  TOKEN_STORAGE_KEY,
  writeRoomIdForUser,
} from "./lib/roomStorage";
import {
  isPlaybackMessage,
  WS_BUFFERED_CLOSE_THRESHOLD,
  WS_HEARTBEAT_INTERVAL_MS,
  WS_HEARTBEAT_TIMEOUT_MS,
} from "./lib/ws";
import { useStableViewportHeight, resetMobileViewport } from "./hooks/useStableViewportHeight";
import { useMediaQuery } from "./hooks/useMediaQuery";
import { useRoomConnectionState, useRoomMenuDismissal } from "./hooks/useRoomChrome";
import { useAudioController } from "./hooks/useAudioController";
import { AdminView } from "./components/AdminView";
import { AuthView, RoomView, TopBar } from "./components/AuthRoom";
import { QueueTabs, type QueueTab } from "./components/QueuePlayer";
import { MiniPlayer, NowPlayingDock, PlayerSheet } from "./components/NowPlaying";
import { MembersPanel, UserMenu } from "./components/Popovers";
import { PlaylistsView, SearchOverlay } from "./components/SearchPlaylists";
import { ConfirmDialog, ScrollArea, SegmentedTabs } from "./components/common";
import { TrackRepairContext } from "./components/TrackAvailability";
import { TrackReplacementDialog } from "./components/TrackReplacementDialog";
import { TrackList, type PlaylistMembership } from "./components/TrackList";
import type {
  PlaybackEnvelope,
  Playlist,
  PublicConfig,
  QueueItem,
  Room,
  TrendingItem,
  Track,
  UserPublic,
  WsMessage,
} from "./types";

type Tab = "trending" | "playlists" | "queue";
type Overlay = "search" | "members" | "account" | "roomMenu" | "player" | null;

export function App() {
  useStableViewportHeight();
  const queryClient = useQueryClient();
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY));
  const [adminOpen, setAdminOpen] = useState(false);
  const [sessionUserId, setSessionUserId] = useState<number | null>(() => numericStorageValue(SESSION_USER_ID_KEY));
  const [roomId, setRoomIdState] = useState<number | null>(() => readInitialRoomId());
  const [tab, setTab] = useState<Tab>("trending");
  const [queueTab, setQueueTab] = useState<QueueTab>("queue");
  const [activeOverlay, setActiveOverlay] = useState<Overlay>(null);
  const [replacementTarget, setReplacementTarget] = useState<Track | null>(null);
  const [trackNotice, setTrackNotice] = useState("");
  const [leaveConfirmOpen, setLeaveConfirmOpen] = useState(false);
  const [connectionState, setConnectionState] = useRoomConnectionState(roomId);
  const [hint, setHint] = useState("");
  const roomListAccountButtonRef = useRef<HTMLButtonElement>(null);
  const roomMenuButtonRef = useRef<HTMLButtonElement>(null);
  const queueTabInPrimary = useMediaQuery("(max-width: 1100px)");
  const isMobile = useMediaQuery("(max-width: 620px)");

  const configQuery = useQuery({
    queryKey: ["public-config"],
    queryFn: () => api<PublicConfig>("/api/config"),
  });
  const config = useMemo(() => mergeConfig(configQuery.data || defaultConfig), [configQuery.data]);

  const meQuery = useQuery({
    queryKey: ["me", token],
    queryFn: () => api<UserPublic>("/api/me", { token }),
    enabled: !!token,
  });

  const activeUserId = meQuery.data?.id ?? sessionUserId;

  const setRoomId = useCallback((nextRoomId: number | null) => {
    setRoomIdState(nextRoomId);
    writeRoomIdForUser(nextRoomId, activeUserId);
  }, [activeUserId]);

  const roomsQuery = useQuery({
    queryKey: ["rooms", token],
    queryFn: () => api<Room[]>("/api/rooms", { token }),
    enabled: !!token && !roomId,
    refetchInterval: token && !roomId ? config.client.rooms_refresh_interval_ms : false,
  });

  const queueQuery = useQuery({
    queryKey: ["queue", roomId, token],
    queryFn: () => api<QueueItem[]>(`/api/rooms/${roomId}/queue`, { token }),
    enabled: !!token && !!roomId,
  });

  const roomQuery = useQuery({
    queryKey: ["room", roomId, token],
    queryFn: () => api<Room>(`/api/rooms/${roomId}`, { token }),
    enabled: !!token && !!roomId,
  });

  const refreshRoomData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["queue", roomId, token] });
    queryClient.invalidateQueries({ queryKey: ["history", roomId, token] });
    queryClient.invalidateQueries({ queryKey: ["trending", token] });
    queryClient.invalidateQueries({ queryKey: ["playlists", token] });
    queryClient.invalidateQueries({ queryKey: ["playlist-map", token] });
    queryClient.invalidateQueries({ queryKey: ["playlist-items"] });
  }, [queryClient, roomId, token]);

  const audio = useAudioController(roomId, token, queueQuery.data || [], config.audio_loudness.ffmpeg_available, refreshRoomData);

  const historyQuery = useQuery({
    queryKey: ["history", roomId, token],
    queryFn: () => api<QueueItem[]>(`/api/rooms/${roomId}/history`, { token }),
    enabled: !!token && !!roomId,
  });

  const trendingQuery = useQuery({
    queryKey: ["trending", token, config.trending.limit],
    queryFn: () => api<TrendingItem[]>(`/api/trending?limit=${encodeURIComponent(config.trending.limit)}`, { token }),
    enabled: !!token && !!roomId,
    refetchInterval: token && roomId ? config.client.trending_sync_interval_ms : false,
  });

  const playlistsQuery = useQuery({
    queryKey: ["playlists", token],
    queryFn: () => api<Playlist[]>("/api/playlists", { token }),
    enabled: !!token && !!roomId,
    refetchInterval: token && roomId ? config.client.trending_sync_interval_ms : false,
  });

  const playlistMapQuery = useQuery({
    queryKey: ["playlist-map", token],
    queryFn: () => api<Record<string, PlaylistMembership[]>>("/api/playlists/track-map", { token }),
    enabled: !!token && !!roomId,
  });

  const membersQuery = useQuery({
    queryKey: ["members", roomId, token],
    queryFn: () => api<Array<{ id: number; username: string }>>(`/api/rooms/${roomId}/members`, { token }),
    enabled: !!token && !!roomId,
  });

  const queuedKeys = useMemo(() => new Set((queueQuery.data || []).map((item) => trackKey(item.track))), [queueQuery.data]);
  const playlistMap = playlistMapQuery.data || {};
  const playlists = playlistsQuery.data || [];
  const libraryTabs = useMemo(
    () => [
      { value: "trending", label: "热门", icon: <WandSparkles /> },
      { value: "playlists", label: "歌单", icon: <Heart /> },
      ...(queueTabInPrimary
        ? [{ value: "queue", label: `播放列表${queuedKeys.size ? ` ${queuedKeys.size}` : ""}`, icon: <ListMusic /> }]
        : []),
    ],
    [queueTabInPrimary, queuedKeys.size],
  );

  useEffect(() => {
    if (!queueTabInPrimary && tab === "queue") setTab("trending");
  }, [queueTabInPrimary, tab]);

  useEffect(() => {
    setReplacementTarget(null);
    setTrackNotice("");
    if (token) resetMobileViewport();
  }, [roomId, token]);

  const closeRoomMenu = useCallback(() => setActiveOverlay(null), []);
  useRoomMenuDismissal(activeOverlay === "roomMenu", closeRoomMenu);

  useEffect(() => {
    const onStorage = (event: StorageEvent) => {
      if (event.key !== TOKEN_STORAGE_KEY && event.key !== SESSION_USER_ID_KEY) return;
      setToken(localStorage.getItem(TOKEN_STORAGE_KEY));
      setSessionUserId(numericStorageValue(SESSION_USER_ID_KEY));
      setRoomIdState(readInitialRoomId());
      queryClient.clear();
    };
    window.addEventListener("storage", onStorage);
    return () => window.removeEventListener("storage", onStorage);
  }, [queryClient]);

  useEffect(() => {
    if (!meQuery.data) return;
    setSessionUserId(meQuery.data.id);
    localStorage.setItem(SESSION_USER_ID_KEY, String(meQuery.data.id));
    if (roomId) writeRoomIdForUser(roomId, meQuery.data.id);
  }, [meQuery.data, roomId]);



  const applyPlaybackEnvelope = audio.applyPlaybackEnvelope;

  const handleRoomGone = useCallback(() => {
    setActiveOverlay(null);
    setConnectionState(navigator.onLine ? "reconnecting" : "offline");
    setRoomId(null);
    applyPlaybackEnvelope({
      playback_state: {
        room_id: 0,
        mode: "order_only",
        current_queue_item_id: null,
        is_playing: false,
        position_ms: 0,
        volume: 50,
        updated_at: new Date().toISOString(),
      },
      current_track: null,
      ordered_by: null,
    });
  }, [applyPlaybackEnvelope, setRoomId]);

  useEffect(() => {
    if (!token || !roomId) return;
    let cancelled = false;
    async function syncState() {
      try {
        const state = await api<PlaybackEnvelope>(`/api/rooms/${roomId}/state`, { token });
        if (!cancelled) applyPlaybackEnvelope(state);
      } catch (error) {
        if (error instanceof ApiError && (error.status === 401 || error.status === 403 || error.status === 404)) handleRoomGone();
      }
    }
    syncState();
    const timer = window.setInterval(syncState, Math.max(1000, config.client.sync_interval_ms));
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [applyPlaybackEnvelope, config.client.sync_interval_ms, handleRoomGone, roomId, token]);

  useEffect(() => {
    if (!token || !roomId) return;
    let closed = false;
    let reconnectTimer = 0;
    let heartbeatTimer = 0;
    let attempt = 0;
    let lastWsMessageAt = Date.now();
    let ws: WebSocket | null = null;

    function stopHeartbeat() {
      window.clearInterval(heartbeatTimer);
      heartbeatTimer = 0;
    }

    function closeStaleSocket() {
      if (!ws || ws.readyState === WebSocket.CLOSED || ws.readyState === WebSocket.CLOSING) return;
      try {
        ws.close();
      } catch {
        // The close event will reconnect when possible.
      }
    }

    function startHeartbeat() {
      stopHeartbeat();
      heartbeatTimer = window.setInterval(() => {
        if (!ws || ws.readyState !== WebSocket.OPEN) return;
        if (Date.now() - lastWsMessageAt > WS_HEARTBEAT_TIMEOUT_MS || ws.bufferedAmount > WS_BUFFERED_CLOSE_THRESHOLD) {
          closeStaleSocket();
          return;
        }
        try {
          ws.send(JSON.stringify({ type: "ping" }));
        } catch {
          closeStaleSocket();
        }
      }, WS_HEARTBEAT_INTERVAL_MS);
    }

    function connect() {
      setConnectionState(navigator.onLine ? "reconnecting" : "offline");
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${protocol}://${location.host}${withBase("/ws")}`);
      ws.addEventListener("open", () => {
        setConnectionState("connected");
        attempt = 0;
        lastWsMessageAt = Date.now();
        ws?.send(JSON.stringify({ type: "join_room", room_id: roomId, token }));
        startHeartbeat();
      });
      ws.addEventListener("message", async (event) => {
        lastWsMessageAt = Date.now();
        try {
          const msg = JSON.parse(event.data) as WsMessage;
          if (msg.type === "joined" || msg.type === "pong") return;
          if (isPlaybackMessage(msg)) {
            applyPlaybackEnvelope(msg);
            refreshRoomData();
          }
          if (msg.type === "error") {
            const message = String(msg.message || "");
            if (message === "not a room member") handleRoomGone();
          }
          if (msg.type === "queue_updated") refreshRoomData();
          if (msg.type === "room_destroyed") handleRoomGone();
          if (msg.type === "room_member_left" && meQuery.data && msg.user_id === meQuery.data.id) handleRoomGone();
          if (msg.type === "room_member_joined" || msg.type === "room_member_left") {
            queryClient.invalidateQueries({ queryKey: ["members", roomId, token] });
            queryClient.invalidateQueries({ queryKey: ["rooms", token] });
          }
        } catch {
          // Ignore malformed messages.
        }
      });
      ws.addEventListener("error", closeStaleSocket);
      ws.addEventListener("close", () => {
        setConnectionState(navigator.onLine ? "reconnecting" : "offline");
        stopHeartbeat();
        if (closed || !roomId) return;
        const delay = Math.min(30000, 1000 * 2 ** attempt);
        attempt += 1;
        reconnectTimer = window.setTimeout(connect, delay);
      });
    }

    connect();
    return () => {
      closed = true;
      window.clearTimeout(reconnectTimer);
      stopHeartbeat();
      ws?.close();
    };
  }, [applyPlaybackEnvelope, handleRoomGone, meQuery.data, queryClient, refreshRoomData, roomId, token]);

  const logout = useCallback(() => {
    setToken(null);
    setAdminOpen(false);
    setActiveOverlay(null);
    setLeaveConfirmOpen(false);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    // Keep roomId:{user_id} so the same account can resume its room after logging in again.
    setRoomIdState(null);
    setHint("");
    queryClient.clear();
  }, [queryClient]);

  useEffect(() => {
    if (meQuery.error instanceof ApiError && meQuery.error.status === 401) logout();
  }, [logout, meQuery.error]);

  if (adminOpen && token) {
    return <AdminView token={token} onExit={() => setAdminOpen(false)} />;
  }

  if (!token) {
    return (
      <AuthView
        hint={hint}
        onHint={setHint}
        onLogin={(out) => {
          const previousUserId = numericStorageValue(SESSION_USER_ID_KEY);
          const nextRoomId = previousUserId === out.user.id ? numericStorageValue(roomStorageKey(out.user.id)) : null;
          setToken(out.token);
          setSessionUserId(out.user.id);
          setRoomIdState(nextRoomId);
          localStorage.setItem(TOKEN_STORAGE_KEY, out.token);
          localStorage.setItem(SESSION_USER_ID_KEY, String(out.user.id));
          writeRoomIdForUser(nextRoomId, out.user.id);
          queryClient.removeQueries({ queryKey: ["me"] });
        }}
      />
    );
  }

  if (!roomId) {
    return (
      <>
        <RoomView
          token={token}
          me={meQuery.data}
          rooms={roomsQuery.data || []}
          loading={roomsQuery.isLoading}
          accountOpen={activeOverlay === "account"}
          accountButtonRef={roomListAccountButtonRef}
          onOpenAccount={() => setActiveOverlay("account")}
          onOpenAdmin={() => setAdminOpen(true)}
          onLogout={logout}
          onEnter={(id) => {
            resetMobileViewport();
            setActiveOverlay(null);
            if (audio.playEnabled) audio.unlockAudio();
            setRoomId(id);
          }}
        />
        {activeOverlay === "account" && (
          <UserMenu
            token={token}
            user={meQuery.data}
            anchorRef={roomListAccountButtonRef}
            onClose={() => setActiveOverlay(null)}
            onLogout={logout}
            onUpdated={() => queryClient.invalidateQueries({ queryKey: ["me", token] })}
          />
        )}
      </>
    );
  }

  const mobileTabIndex = Math.max(0, libraryTabs.findIndex((item) => item.value === tab));
  const mobileSegmentStyle = {
    "--segment-count": libraryTabs.length,
    "--segment-index": mobileTabIndex,
    "--segment-width": `${100 / libraryTabs.length}%`,
    "--segment-shift": `${mobileTabIndex * 100}%`,
  } as CSSProperties;

  return (
    <TrackRepairContext.Provider value={{ onRepair: (track) => { setActiveOverlay(null); setTrackNotice(""); setReplacementTarget(track); }, canRepair: (track) => (playlistMap[trackKey(track)] || []).length > 0 }}>
    <div className="appFrame">
      <TopBar
        roomName={roomQuery.data?.name || `房间 #${roomId}`}
        username={meQuery.data?.username || "账户"}
        members={membersQuery.data || []}
        connectionState={connectionState}
        roomMenuOpen={activeOverlay === "roomMenu"}
        membersOpen={activeOverlay === "members"}
        searchOpen={activeOverlay === "search"}
        roomMenuButtonRef={roomMenuButtonRef}
        onToggleUserMenu={() => {
          setActiveOverlay("account");
        }}
        onToggleMembers={() => setActiveOverlay((value) => value === "members" ? null : "members")}
        onSearch={() => setActiveOverlay("search")}
        onToggleRoomMenu={() => setActiveOverlay((value) => value === "roomMenu" ? null : "roomMenu")}
        onLeaveRequest={() => { setActiveOverlay(null); setLeaveConfirmOpen(true); }}
      />

      {activeOverlay === "account" && (
        <UserMenu
          token={token}
          user={meQuery.data}
          anchorRef={roomMenuButtonRef}
          onClose={() => setActiveOverlay(null)}
          onLogout={logout}
          onUpdated={() => queryClient.invalidateQueries({ queryKey: ["me", token] })}
        />
      )}

      {activeOverlay === "members" && (
        <MembersPanel
          members={membersQuery.data || []}
          loading={membersQuery.isLoading}
          onClose={() => setActiveOverlay(null)}
        />
      )}

      <main className="workspace">
        <section className="primaryColumn">
          <section className="glassPanel libraryPanel">
            <SegmentedTabs
              className="libraryTabs"
              label="内容分类"
              value={tab}
              onChange={(value) => setTab(value as Tab)}
              items={libraryTabs}
            />
            <ScrollArea className="libraryScroll">
              {trackNotice && <p className="hintText" role="status">{trackNotice}</p>}
              {tab === "trending" && (
                <TrackList
                  items={(trendingQuery.data || []).map((item) => ({ track: item.track, meta: `${item.track.source} · ${item.track.artist || "-"} · ${item.order_count} 次` }))}
                  token={token}
                  roomId={roomId}
                  playlists={playlists}
                  queuedKeys={queuedKeys}
                  playlistMap={playlistMap}
                  onChanged={refreshRoomData}
                />
              )}
              {tab === "playlists" && (
                <PlaylistsView
                  token={token}
                  roomId={roomId}
                  playlists={playlists}
                  playlistMap={playlistMap}
                  queuedKeys={queuedKeys}
                  qqMusicEnabled={config.qq_music.enabled}
                  onChanged={refreshRoomData}
                />
              )}
              {tab === "queue" && (
                <QueueTabs
                  value={queueTab}
                  onChange={setQueueTab}
                  queue={queueQuery.data || []}
                  history={historyQuery.data || []}
                  queuedKeys={queuedKeys}
                  token={token}
                  roomId={roomId}
                  onChanged={refreshRoomData}
                />
              )}
            </ScrollArea>
          </section>
        </section>

        <aside className="sideColumn glassPanel">
          <SegmentedTabs
            label="队列视图"
            value={queueTab}
            onChange={(value) => setQueueTab(value as QueueTab)}
            items={[
              { value: "queue", label: `队列${queuedKeys.size ? ` ${queuedKeys.size}` : ""}`, icon: <ListMusic /> },
              { value: "history", label: "历史", icon: <History /> },
            ]}
          />
          <ScrollArea className="sideScroll">
            <QueueTabs
              value={queueTab}
              onChange={setQueueTab}
              queue={queueQuery.data || []}
              history={historyQuery.data || []}
              queuedKeys={queuedKeys}
              token={token}
              roomId={roomId}
              onChanged={refreshRoomData}
              compact
            />
          </ScrollArea>
        </aside>
      </main>

      {isMobile ? <MiniPlayer audio={audio} onOpen={() => setActiveOverlay("player")} /> : <NowPlayingDock audio={audio} />}

      {isMobile && <nav className="mobileNav glassPanel" aria-label="主要内容" style={mobileSegmentStyle}>
        {libraryTabs.map((item) => <button key={item.value} className={tab === item.value ? "active" : ""} onClick={() => setTab(item.value as Tab)}>{item.icon}<span>{item.value === "queue" ? "队列" : item.label}</span></button>)}
      </nav>}

      {activeOverlay === "player" && <PlayerSheet audio={audio} onClose={() => setActiveOverlay(null)} />}

      {activeOverlay === "search" && (
        <SearchOverlay
          token={token}
          roomId={roomId}
          queuedKeys={queuedKeys}
          playlistMap={playlistMap}
          playlists={playlists}
          historyLimit={config.client.search_history_limit}
          qqMusicEnabled={config.qq_music.enabled}
          onClose={() => setActiveOverlay(null)}
          onChanged={refreshRoomData}
        />
      )}
      {replacementTarget && <TrackReplacementDialog key={trackKey(replacementTarget)} track={replacementTarget} token={token} qqMusicEnabled={config.qq_music.enabled} onClose={() => setReplacementTarget(null)} onReplaced={(message) => { setReplacementTarget(null); setTrackNotice(message); refreshRoomData(); }} />}
      {leaveConfirmOpen && <ConfirmDialog title="退出房间？" message="退出后需要重新加入才能继续点歌。" confirmText="退出房间" onCancel={() => setLeaveConfirmOpen(false)} onConfirm={async () => {
        await api(`/api/rooms/${roomId}/leave`, { method: "POST", token });
        setLeaveConfirmOpen(false);
        handleRoomGone();
      }} />}
    </div>
    </TrackRepairContext.Provider>
  );
}
