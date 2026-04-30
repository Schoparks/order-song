import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import {
  ArrowUp,
  ArrowUpToLine,
  BadgePlus,
  ChevronLeft,
  Check,
  CircleUserRound,
  CloudDownload,
  Gauge,
  Heart,
  History,
  ListMusic,
  LogOut,
  Music2,
  Pause,
  Pencil,
  Play,
  Plus,
  Search,
  Shuffle,
  SkipForward,
  Trash2,
  UserCog,
  UsersRound,
  Volume2,
  VolumeX,
  WandSparkles,
  X,
} from "lucide-react";
import { api, ApiError, defaultConfig, mergeConfig, withBase } from "./lib/api";
import { trackKey, trackPayload } from "./lib/track";
import { useAudioController } from "./hooks/useAudioController";
import type {
  LoginOut,
  PlaybackEnvelope,
  Playlist,
  PlaylistItem,
  PublicConfig,
  QueueItem,
  Room,
  Track,
  TrendingItem,
  UserPublic,
  WsMessage,
} from "./types";

type Tab = "trending" | "playlists" | "queue";
type QueueTab = "queue" | "history";

interface PlaylistMembership {
  item_id: number;
  playlist_id: number;
  playlist_name: string;
}

type AdminUser = UserPublic & { last_active_room_id?: number | null };
type AdminRoom = Room & { created_by: string; members: Array<{ id: number; username: string }> };

function blurActiveElement() {
  const active = document.activeElement;
  if (active instanceof HTMLElement) active.blur();
}

function useDialogViewportLock() {
  useLayoutEffect(() => {
    const { body, documentElement } = document;
    const scrollX = window.scrollX;
    const scrollY = window.scrollY;
    const previous = {
      bodyPosition: body.style.position,
      bodyTop: body.style.top,
      bodyLeft: body.style.left,
      bodyRight: body.style.right,
      bodyWidth: body.style.width,
      bodyOverflow: body.style.overflow,
      htmlOverflow: documentElement.style.overflow,
    };

    body.style.position = "fixed";
    body.style.top = `-${scrollY}px`;
    body.style.left = `-${scrollX}px`;
    body.style.right = "0";
    body.style.width = "100%";
    body.style.overflow = "hidden";
    documentElement.style.overflow = "hidden";

    return () => {
      body.style.position = previous.bodyPosition;
      body.style.top = previous.bodyTop;
      body.style.left = previous.bodyLeft;
      body.style.right = previous.bodyRight;
      body.style.width = previous.bodyWidth;
      body.style.overflow = previous.bodyOverflow;
      documentElement.style.overflow = previous.htmlOverflow;

      const restoreScroll = () => window.scrollTo(scrollX, scrollY);
      window.requestAnimationFrame(restoreScroll);
      window.setTimeout(restoreScroll, 80);
    };
  }, []);
}

function isPlaybackMessage(msg: WsMessage): msg is Extract<WsMessage, { type: "playback_updated" }> {
  return msg.type === "playback_updated" && !!(msg as { playback_state?: unknown }).playback_state;
}

function useMediaQuery(query: string) {
  const getMatches = useCallback(() => window.matchMedia(query).matches, [query]);
  const [matches, setMatches] = useState(getMatches);

  useEffect(() => {
    const media = window.matchMedia(query);
    const update = () => setMatches(media.matches);
    update();
    media.addEventListener("change", update);
    return () => media.removeEventListener("change", update);
  }, [query]);

  return matches;
}

function ScrollArea({ children, className = "" }: { children: ReactNode; className?: string }) {
  const ref = useRef<HTMLDivElement | null>(null);
  const [showTop, setShowTop] = useState(false);

  useEffect(() => {
    const node = ref.current;
    if (!node) return;
    const onScroll = () => setShowTop(node.scrollTop > 280);
    onScroll();
    node.addEventListener("scroll", onScroll, { passive: true });
    return () => node.removeEventListener("scroll", onScroll);
  }, []);

  return (
    <div className={`scrollShell ${className}`}>
      <div ref={ref} className="scrollArea">
        {children}
      </div>
      {showTop && (
        <button className="scrollTopButton" onClick={() => ref.current?.scrollTo({ top: 0, behavior: "smooth" })}>
          <ArrowUp />
        </button>
      )}
    </div>
  );
}

function CardDialog({
  title,
  children,
  onClose,
}: {
  title: string;
  children: ReactNode;
  onClose: () => void;
}) {
  useDialogViewportLock();
  const closeDialog = useCallback(() => {
    blurActiveElement();
    onClose();
  }, [onClose]);

  return createPortal(
    <div className="dialogOverlay" onMouseDown={closeDialog}>
      <section className="dialogCard glassPanel" onMouseDown={(event) => event.stopPropagation()}>
        <div className="sheetHeader">
          <h3>{title}</h3>
          <button className="iconButton" onClick={closeDialog}><X /></button>
        </div>
        {children}
      </section>
    </div>,
    document.body,
  );
}

function ConfirmDialog({
  title,
  message,
  confirmText = "确认",
  busy,
  onCancel,
  onConfirm,
}: {
  title: string;
  message: string;
  confirmText?: string;
  busy?: boolean;
  onCancel: () => void;
  onConfirm: () => void | Promise<void>;
}) {
  return (
    <CardDialog title={title} onClose={onCancel}>
      <p className="dialogMessage">{message}</p>
      <div className="dialogActions">
        <button className="glassButton" onClick={onCancel} disabled={busy}>取消</button>
        <button className="dangerButton" onClick={() => { void onConfirm(); }} disabled={busy}>{busy ? "处理中" : confirmText}</button>
      </div>
    </CardDialog>
  );
}

