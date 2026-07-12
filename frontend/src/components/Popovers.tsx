import { useCallback, useId, useRef, useState } from "react";
import { LogOut, X } from "lucide-react";
import { api } from "../lib/api";
import { useDialogViewportLock } from "../hooks/useStableViewportHeight";
import type { UserPublic } from "../types";
import { EmptyState, useModalFocus } from "./common";
import { InitialAvatar } from "./AuthRoom";

export function UserMenu({ token, user, onClose, onLogout, onUpdated }: { token: string; user?: UserPublic; onClose: () => void; onLogout: () => void; onUpdated: () => void }) {
  useDialogViewportLock();
  const titleId = useId();
  const nameInputRef = useRef<HTMLInputElement | null>(null);
  const [name, setName] = useState(user?.username || "");
  const [oldPassword, setOldPassword] = useState("");
  const [newPassword, setNewPassword] = useState("");
  const [notice, setNotice] = useState<{ text: string; error: boolean } | null>(null);
  const [pending, setPending] = useState<"username" | "password" | null>(null);
  const closeMenu = useCallback(() => {
    if (!pending) onClose();
  }, [onClose, pending]);
  const { dialogRef, onDialogKeyDown } = useModalFocus({ onClose: closeMenu, canDismiss: !pending, initialFocusRef: nameInputRef });

  async function saveUsername() {
    const username = name.trim();
    if (!username || pending) return;
    setPending("username");
    setNotice(null);
    try {
      await api("/api/me", { method: "PATCH", token, json: { username } });
      setNotice({ text: "用户名已更新", error: false });
      onUpdated();
    } catch (error) {
      setNotice({ text: error instanceof Error ? error.message : "用户名更新失败", error: true });
    } finally {
      setPending(null);
    }
  }

  async function savePassword() {
    if (!oldPassword || !newPassword || pending) return;
    setPending("password");
    setNotice(null);
    try {
      await api("/api/me/password", { method: "PATCH", token, json: { old_password: oldPassword, new_password: newPassword } });
      setNotice({ text: "密码已更新", error: false });
      setOldPassword("");
      setNewPassword("");
    } catch (error) {
      setNotice({ text: error instanceof Error ? error.message : "密码更新失败", error: true });
    } finally {
      setPending(null);
    }
  }

  return (
    <div className="popoverLayer" onMouseDown={(event) => event.target === event.currentTarget && closeMenu()}>
      <aside
        id="account-popover"
        ref={dialogRef}
        className="popover glassPanel accountPopover"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        aria-busy={!!pending || undefined}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <div className="sheetHeader">
          <h3 id={titleId}>账户</h3>
          <button className="iconButton" aria-label="关闭账户菜单" onClick={closeMenu} disabled={!!pending}><X /></button>
        </div>
        <form className="miniForm" onSubmit={(event) => { event.preventDefault(); void saveUsername(); }}>
          <label className="fieldLabel"><span>新用户名</span><input ref={nameInputRef} value={name} onChange={(e) => setName(e.target.value)} placeholder="输入新用户名" autoComplete="username" disabled={!!pending} /></label>
          <button className="smallButton" disabled={!!pending || !name.trim()}>{pending === "username" ? "保存中…" : "保存用户名"}</button>
        </form>
        <form className="miniForm" onSubmit={(event) => { event.preventDefault(); void savePassword(); }}>
          <label className="fieldLabel"><span>旧密码</span><input type="password" value={oldPassword} onChange={(e) => setOldPassword(e.target.value)} placeholder="输入旧密码" autoComplete="current-password" disabled={!!pending} /></label>
          <label className="fieldLabel"><span>新密码</span><input type="password" value={newPassword} onChange={(e) => setNewPassword(e.target.value)} placeholder="输入新密码" autoComplete="new-password" disabled={!!pending} /></label>
          <button className="smallButton" disabled={!!pending || !oldPassword || !newPassword}>{pending === "password" ? "保存中…" : "保存密码"}</button>
        </form>
        {notice && <p className={notice.error ? "actionError" : "hintText"} role={notice.error ? "alert" : "status"}>{notice.text}</p>}
        <button className="dangerButton wide" onClick={onLogout} disabled={!!pending}><LogOut />退出登录</button>
      </aside>
    </div>
  );
}

export function MembersPanel({ members, loading, onClose }: { members: Array<{ id: number; username: string }>; loading: boolean; onClose: () => void }) {
  useDialogViewportLock();
  const titleId = useId();
  const { dialogRef, onDialogKeyDown } = useModalFocus({ onClose });
  return (
    <div className="popoverLayer" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <aside
        id="members-popover"
        ref={dialogRef}
        className="popover glassPanel membersPopover"
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        tabIndex={-1}
        onKeyDown={onDialogKeyDown}
      >
        <div className="sheetHeader">
          <h3 id={titleId}>房间成员</h3>
          <button className="iconButton" aria-label="关闭成员列表" onClick={onClose}><X /></button>
        </div>
        {loading && <EmptyState title="加载中" />}
        {!loading && !members.length && <EmptyState title="暂无成员" />}
        {members.map((member) => <div className="memberRow" key={member.id}><InitialAvatar name={member.username} small />{member.username}</div>)}
      </aside>
    </div>
  );
}
