import { useState } from "react";
import { CircleUserRound, LogOut, X } from "lucide-react";
import { api } from "../lib/api";
import type { UserPublic } from "../types";
import { EmptyState } from "./common";

export function UserMenu({ token, user, onClose, onLogout, onUpdated }: { token: string; user?: UserPublic; onClose: () => void; onLogout: () => void; onUpdated: () => void }) {
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

export function MembersPanel({ members, loading, onClose }: { members: Array<{ id: number; username: string }>; loading: boolean; onClose: () => void }) {
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
