import { useState } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { CircleUserRound, LogOut, Music2, Plus, Search, UsersRound } from "lucide-react";
import { api } from "../lib/api";
import { beginMobileKeyboardDismissal, settleMobileViewportBeforeRouteChange } from "../hooks/useStableViewportHeight";
import type { LoginOut, Room, UserPublic } from "../types";
import { EmptyState } from "./common";

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

export function TopBar({
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
