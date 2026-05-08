import { useCallback, useEffect, useMemo, useState } from "react";
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
import { useAudioController } from "./hooks/useAudioController";
import { AdminView } from "./components/AdminView";
import { AuthView, RoomView, TopBar } from "./components/AuthRoom";
import { PlayerBar, QueueTabs, type QueueTab } from "./components/QueuePlayer";
import { MembersPanel, UserMenu } from "./components/Popovers";
import { PlaylistsView, SearchOverlay } from "./components/SearchPlaylists";
import { ScrollArea, SegmentedTabs } from "./components/common";
import { TrackList, type PlaylistMembership } from "./components/TrackList";
import type {
  PlaybackEnvelope,
  Playlist,
  PublicConfig,
  QueueItem,
  Room,
  TrendingItem,
  UserPublic,
  WsMessage,
} from "./types";

type Tab = "trending" | "playlists" | "queue";

export function App() {
  useStableViewportHeight();
  const queryClient = useQueryClient();
  const [token, setToken] = useState(() => localStorage.getItem(TOKEN_STORAGE_KEY));
  const [adminToken, setAdminToken] = useState<string | null>(null);
  const [sessionUserId, setSessionUserId] = useState<number | null>(() => numericStorageValue(SESSION_USER_ID_KEY));
  const [roomId, setRoomIdState] = useState<number | null>(() => readInitialRoomId());
  const [tab, setTab] = useState<Tab>("trending");
  const [queueTab, setQueueTab] = useState<QueueTab>("queue");
  const [searchOpen, setSearchOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [hint, setHint] = useState("");
  const queueTabInPrimary = useMediaQuery("(max-width: 1100px)");

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

  const audio = useAudioController(roomId, token, queueQuery.data || [], config.audio_loudness.ffmpeg_available);

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
    enabled: !!token && !!roomId && membersOpen,
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
    if (token) resetMobileViewport();
  }, [roomId, token]);

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

  const refreshRoomData = useCallback(() => {
    queryClient.invalidateQueries({ queryKey: ["queue", roomId, token] });
    queryClient.invalidateQueries({ queryKey: ["history", roomId, token] });
    queryClient.invalidateQueries({ queryKey: ["trending", token] });
    queryClient.invalidateQueries({ queryKey: ["playlists", token] });
    queryClient.invalidateQueries({ queryKey: ["playlist-map", token] });
  }, [queryClient, roomId, token]);

  const applyPlaybackEnvelope = audio.applyPlaybackEnvelope;

  const handleRoomGone = useCallback(() => {
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
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${protocol}://${location.host}${withBase("/ws")}`);
      ws.addEventListener("open", () => {
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
    setAdminToken(null);
    localStorage.removeItem(TOKEN_STORAGE_KEY);
    setRoomId(null);
    setHint("");
    queryClient.clear();
  }, [queryClient, setRoomId]);

  useEffect(() => {
    if (meQuery.error instanceof ApiError && meQuery.error.status === 401) logout();
  }, [logout, meQuery.error]);

  if (adminToken) {
    return <AdminView token={adminToken} onExit={() => setAdminToken(null)} />;
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
        onAdminLogin={(out) => setAdminToken(out.token)}
      />
    );
  }

  if (!roomId) {
    return (
      <RoomView
        token={token}
        me={meQuery.data}
        rooms={roomsQuery.data || []}
        loading={roomsQuery.isLoading}
        onLogout={logout}
        onEnter={(id) => {
          resetMobileViewport();
          if (audio.playEnabled) audio.unlockAudio();
          setRoomId(id);
        }}
      />
    );
  }

  return (
    <div className="appFrame">
      <TopBar
        username={meQuery.data?.username || "账户"}
        playEnabled={audio.playEnabled}
        onTogglePlayEnabled={audio.setPlayEnabled}
        userMenuOpen={userMenuOpen}
        membersOpen={membersOpen}
        onToggleUserMenu={() => setUserMenuOpen((v) => !v)}
        onToggleMembers={() => setMembersOpen((v) => !v)}
        onSearch={() => setSearchOpen(true)}
        onLeave={async () => {
          await api(`/api/rooms/${roomId}/leave`, { method: "POST", token }).catch(() => {});
          handleRoomGone();
        }}
      />

      {userMenuOpen && (
        <UserMenu
          token={token}
          user={meQuery.data}
          onClose={() => setUserMenuOpen(false)}
          onLogout={logout}
          onUpdated={() => queryClient.invalidateQueries({ queryKey: ["me", token] })}
        />
      )}

      {membersOpen && (
        <MembersPanel
          members={membersQuery.data || []}
          loading={membersQuery.isLoading}
          onClose={() => setMembersOpen(false)}
        />
      )}

      <main className="workspace">
        <section className="primaryColumn">
          <section className="glassPanel libraryPanel">
            <SegmentedTabs
              className="libraryTabs"
              value={tab}
              onChange={(value) => setTab(value as Tab)}
              items={libraryTabs}
            />
            <ScrollArea className="libraryScroll">
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

      <PlayerBar audio={audio} />

      {searchOpen && (
        <SearchOverlay
          token={token}
          roomId={roomId}
          queuedKeys={queuedKeys}
          playlistMap={playlistMap}
          playlists={playlists}
          historyLimit={config.client.search_history_limit}
          onClose={() => setSearchOpen(false)}
          onChanged={refreshRoomData}
        />
      )}
    </div>
  );
}
