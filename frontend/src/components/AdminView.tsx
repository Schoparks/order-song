import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { ArrowLeft, ChevronLeft, ChevronRight, CircleUserRound, DoorOpen, KeyRound, Trash2, UserCog, UsersRound, X } from "lucide-react";
import { api } from "../lib/api";
import type { Room, UserPublic } from "../types";
import { ConfirmDialog, SegmentedTabs } from "./common";

type AdminUser = UserPublic & { last_active_room_id?: number | null };
type AdminRoom = Room & { created_by: string; members: Array<{ id: number; username: string }> };
type AdminSection = "users" | "rooms";
const USERS_PER_PAGE = 8;
const ROOMS_PER_PAGE = 6;

function Pagination({ label, page, pageCount, onChange }: { label: string; page: number; pageCount: number; onChange: (page: number) => void }) {
  if (pageCount <= 1) return null;
  return (
    <nav className="adminPagination" aria-label={`${label}分页`}>
      <button className="smallButton" disabled={page <= 1} onClick={() => onChange(page - 1)}><ChevronLeft />上一页</button>
      <span>{page} / {pageCount}</span>
      <button className="smallButton" disabled={page >= pageCount} onClick={() => onChange(page + 1)}>下一页<ChevronRight /></button>
    </nav>
  );
}

export function AdminView({ token, onExit }: { token: string; onExit: () => void }) {
  const queryClient = useQueryClient();
  const [activeSection, setActiveSection] = useState<AdminSection>("users");
  const [userPage, setUserPage] = useState(1);
  const [roomPage, setRoomPage] = useState(1);
  const [busyUserId, setBusyUserId] = useState<number | null>(null);
  const [destructiveBusy, setDestructiveBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const [resetPasswordTarget, setResetPasswordTarget] = useState<AdminUser | null>(null);
  const [deleteUserTarget, setDeleteUserTarget] = useState<AdminUser | null>(null);
  const [deleteRoomTarget, setDeleteRoomTarget] = useState<AdminRoom | null>(null);
  const [removeMemberTarget, setRemoveMemberTarget] = useState<{ room: AdminRoom; member: { id: number; username: string } } | null>(null);
  const meQuery = useQuery({ queryKey: ["admin-me", token], queryFn: () => api<UserPublic>("/api/me", { token }) });
  const usersQuery = useQuery({ queryKey: ["admin-users", token], queryFn: () => api<Array<AdminUser>>("/api/admin/users", { token }) });
  const roomsQuery = useQuery({ queryKey: ["admin-rooms", token], queryFn: () => api<Array<AdminRoom>>("/api/admin/rooms", { token }) });
  const users = usersQuery.data || [];
  const rooms = roomsQuery.data || [];
  const userPageCount = Math.max(1, Math.ceil(users.length / USERS_PER_PAGE));
  const roomPageCount = Math.max(1, Math.ceil(rooms.length / ROOMS_PER_PAGE));
  const visibleUserPage = Math.min(userPage, userPageCount);
  const visibleRoomPage = Math.min(roomPage, roomPageCount);
  const visibleUsers = users.slice((visibleUserPage - 1) * USERS_PER_PAGE, visibleUserPage * USERS_PER_PAGE);
  const visibleRooms = rooms.slice((visibleRoomPage - 1) * ROOMS_PER_PAGE, visibleRoomPage * ROOMS_PER_PAGE);

  async function toggleAdmin(user: UserPublic) {
    setBusyUserId(user.id);
    setNotice("");
    try {
      await api(`/api/admin/users/${user.id}`, { method: "PATCH", token, json: { is_admin: !user.is_admin } });
      await queryClient.invalidateQueries({ queryKey: ["admin-users", token] });
    } catch (error) {
      setNotice(error instanceof Error ? error.message : "管理员权限更新失败");
    } finally {
      setBusyUserId(null);
    }
  }

  async function resetPassword() {
    if (!resetPasswordTarget) return;
    setDestructiveBusy(true);
    setNotice("");
    try {
      await api(`/api/admin/users/${resetPasswordTarget.id}/reset-password`, { method: "POST", token });
      setNotice(`已将「${resetPasswordTarget.username}」的密码重置为 123456`);
      setResetPasswordTarget(null);
    } finally {
      setDestructiveBusy(false);
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
          <button className="glassButton" onClick={onExit}><ArrowLeft />返回点歌</button>
        </div>
        {notice && <p className="hintText" role="status">{notice}</p>}
        <SegmentedTabs
          className="adminSectionTabs"
          label="管理内容"
          value={activeSection}
          onChange={(value) => setActiveSection(value as AdminSection)}
          items={[
            { value: "users", label: `用户 ${users.length}`, icon: <UsersRound /> },
            { value: "rooms", label: `房间 ${rooms.length}`, icon: <DoorOpen /> },
          ]}
        />
        {activeSection === "users" && (
          <section className="adminSection" aria-label="用户管理">
            <h2>用户</h2>
            <div className="adminList">
              {visibleUsers.map((user) => {
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
                        className="smallButton"
                        disabled={busyUserId === user.id}
                        onClick={() => setResetPasswordTarget(user)}
                      >
                        <KeyRound />重置密码
                      </button>
                      <button
                        className="iconButton dangerIconButton"
                        aria-label={`删除用户 ${user.username}`}
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
            <Pagination label="用户列表" page={visibleUserPage} pageCount={userPageCount} onChange={setUserPage} />
          </section>
        )}
        {activeSection === "rooms" && (
          <section className="adminSection" aria-label="房间管理">
            <h2>房间</h2>
            <div className="adminList">
              {visibleRooms.map((room) => (
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
                    aria-label={`删除房间 ${room.name}`}
                    title="删除房间"
                    onClick={() => setDeleteRoomTarget(room)}
                  >
                    <Trash2 />
                  </button>
                </div>
              ))}
            </div>
            <Pagination label="房间列表" page={visibleRoomPage} pageCount={roomPageCount} onChange={setRoomPage} />
          </section>
        )}
      </section>
      {resetPasswordTarget && (
        <ConfirmDialog
          title="重置密码"
          message={`确定将用户「${resetPasswordTarget.username}」的密码重置为 123456 吗？请提醒该用户登录后尽快修改密码。`}
          confirmText="重置为 123456"
          busy={destructiveBusy}
          onCancel={() => setResetPasswordTarget(null)}
          onConfirm={resetPassword}
        />
      )}
      {deleteUserTarget && (
        <ConfirmDialog
          title="删除用户"
          message={`确定删除用户「${deleteUserTarget.username}」吗？其创建的房间（即使仍有其他成员）、房间队列与播放状态、个人歌单和成员关系都会被删除。`}
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
