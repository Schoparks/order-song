import { useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { LogOut, MoreHorizontal, Music2, Plus, Search, Settings, UsersRound } from "lucide-react";
import { api } from "../lib/api";
import { beginMobileKeyboardDismissal, settleMobileViewportBeforeRouteChange } from "../hooks/useStableViewportHeight";
import { useAnchoredPopover } from "../hooks/useRoomChrome";
import type { LoginOut, Room, UserPublic } from "../types";
import { EmptyState } from "./common";

export function avatarTone(value: string): number {
  return Array.from(value).reduce((total, char) => total + char.charCodeAt(0), 0) % 6;
}

export function InitialAvatar({ name, small = false }: { name: string; small?: boolean }) {
  return <span className={`initialAvatar tone${avatarTone(name)} ${small ? "small" : ""}`} aria-hidden="true">{name.trim().slice(0, 1).toUpperCase() || "?"}</span>;
}

export function AuthView({
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
    beginMobileKeyboardDismissal();
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
      await settleMobileViewportBeforeRouteChange();
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
          <label className="fieldLabel"><span>用户名</span><input className="input" value={username} onChange={(e) => setUsername(e.target.value)} placeholder="请输入用户名" autoComplete="username" /></label>
          <label className="fieldLabel"><span>密码</span><input className="input" value={password} onChange={(e) => setPassword(e.target.value)} placeholder="请输入密码" type="password" autoComplete="current-password" onKeyDown={(e) => e.key === "Enter" && submit("login")} /></label>
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

export function RoomView({
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
  const [joiningRoomId, setJoiningRoomId] = useState<number | null>(null);
  const [joinError, setJoinError] = useState("");
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
        <label className="fieldLabel"><span>房间名称（可为空）</span><input className="input" value={name} onChange={(e) => setName(e.target.value)} placeholder="例如：周末歌房" /></label>
        <button className="primaryButton" onClick={() => createRoom.mutate()} disabled={createRoom.isPending}>
          <Plus />{createRoom.isPending ? "创建中" : "创建并进入"}
        </button>
      </section>
      {(joinError || createRoom.error) && (
        <p className="actionError" role="alert">{joinError || (createRoom.error instanceof Error ? createRoom.error.message : "创建房间失败")}</p>
      )}
      <section className="roomGrid">
        {loading && <EmptyState title="正在加载房间" />}
        {!loading && !rooms.length && <EmptyState title="暂无房间" meta="创建一个房间开始点歌。" />}
        {rooms.map((room) => (
          <button
            key={room.id}
            className="roomTile glassPanel"
            disabled={joiningRoomId !== null}
            onClick={async () => {
              setJoinError("");
              setJoiningRoomId(room.id);
              try {
                await api(`/api/rooms/${room.id}/join`, { method: "POST", token });
                queryClient.invalidateQueries({ queryKey: ["rooms", token] });
                onEnter(room.id);
              } catch (error) {
                setJoinError(error instanceof Error ? error.message : "加入房间失败");
              } finally {
                setJoiningRoomId(null);
              }
            }}
          >
            <div className="roomTileCover">
              {room.now_playing?.cover_url
                ? <img src={room.now_playing.cover_url} alt="" referrerPolicy="no-referrer" />
                : <Music2 />}
              <span className={`roomPlaybackState ${room.now_playing?.is_playing ? "playing" : ""}`}>{room.now_playing ? (room.now_playing.is_playing ? "播放中" : "已暂停") : "等待开场"}</span>
            </div>
            <div className="roomTileBody">
              <span className="roomIndex">#{room.id}</span>
              <strong>{joiningRoomId === room.id ? "加入中…" : room.name}</strong>
              <span className="roomNowPlaying">{room.now_playing ? `${room.now_playing.title}${room.now_playing.artist ? ` · ${room.now_playing.artist}` : ""}` : "还没有人点歌"}</span>
              <div className="roomMembers"><span className="avatarStack">{(room.member_names || []).slice(0, 4).map((name) => <InitialAvatar key={name} name={name} small />)}</span><span>{room.member_count || 0} 人</span></div>
            </div>
          </button>
        ))}
      </section>
    </main>
  );
}

export function TopBar({
  roomName,
  username,
  members,
  connectionState,
  roomMenuOpen,
  membersOpen,
  searchOpen,
  onToggleUserMenu,
  onToggleMembers,
  onSearch,
  onToggleRoomMenu,
  onLeaveRequest,
}: {
  roomName: string;
  username: string;
  members: Array<{ id: number; username: string }>;
  connectionState: "connected" | "reconnecting" | "offline";
  roomMenuOpen: boolean;
  membersOpen: boolean;
  searchOpen: boolean;
  onToggleUserMenu: () => void;
  onToggleMembers: () => void;
  onSearch: () => void;
  onToggleRoomMenu: () => void;
  onLeaveRequest: () => void;
}) {
  const roomMenuButtonRef = useRef<HTMLButtonElement | null>(null);
  const roomMenuPosition = useAnchoredPopover(roomMenuOpen, roomMenuButtonRef, "below");
  return (
    <header className="topBar glassPanel">
      <div className="roomIdentity">
        <div><span className={`connectionDot ${connectionState}`} /><span className="eyebrow">{connectionState === "connected" ? "已连接" : connectionState === "offline" ? "离线" : "重连中"}</span></div>
        <strong>{roomName}</strong>
      </div>
      <div className="topActions">
        <button className={`avatarStackButton ${membersOpen ? "active" : ""}`} aria-label={`查看 ${members.length} 位房间成员`} aria-haspopup="dialog" aria-controls="members-popover" aria-expanded={membersOpen} onClick={onToggleMembers}>
          <span className="avatarStack">{members.slice(0, 4).map((member) => <InitialAvatar key={member.id} name={member.username} small />)}</span>
          <span className="memberCount">{members.length}</span>
        </button>
        <button
          className="pillButton searchButton"
          aria-label="搜索歌曲"
          aria-haspopup="dialog"
          aria-expanded={searchOpen}
          aria-controls="search-overlay"
          onClick={onSearch}
        ><Search /><span>搜索</span></button>
        <button
          ref={roomMenuButtonRef}
          className={`iconButton roomMenuButton ${roomMenuOpen ? "active" : ""}`}
          aria-label="房间与账户菜单"
          aria-haspopup="menu"
          aria-expanded={roomMenuOpen}
          aria-controls="room-menu"
          onClick={onToggleRoomMenu}
        ><MoreHorizontal /></button>
        {roomMenuOpen && createPortal(<div id="room-menu" className="roomMenu glassPanel" style={roomMenuPosition} role="menu" aria-label="房间与账户菜单">
          <div className="roomMenuUser"><InitialAvatar name={username} /><span><strong>{username}</strong><small>当前账户</small></span></div>
          <button role="menuitem" onClick={onToggleMembers}><UsersRound />房间成员 <small>{members.length}</small></button>
          <button role="menuitem" onClick={onToggleUserMenu}><Settings />账户设置</button>
          <button role="menuitem" className="dangerText" onClick={onLeaveRequest}><LogOut />退出房间</button>
        </div>, document.body)}
      </div>
    </header>
  );
}
