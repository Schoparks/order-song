import { useState } from "react";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import { CircleUserRound, Trash2, UserCog, X } from "lucide-react";
import { api } from "../lib/api";
import type { Room, UserPublic } from "../types";
import { ConfirmDialog } from "./common";

type AdminUser = UserPublic & { last_active_room_id?: number | null };
type AdminRoom = Room & { created_by: string; members: Array<{ id: number; username: string }> };

export function AdminView({ token, onExit }: { token: string; onExit: () => void }) {
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
