import { state, stopPeriodicSync } from './state.js';
import { api } from './api.js';
import { escapeHtml } from './utils.js';
import { showView, setChromeVisible } from './ui.js';
import { audio, setNowPlaying } from './player.js';

export function handleRoomGone() {
  localStorage.removeItem("roomId");
  state.roomId = null;
  stopPeriodicSync();
  if (state.ws) { try { state.ws.close(); } catch (_) {} state.ws = null; }
  audio.pause();
  audio.src = "";
  document.getElementById("leaveRoom").disabled = true;
  document.getElementById("queueList").innerHTML = "";
  const hl = document.getElementById("historyList");
  if (hl) hl.innerHTML = "";
  setNowPlaying(null);
  setChromeVisible(false);
  showView("viewRooms");
  loadRooms().catch(() => {});
  startRoomsRefresh();
}

export function formatMemberSummary(names, count) {
  if (!count) return "暂无成员";
  const shown = names.map(escapeHtml).join("、");
  if (count <= names.length) return shown;
  return `${shown} 等${count}人`;
}

export async function loadRooms() {
  const rooms = await api("/api/rooms");
  const el = document.getElementById("roomList");
  el.innerHTML = "";
  if (!rooms.length) {
    el.innerHTML = `<div class="item"><div><div class="title">暂无房间</div><div class="meta">你可以创建一个房间</div></div></div>`;
    return;
  }
  rooms.forEach((r, idx) => {
    const row = document.createElement("div");
    row.className = "item";
    row.style.animationDelay = `${idx * 50}ms`;
    const summary = formatMemberSummary(r.member_names || [], r.member_count || 0);
    row.innerHTML = `
      <div>
        <div class="title">${escapeHtml(r.name)}</div>
        <div class="meta">${r.member_count || 0}人 · ${summary}</div>
      </div>
      <div class="actions"><button class="btn small">进入</button></div>
    `;
    row.querySelector("button").addEventListener("click", async () => {
      state.roomId = r.id;
      await api(`/api/rooms/${state.roomId}/join`, { method: "POST" });
      localStorage.setItem("roomId", String(state.roomId));
      const { bootstrap } = await import('./app.js');
      await bootstrap();
    });
    el.appendChild(row);
  });
}

export function startRoomsRefresh() {
  stopRoomsRefresh();
  state.roomsRefreshTimer = setInterval(() => {
    loadRooms().catch(() => {});
  }, 5000);
}

export function stopRoomsRefresh() {
  if (state.roomsRefreshTimer) {
    clearInterval(state.roomsRefreshTimer);
    state.roomsRefreshTimer = null;
  }
}