function trackCoverSrc(track: Track): string | null {
  if (!track.cover_url) return null;
  if (track.source === "bilibili") {
    if (track.cover_url.startsWith("//")) return `https:${track.cover_url}`;
    return track.cover_url.replace(/^http:\/\//i, "https://");
  }
  return track.cover_url;
}

function TrackCover({ track }: { track: Track }) {
  const [failed, setFailed] = useState(false);
  const src = trackCoverSrc(track);
  if (!src || failed) return <div className="cover placeholder"><Music2 /></div>;
  return (
    <img
      className="cover"
      src={src}
      alt=""
      loading="lazy"
      referrerPolicy={track.source === "bilibili" ? "no-referrer" : undefined}
      onError={() => setFailed(true)}
    />
  );
}

export function App() {
  const queryClient = useQueryClient();
  const [token, setToken] = useState(() => localStorage.getItem("token"));
  const [adminToken, setAdminToken] = useState<string | null>(null);
  const [roomId, setRoomIdState] = useState<number | null>(() => Number(localStorage.getItem("roomId") || "") || null);
  const [tab, setTab] = useState<Tab>("trending");
  const [queueTab, setQueueTab] = useState<QueueTab>("queue");
  const [searchOpen, setSearchOpen] = useState(false);
  const [membersOpen, setMembersOpen] = useState(false);
  const [userMenuOpen, setUserMenuOpen] = useState(false);
  const [hint, setHint] = useState("");
  const queueTabInPrimary = useMediaQuery("(max-width: 1100px)");

  const audio = useAudioController(roomId, token);

  const setRoomId = useCallback((nextRoomId: number | null) => {
    setRoomIdState(nextRoomId);
    if (nextRoomId) localStorage.setItem("roomId", String(nextRoomId));
    else localStorage.removeItem("roomId");
  }, []);

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
        if (error instanceof ApiError && (error.status === 401 || error.status === 404)) handleRoomGone();
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
    let attempt = 0;
    let ws: WebSocket | null = null;

    function connect() {
      const protocol = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${protocol}://${location.host}${withBase("/ws")}`);
      ws.addEventListener("open", () => {
        attempt = 0;
        ws?.send(JSON.stringify({ type: "join_room", room_id: roomId, token }));
      });
      ws.addEventListener("message", async (event) => {
        try {
          const msg = JSON.parse(event.data) as WsMessage;
          if (isPlaybackMessage(msg)) {
            applyPlaybackEnvelope(msg);
            refreshRoomData();
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
      ws.addEventListener("close", () => {
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
      ws?.close();
    };
  }, [applyPlaybackEnvelope, handleRoomGone, meQuery.data, queryClient, refreshRoomData, roomId, token]);

  const logout = useCallback(() => {
    setToken(null);
    setAdminToken(null);
    localStorage.removeItem("token");
    setRoomId(null);
    setHint("");
    queryClient.clear();
  }, [queryClient, setRoomId]);

  if (adminToken) {
    return <AdminView token={adminToken} onExit={() => setAdminToken(null)} />;
  }

  if (!token) {
    return (
      <AuthView
        hint={hint}
        onHint={setHint}
        onLogin={(out) => {
          setToken(out.token);
          localStorage.setItem("token", out.token);
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
        onEnter={(id) => setRoomId(id)}
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

function AuthView({
  hint,
  onHint,
  onLogin,
  onAdminLogin,
}: {
  hint: string;
  onHint: (message: string) => void;
  onLogin: (out: LoginOut) => void;
  onAdminLogin: (out: LoginOut) => void;
}) {
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [busy, setBusy] = useState<"login" | "register" | "admin" | null>(null);

  async function submit(kind: "login" | "register" | "admin") {
    if (!username.trim() || !password) return;
    setBusy(kind);
    onHint("");
    try {
      if (kind === "register") {
        await api("/api/auth/register", { method: "POST", json: { username: username.trim(), password } });
        onHint("注册成功，请登录");
        return;
      }
      const out = await api<LoginOut>(kind === "admin" ? "/api/auth/admin-login" : "/api/auth/login", {
        method: "POST",
        json: { username: username.trim(), password },
      });
      if (kind === "admin") onAdminLogin(out);
      else onLogin(out);
    } catch (error) {
      onHint(error instanceof Error ? error.message : "操作失败");
    } finally {
      setBusy(null);
    }
  }

  return (
    <main className="authScene">
      <section className="authCard liquidHero">
        <div className="brandMark"><Music2 /></div>
        <h1>order-song</h1>
        <p>多人在线点歌、同步播放和歌单管理。</p>
        <div className="authFields">
          <input className="input" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="用户名" autoComplete="username" />
          <input className="input" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="密码" type="password" autoComplete="current-password" onKeyDown={(e) => e.key === "Enter" && submit("login")} />
        </div>
        <div className="buttonRow">
          <button className="primaryButton" disabled={!!busy} onClick={() => submit("login")}>{busy === "login" ? "登录中" : "登录"}</button>
          <button className="glassButton" disabled={!!busy} onClick={() => submit("register")}>{busy === "register" ? "注册中" : "注册"}</button>
          <button className="ghostButton" disabled={!!busy} onClick={() => submit("admin")}>管理端</button>
        </div>
        {hint && <p className="hintText">{hint}</p>}
      </section>
    </main>
  );
}

function RoomView({
  token,
  me,
  rooms,
  loading,
  onLogout,
  onEnter,
}: {
  token: string;
  me?: UserPublic;
  rooms: Room[];
  loading: boolean;
  onLogout: () => void;
  onEnter: (roomId: number) => void;
}) {
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const createRoom = useMutation({
    mutationFn: () => api<Room>("/api/rooms", { method: "POST", token, json: { name: name.trim() || null } }),
    onSuccess: (room) => onEnter(room.id),
  });

  return (
    <main className="roomScene">
      <header className="roomHeader glassPanel">
        <div>
          <span className="eyebrow">欢迎回来</span>
          <h1>{me?.username || "order-song"}</h1>
        </div>
        <button className="iconTextButton" onClick={onLogout}><LogOut />退出</button>
      </header>
      <section className="roomComposer glassPanel">
        <input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="房间名称（可为空）" />
        <button className="primaryButton" onClick={() => createRoom.mutate()} disabled={createRoom.isPending}>
          <Plus />{createRoom.isPending ? "创建中" : "创建并进入"}
        </button>
      </section>
      <section className="roomGrid">
        {loading && <EmptyState title="正在加载房间" />}
        {!loading && !rooms.length && <EmptyState title="暂无房间" meta="创建一个房间开始点歌。" />}
        {rooms.map((room) => (
          <button
            key={room.id}
            className="roomTile glassPanel"
            onClick={async () => {
              await api(`/api/rooms/${room.id}/join`, { method: "POST", token });
              queryClient.invalidateQueries({ queryKey: ["rooms", token] });
              onEnter(room.id);
            }}
          >
            <span className="roomIndex">#{room.id}</span>
            <strong>{room.name}</strong>
            <span>{room.member_count || 0} 人 · {(room.member_names || []).join("、") || "等待加入"}</span>
          </button>
        ))}
      </section>
    </main>
  );
}

function TopBar({
  username,
  playEnabled,
  userMenuOpen,
  membersOpen,
  onTogglePlayEnabled,
  onToggleUserMenu,
  onToggleMembers,
  onSearch,
  onLeave,
}: {
  username: string;
  playEnabled: boolean;
  userMenuOpen: boolean;
  membersOpen: boolean;
  onTogglePlayEnabled: (value: boolean) => void;
  onToggleUserMenu: () => void;
  onToggleMembers: () => void;
  onSearch: () => void;
  onLeave: () => void;
}) {
  return (
    <header className="topBar glassPanel">
      <div className="topCluster">
        <button className={`pillButton ${userMenuOpen ? "active" : ""}`} onClick={onToggleUserMenu}><CircleUserRound />{username}</button>
        <label className="liquidSwitch">
          <input type="checkbox" checked={playEnabled} onChange={(e) => onTogglePlayEnabled(e.target.checked)} />
          <span />
          <b>{playEnabled ? "可播放" : "仅点歌"}</b>
        </label>
        <button className="pillButton mobileSearchButton" onClick={onSearch}><Search /><span>搜索</span></button>
      </div>
      <div className="topCluster">
        <button className={`pillButton ${membersOpen ? "active" : ""}`} onClick={onToggleMembers}><UsersRound /><span>成员</span></button>
        <button className="dangerButton leaveRoomButton" onClick={onLeave}><LogOut /><span>退出</span></button>
      </div>
    </header>
  );
}

function SegmentedTabs({
  value,
  items,
  onChange,
  className = "",
}: {
  value: string;
  items: Array<{ value: string; label: string; icon?: ReactNode }>;
  onChange: (value: string) => void;
  className?: string;
}) {
  return (
    <div className={`segmented ${className}`} role="tablist">
      {items.map((item) => (
        <button key={item.value} className={value === item.value ? "active" : ""} onClick={() => onChange(item.value)}>
          {item.icon}
          {item.label}
        </button>
      ))}
    </div>
  );
}

function TrackList({
  items,
  token,
  roomId,
  playlists,
  queuedKeys,
  playlistMap,
  onChanged,
}: {
  items: Array<{ track: Track; meta?: string }>;
  token: string;
  roomId: number;
  playlists: Playlist[];
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  onChanged: () => void;
}) {
  if (!items.length) return <EmptyState title="暂无内容" />;
  return (
    <div className="songList">
      {items.map(({ track, meta }) => (
        <TrackRow
          key={trackKey(track)}
          track={track}
          meta={meta}
          token={token}
          roomId={roomId}
          playlists={playlists}
          queued={queuedKeys.has(trackKey(track))}
          playlistMemberships={playlistMap[trackKey(track)] || []}
          onChanged={onChanged}
        />
      ))}
    </div>
  );
}

function TrackRow({
  track,
  meta,
  token,
  roomId,
  playlists,
  queued,
  playlistMemberships,
  onChanged,
}: {
  track: Track;
  meta?: string;
  token: string;
  roomId: number;
  playlists: Playlist[];
  queued: boolean;
  playlistMemberships: PlaylistMembership[];
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState<"queue" | "playlist" | null>(null);
  const playlisted = playlistMemberships.length > 0;
  return (
    <article className="songRow">
      <TrackCover track={track} />
      <div className="songInfo">
        <strong>{track.title}</strong>
        <span>{meta || `${track.source} · ${track.artist || "-"}`}</span>
      </div>
      <div className="rowActions">
        <button
          className={`smallButton ${queued ? "active" : ""}`}
          disabled={busy === "queue"}
          onClick={async () => {
            setBusy("queue");
            await api(`/api/rooms/${roomId}/queue`, { method: "POST", token, json: trackPayload(track) }).finally(() => setBusy(null));
            onChanged();
          }}
        >
          <BadgePlus />{queued ? "已点" : "点歌"}
        </button>
        <PlaylistQuickButton
          track={track}
          token={token}
          playlists={playlists}
          memberships={playlistMemberships}
          playlisted={playlisted}
          busy={busy === "playlist"}
          setBusy={(value) => setBusy(value ? "playlist" : null)}
          onChanged={onChanged}
        />
      </div>
    </article>
  );
}

function PlaylistQuickButton({
  track,
  token,
  playlists,
  memberships,
  playlisted,
  busy,
  setBusy,
  onChanged,
}: {
  track: Track;
  token: string;
  playlists: Playlist[];
  memberships: PlaylistMembership[];
  playlisted: boolean;
  busy: boolean;
  setBusy: (value: boolean) => void;
  onChanged: () => void;
}) {
  const [open, setOpen] = useState(false);
  return (
    <>
      <button className={`smallButton ${playlisted ? "active" : ""}`} disabled={busy} onClick={() => setOpen(true)}>
        <Heart />{playlisted ? "已收藏" : "入歌单"}
      </button>
      {open && (
        <PlaylistPickerCard
          track={track}
          token={token}
          playlists={playlists}
          memberships={memberships}
          busy={busy}
          setBusy={setBusy}
          onClose={() => setOpen(false)}
          onChanged={onChanged}
        />
      )}
    </>
  );
}

function PlaylistPickerCard({
  track,
  token,
  playlists,
  memberships,
  busy,
  setBusy,
  onClose,
  onChanged,
}: {
  track: Track;
  token: string;
  playlists: Playlist[];
  memberships: PlaylistMembership[];
  busy: boolean;
  setBusy: (value: boolean) => void;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [localPlaylists, setLocalPlaylists] = useState(playlists);
  const [checkedIds, setCheckedIds] = useState(() => new Set(memberships.map((item) => item.playlist_id)));
  const [newName, setNewName] = useState("");
  const [error, setError] = useState("");
  const membershipByPlaylist = useMemo(
    () => new Map(memberships.map((item) => [item.playlist_id, item])),
    [memberships],
  );

  async function createPlaylist() {
    const name = newName.trim();
    if (!name) return;
    setError("");
    setBusy(true);
    try {
      const created = await api<Playlist>("/api/playlists", { method: "POST", token, json: { name } });
      setLocalPlaylists((items) => [...items, created]);
      setCheckedIds((previous) => {
        const next = new Set(previous);
        next.add(created.id);
        return next;
      });
      setNewName("");
      onChanged();
    } catch (err) {
      setError(err instanceof Error ? err.message : "创建歌单失败");
    } finally {
      setBusy(false);
    }
  }

  async function saveMemberships() {
    setError("");
    setBusy(true);
    try {
      for (const membership of memberships) {
        if (!checkedIds.has(membership.playlist_id)) {
          await api(`/api/playlists/${membership.playlist_id}/items/${membership.item_id}`, { method: "DELETE", token });
        }
      }
      for (const playlist of localPlaylists) {
        if (checkedIds.has(playlist.id) && !membershipByPlaylist.has(playlist.id)) {
          await api(`/api/playlists/${playlist.id}/items`, { method: "POST", token, json: trackPayload(track) });
        }
      }
      onChanged();
      onClose();
    } catch (err) {
      setError(err instanceof Error ? err.message : "保存收藏失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <CardDialog title="收藏到歌单" onClose={onClose}>
      <div className="pickerTrack">
        <TrackCover track={track} />
        <div className="songInfo">
          <strong>{track.title}</strong>
          <span>{track.artist || "-"} · {track.source}</span>
        </div>
      </div>
      <form className="inlineCreate dialogCreate" onSubmit={(event) => { event.preventDefault(); createPlaylist(); }}>
        <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="新歌单" />
        <button className="smallButton" disabled={busy || !newName.trim()}><Plus />创建</button>
      </form>
      {localPlaylists.length ? (
        <div className="playlistPickerList">
          {localPlaylists.map((playlist) => (
            <label className="checkRow" key={playlist.id}>
              <input
                type="checkbox"
                checked={checkedIds.has(playlist.id)}
                onChange={(event) => {
                  setCheckedIds((previous) => {
                    const next = new Set(previous);
                    if (event.target.checked) next.add(playlist.id);
                    else next.delete(playlist.id);
                    return next;
                  });
                }}
              />
              <span>{playlist.name}</span>
              <em>{playlist.item_count || 0} 首</em>
            </label>
          ))}
        </div>
      ) : (
        <EmptyState title="暂无歌单" meta="创建一个歌单后再保存。" />
      )}
      {error && <p className="hintText">{error}</p>}
      <div className="dialogActions">
        <button className="glassButton" onClick={onClose} disabled={busy}>取消</button>
        <button className="primaryButton" onClick={saveMemberships} disabled={busy}>
          <Check />{busy ? "保存中" : "保存"}
        </button>
      </div>
    </CardDialog>
  );
}

function SearchOverlay({
  token,
  roomId,
  queuedKeys,
  playlistMap,
  playlists,
  historyLimit,
  onClose,
  onChanged,
}: {
  token: string;
  roomId: number;
  queuedKeys: Set<string>;
  playlistMap: Record<string, PlaylistMembership[]>;
  playlists: Playlist[];
  historyLimit: number;
  onClose: () => void;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState("");
  const [history, setHistory] = useState<string[]>(() => {
    try {
      return JSON.parse(localStorage.getItem("searchHistory") || "[]") as string[];
    } catch {
      return [];
    }
  });
  const [results, setResults] = useState<Track[]>([]);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [historyVisible, setHistoryVisible] = useState(false);

  async function run(nextQuery = query) {
    const q = nextQuery.trim();
    if (!q) return;
    setBusy(true);
    setError("");
    setHistoryVisible(false);
    const nextHistory = [q, ...history.filter((item) => item !== q)].slice(0, historyLimit);
    setHistory(nextHistory);
    localStorage.setItem("searchHistory", JSON.stringify(nextHistory));
    try {
      setResults(await api<Track[]>(`/api/search?q=${encodeURIComponent(q)}`, { token }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "搜索失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="overlay">
      <section className="searchSheet glassPanel">
        <div className="searchSticky">
          <div className="sheetHeader">
            <h2>搜索</h2>
            <button className="iconButton" onClick={onClose}><X /></button>
          </div>
          <div className="searchInputWrap">
            <Search />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onFocus={() => setHistoryVisible(true)}
              onClick={() => setHistoryVisible(true)}
              onKeyDown={(e) => e.key === "Enter" && run()}
              autoFocus
              placeholder="歌曲、歌手、BV 号或 B 站标题"
            />
            <button className="primaryButton" onClick={() => run()} disabled={busy}>{busy ? "搜索中" : "搜索"}</button>
          </div>
          {historyVisible && !!history.length && (
            <div className="chips">
              {history.map((item) => (
                <button key={item} onClick={() => { setQuery(item); setHistoryVisible(false); run(item); }}>{item}</button>
              ))}
            </div>
          )}
          {error && <p className="hintText">{error}</p>}
        </div>
        <ScrollArea className="searchResults">
          <TrackList
            items={results.map((track) => ({ track }))}
            token={token}
            roomId={roomId}
            playlists={playlists}
            queuedKeys={queuedKeys}
            playlistMap={playlistMap}
            onChanged={onChanged}
          />
        </ScrollArea>
      </section>
    </div>
  );
}

function PlaylistsView({
  token,
  roomId,
  playlists,
  playlistMap,
  queuedKeys,
  onChanged,
}: {
  token: string;
  roomId: number;
  playlists: Playlist[];
  playlistMap: Record<string, PlaylistMembership[]>;
  queuedKeys: Set<string>;
  onChanged: () => void;
}) {
  const [selected, setSelected] = useState<Playlist | null>(null);
  const [createOpen, setCreateOpen] = useState(false);
  const [importOpen, setImportOpen] = useState(false);
  const [renameOpen, setRenameOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [renameName, setRenameName] = useState("");
  const [neteaseUrl, setNeteaseUrl] = useState("");
  const [neteaseName, setNeteaseName] = useState("");
  const [importMessage, setImportMessage] = useState("");
  const [importing, setImporting] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Playlist | null>(null);
  const [deleting, setDeleting] = useState(false);
  const itemsQuery = useQuery({
    queryKey: ["playlist-items", selected?.id, token],
    queryFn: () => api<PlaylistItem[]>(`/api/playlists/${selected?.id}/items`, { token }),
    enabled: !!selected,
  });

  async function createPlaylist() {
    const name = newName.trim();
    if (!name) return;
    await api("/api/playlists", { method: "POST", token, json: { name } });
    setNewName("");
    setCreateOpen(false);
    onChanged();
  }

  async function renamePlaylist() {
    if (!selected) return;
    const name = renameName.trim();
    if (!name) return;
    await api(`/api/playlists/${selected.id}`, { method: "PATCH", token, json: { name } });
    setSelected({ ...selected, name });
    setRenameOpen(false);
    onChanged();
  }

  async function importNeteasePlaylist() {
    const url = neteaseUrl.trim();
    if (!url) return;
    setImporting(true);
    setImportMessage("");
    try {
      const result = await api<{ playlist_name: string; added: number; skipped: number; total: number }>("/api/playlists/import-netease", {
        method: "POST",
        token,
        json: { url, name: neteaseName.trim() || undefined },
      });
      setNeteaseUrl("");
      setNeteaseName("");
      setImportOpen(false);
      setImportMessage(`已导入「${result.playlist_name}」：${result.added} 首，跳过 ${result.skipped} 首。`);
      onChanged();
    } catch (err) {
      setImportMessage(err instanceof Error ? err.message : "导入失败");
    } finally {
      setImporting(false);
    }
  }

  async function deletePlaylist() {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await api(`/api/playlists/${deleteTarget.id}`, { method: "DELETE", token });
      setDeleteTarget(null);
      onChanged();
    } finally {
      setDeleting(false);
    }
  }

  if (selected) {
    return (
      <div className="playlistDetail">
        <div className="sectionToolbar">
          <button className="iconTextButton" onClick={() => setSelected(null)}><ChevronLeft />返回</button>
          <h3 className="editableTitle">
            {selected.name}
            <button
              className="iconButton titleEditButton"
              title="重命名歌单"
              aria-label="重命名歌单"
              onClick={() => {
                setRenameName(selected.name);
                setRenameOpen(true);
              }}
            >
              <Pencil />
            </button>
          </h3>
          <button
            className="glassButton"
            onClick={async () => {
              await api(`/api/rooms/${roomId}/queue/playlist`, { method: "POST", token, json: { playlist_id: selected.id } });
              onChanged();
            }}
          >
            一键点歌
          </button>
        </div>
        <TrackList
          items={(itemsQuery.data || []).map((item) => ({ track: item.track }))}
          token={token}
          roomId={roomId}
          playlists={playlists}
          queuedKeys={queuedKeys}
          playlistMap={playlistMap}
          onChanged={onChanged}
        />
        {renameOpen && (
          <CardDialog title="重命名歌单" onClose={() => setRenameOpen(false)}>
            <form
              className="dialogForm"
              onSubmit={(event) => {
                event.preventDefault();
                renamePlaylist();
              }}
            >
              <input value={renameName} onChange={(event) => setRenameName(event.target.value)} placeholder="歌单名称" />
              <div className="dialogActions">
                <button type="button" className="glassButton" onClick={() => setRenameOpen(false)}>取消</button>
                <button className="primaryButton" disabled={!renameName.trim()}><Check />保存</button>
              </div>
            </form>
          </CardDialog>
        )}
      </div>
    );
  }

  return (
    <div className="playlistList">
      <div className="sectionToolbar">
        <h3>我的歌单</h3>
        <div className="toolbarActions">
          <button className="smallButton" onClick={() => setCreateOpen(true)}><Plus />新建歌单</button>
          <button className="smallButton" onClick={() => setImportOpen(true)}><CloudDownload />导入网易云</button>
        </div>
      </div>
      {importMessage && <p className="hintText">{importMessage}</p>}
      {!playlists.length && <EmptyState title="暂无歌单" meta="搜索后可将歌曲加入歌单。" />}
      {playlists.map((playlist) => (
        <article key={playlist.id} className="playlistRow" onClick={() => setSelected(playlist)}>
          <div>
            <strong>{playlist.name}</strong>
            <span>{playlist.item_count || 0} 首歌曲</span>
          </div>
          <div className="rowActions playlistRowActions">
            <button
              className="smallButton"
              onClick={async (e) => {
                e.stopPropagation();
                await api(`/api/rooms/${roomId}/queue/playlist`, { method: "POST", token, json: { playlist_id: playlist.id } });
                onChanged();
              }}
            >
              一键点歌
            </button>
            <button
              className="iconButton danger playlistDeleteButton"
              onClick={async (e) => {
                e.stopPropagation();
                setDeleteTarget(playlist);
              }}
            >
              <Trash2 />
            </button>
          </div>
        </article>
      ))}
      {deleteTarget && (
        <ConfirmDialog
          title="删除歌单"
          message={`删除歌单“${deleteTarget.name}”？`}
          confirmText="删除"
          busy={deleting}
          onCancel={() => setDeleteTarget(null)}
          onConfirm={deletePlaylist}
        />
      )}
      {createOpen && (
        <CardDialog title="新建歌单" onClose={() => setCreateOpen(false)}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              createPlaylist();
            }}
          >
            <input value={newName} onChange={(event) => setNewName(event.target.value)} placeholder="歌单名称" />
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setCreateOpen(false)}>取消</button>
              <button className="primaryButton" disabled={!newName.trim()}><Plus />创建</button>
            </div>
          </form>
        </CardDialog>
      )}
      {importOpen && (
        <CardDialog title="导入网易云歌单" onClose={() => setImportOpen(false)}>
          <form
            className="dialogForm"
            onSubmit={(event) => {
              event.preventDefault();
              importNeteasePlaylist();
            }}
          >
            <input value={neteaseUrl} onChange={(event) => setNeteaseUrl(event.target.value)} placeholder="网易云歌单链接或 ID" />
            <input value={neteaseName} onChange={(event) => setNeteaseName(event.target.value)} placeholder="导入后的歌单名，可留空" />
            <div className="dialogActions">
              <button type="button" className="glassButton" onClick={() => setImportOpen(false)} disabled={importing}>取消</button>
              <button className="primaryButton" disabled={importing || !neteaseUrl.trim()}>
                <CloudDownload />{importing ? "导入中" : "导入"}
              </button>
            </div>
          </form>
        </CardDialog>
      )}
    </div>
  );
}

function QueueTabs(props: {
  value: QueueTab;
  onChange: (value: QueueTab) => void;
  queue: QueueItem[];
  history: QueueItem[];
  queuedKeys: Set<string>;
  token: string;
  roomId: number;
  onChanged: () => void;
  compact?: boolean;
}) {
  const items = props.value === "queue"
    ? props.queue
    : props.history.filter((item) => !props.queuedKeys.has(trackKey(item.track)));
  if (!items.length) {
    return <EmptyState title={props.value === "queue" ? "队列为空" : "暂无历史"} meta={props.value === "queue" ? "搜索歌曲后点歌。" : "播完或切歌后会出现在这里。"} />;
  }
  return (
    <div className={props.compact ? "songList compact" : "songList"}>
      {props.value === "queue" && items.length > 1 && (
        <button
          className="toolbarButton"
          onClick={async () => {
            await api(`/api/rooms/${props.roomId}/queue/shuffle`, { method: "POST", token: props.token });
            props.onChanged();
          }}
        >
          <Shuffle />一键打乱
        </button>
      )}
      {items.map((item) => (
        <article className="songRow" key={item.id}>
          <TrackCover track={item.track} />
          <div className="songInfo">
            <strong>{item.track.title}</strong>
            <span>{item.track.artist || "-"} · {item.track.source} · {item.ordered_by.username}</span>
          </div>
          <div className="rowActions">
            {props.value === "queue" ? (
              <>
                <button className="iconButton bumpButton" title="顶歌" aria-label="顶歌" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue/${item.id}/bump`, { method: "POST", token: props.token }); props.onChanged(); }}><ArrowUpToLine /></button>
                <button className="iconButton danger" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue/${item.id}`, { method: "DELETE", token: props.token }); props.onChanged(); }}><Trash2 /></button>
              </>
            ) : (
              <button className="smallButton" onClick={async () => { await api(`/api/rooms/${props.roomId}/queue`, { method: "POST", token: props.token, json: trackPayload(item.track) }); props.onChanged(); }}>再点一次</button>
            )}
          </div>
        </article>
      ))}
    </div>
  );
}

function PlayerBar({ audio }: { audio: ReturnType<typeof useAudioController> }) {
  const title = audio.track?.title || "未播放";
  const meta = audio.track ? `${audio.track.artist || "-"} · ${audio.track.source}${audio.orderedBy?.username ? ` · ${audio.orderedBy.username} 点播` : ""}` : "-";
  return (
    <footer className="playerBar glassPanel">
      <div className="nowPlaying">
        <strong>{title}</strong>
        <span>{meta}</span>
        <div className="progressLine">
          <input
            type="range"
            min={0}
            max={1000}
            value={Math.round(audio.progressRatio * 1000)}
            onPointerDown={() => audio.setIsSeeking(true)}
            onChange={(e) => {
              const ratio = Number(e.target.value) / 1000;
              audio.setIsSeeking(true);
              audio.setPositionMs(ratio * audio.durationMs);
            }}
            onPointerUp={(e) => audio.commitSeek(Number((e.target as HTMLInputElement).value) / 1000)}
          />
          <span>{audio.currentTimeLabel} / {audio.durationLabel}</span>
        </div>
      </div>
      <div className="playerControls">
        <button className="transportButton" onClick={audio.playPause}>{audio.playback?.is_playing ? <Pause /> : <Play />}</button>
        <button className="transportButton" onClick={audio.next}><SkipForward /></button>
        <div className="volumeCluster">
          <button className="iconButton" onClick={audio.toggleMute}>{audio.volume > 0 ? <Volume2 /> : <VolumeX />}</button>
          <input type="range" min={0} max={100} value={audio.volume} disabled={!audio.playEnabled} onChange={(e) => audio.setVolume(Number(e.target.value))} />
          <label className="checkPill">
            <input type="checkbox" checked={audio.normalizerEnabled} onChange={(e) => audio.setNormalizerEnabled(e.target.checked)} />
            <Gauge />音量均衡
          </label>
        </div>
      </div>
    </footer>
  );
}

function UserMenu({ token, user, onClose, onLogout, onUpdated }: { token: string; user?: UserPublic; onClose: () => void; onLogout: () => void; onUpdated: () => void }) {
  const [name, setName] = useState(user?.username || "");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [message, setMessage] = useState("");
  return (
    <div className="popoverLayer" onMouseDown={onClose}>
      <aside className="popover glassPanel accountPopover" onMouseDown={(event) => event.stopPropagation()}>
      <div className="sheetHeader">
        <h3>账户</h3>
        <button className="iconButton" onClick={onClose}><X /></button>
      </div>
      <form className="miniForm" onSubmit={async (e) => { e.preventDefault(); await api("/api/me", { method: "PATCH", token, json: { username: name.trim() } }); setMessage("用户名已更新"); onUpdated(); }}>
        <input value={name} onChange={(e) => setName(e.target.value)} placeholder="新用户名" />
        <button className="smallButton">保存用户名</button>
      </form>
      <form className="miniForm" onSubmit={async (e) => { e.preventDefault(); await api("/api/me/password", { method: "PATCH", token, json: { old_password: oldPassword, new_password: newPassword } }); setMessage("密码已更新"); setOldPassword(""); setNewPassword(""); }}>
        <input type="password" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} placeholder="旧密码" />
        <input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="新密码" />
        <button className="smallButton">保存密码</button>
      </form>
      {message && <p className="hintText">{message}</p>}
      <button className="dangerButton wide" onClick={onLogout}><LogOut />退出登录</button>
      </aside>
    </div>
  );
}

function MembersPanel({ members, loading, onClose }: { members: Array<{ id: number; username: string }>; loading: boolean; onClose: () => void }) {
  return (
    <div className="popoverLayer" onMouseDown={onClose}>
      <aside className="popover glassPanel membersPopover" onMouseDown={(event) => event.stopPropagation()}>
      <div className="sheetHeader">
        <h3>房间成员</h3>
        <button className="iconButton" onClick={onClose}><X /></button>
      </div>
      {loading && <EmptyState title="加载中" />}
      {!loading && !members.length && <EmptyState title="暂无成员" />}
      {members.map((member) => <div className="memberRow" key={member.id}><CircleUserRound />{member.username}</div>)}
      </aside>
    </div>
  );
}

function AdminView({ token, onExit }: { token: string; onExit: () => void }) {
  const queryClient = useQueryClient();
  const [busyUserId, setBusyUserId] = useState<number | null>(null);
  const [destructiveBusy, setDestructiveBusy] = useState(false);
  const [deleteUserTarget, setDeleteUserTarget] = useState<AdminUser | null>(null);
  const [deleteRoomTarget, setDeleteRoomTarget] = useState<AdminRoom | null>(null);
  const [removeMemberTarget, setRemoveMemberTarget] = useState<{ room: AdminRoom; member: { id: number; username: string } } | null>(null);
  const meQuery = useQuery({ queryKey: ["admin-me", token], queryFn: () => api<UserPublic>("/api/me", { token }) });
  const usersQuery = useQuery({ queryKey: ["admin-users", token], queryFn: () => api<Array<AdminUser>>("/api/admin/users", { token }) });
  const roomsQuery = useQuery({ queryKey: ["admin-rooms", token], queryFn: () => api<Array<AdminRoom>>("/api/admin/rooms", { token }) });

  async function toggleAdmin(user: UserPublic) {
    setBusyUserId(user.id);
    try {
      await api(`/api/admin/users/${user.id}`, { method: "PATCH", token, json: { is_admin: !user.is_admin } });
      queryClient.invalidateQueries({ queryKey: ["admin-users", token] });
    } finally {
      setBusyUserId(null);
    }
  }

  async function refreshAdminData() {
    await Promise.all([
      queryClient.invalidateQueries({ queryKey: ["admin-users", token] }),
      queryClient.invalidateQueries({ queryKey: ["admin-rooms", token] }),
      queryClient.invalidateQueries({ queryKey: ["rooms"] }),
      queryClient.invalidateQueries({ queryKey: ["members"] }),
    ]);
  }

  async function deleteUser() {
    if (!deleteUserTarget) return;
    setDestructiveBusy(true);
    try {
      await api(`/api/admin/users/${deleteUserTarget.id}`, { method: "DELETE", token });
      setDeleteUserTarget(null);
      await refreshAdminData();
    } finally {
      setDestructiveBusy(false);
    }
  }

  async function deleteRoom() {
    if (!deleteRoomTarget) return;
    setDestructiveBusy(true);
    try {
      await api(`/api/admin/rooms/${deleteRoomTarget.id}`, { method: "DELETE", token });
      setDeleteRoomTarget(null);
      await refreshAdminData();
    } finally {
      setDestructiveBusy(false);
    }
  }

  async function removeRoomMember() {
    if (!removeMemberTarget) return;
    setDestructiveBusy(true);
    try {
      await api(`/api/admin/rooms/${removeMemberTarget.room.id}/members/${removeMemberTarget.member.id}`, { method: "DELETE", token });
      setRemoveMemberTarget(null);
      await refreshAdminData();
    } finally {
      setDestructiveBusy(false);
    }
  }

  return (
    <main className="adminScene">
      <section className="glassPanel adminPanel">
        <div className="sectionToolbar">
          <h1>管理后台</h1>
          <button className="dangerButton" onClick={onExit}>退出管理端</button>
        </div>
        <h2>用户</h2>
        <div className="adminList">
          {(usersQuery.data || []).map((user) => {
            const isSelf = meQuery.data?.id === user.id;
            return (
              <div className="adminRow userAdminRow" key={user.id}>
                <span className="adminPrimaryText">{user.username}</span>
                <span className="adminMeta">{user.is_admin ? "管理员" : "用户"}</span>
                <div className="adminActions">
                  <button
                    className={`smallButton ${user.is_admin ? "active" : ""}`}
                    disabled={busyUserId === user.id || (isSelf && user.is_admin)}
                    onClick={() => toggleAdmin(user)}
                  >
                    <UserCog />{user.is_admin ? "取消管理员" : "设为管理员"}
                  </button>
                  <button
                    className="iconButton dangerIconButton"
                    disabled={isSelf}
                    title={isSelf ? "不能删除自己" : "删除用户"}
                    onClick={() => setDeleteUserTarget(user)}
                  >
                    <Trash2 />
                  </button>
                </div>
              </div>
            );
          })}
        </div>
        <h2>房间</h2>
        <div className="adminList">
          {(roomsQuery.data || []).map((room) => (
            <div className="adminRow adminRoomRow" key={room.id}>
              <div className="adminRoomInfo">
                <strong>{room.name}</strong>
                <span>创建者 {room.created_by} · {(room.members || []).length} 人</span>
              </div>
              <div className="adminMemberChips">
                {(room.members || []).map((member) => (
                  <button
                    className="memberChip"
                    key={member.id}
                    title={`将 ${member.username} 踢出 ${room.name}`}
                    onClick={() => setRemoveMemberTarget({ room, member })}
                  >
                    <CircleUserRound />
                    <span>{member.username}</span>
                    <X />
                  </button>
                ))}
                {!(room.members || []).length && <span className="hintText">暂无成员</span>}
              </div>
              <button
                className="iconButton dangerIconButton"
                title="删除房间"
                onClick={() => setDeleteRoomTarget(room)}
              >
                <Trash2 />
              </button>
            </div>
          ))}
        </div>
      </section>
      {deleteUserTarget && (
        <ConfirmDialog
          title="删除用户"
          message={`确定删除用户「${deleteUserTarget.username}」吗？该用户的歌单和房间成员关系会被移除。`}
          confirmText="删除用户"
          busy={destructiveBusy}
          onCancel={() => setDeleteUserTarget(null)}
          onConfirm={deleteUser}
        />
      )}
      {deleteRoomTarget && (
        <ConfirmDialog
          title="删除房间"
          message={`确定删除房间「${deleteRoomTarget.name}」吗？队列、播放状态和成员关系会一并删除。`}
          confirmText="删除房间"
          busy={destructiveBusy}
          onCancel={() => setDeleteRoomTarget(null)}
          onConfirm={deleteRoom}
        />
      )}
      {removeMemberTarget && (
        <ConfirmDialog
          title="踢出成员"
          message={`确定将「${removeMemberTarget.member.username}」踢出房间「${removeMemberTarget.room.name}」吗？`}
          confirmText="踢出房间"
          busy={destructiveBusy}
          onCancel={() => setRemoveMemberTarget(null)}
          onConfirm={removeRoomMember}
        />
      )}
    </main>
  );
}

function EmptyState({ title, meta }: { title: string; meta?: string }) {
  return (
    <div className="emptyState">
      <Music2 />
      <strong>{title}</strong>
      {meta && <span>{meta}</span>}
    </div>
  );
}
