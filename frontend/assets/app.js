import { state, SYNC_INTERVAL_MS, TRENDING_SYNC_INTERVAL_MS, stopPeriodicSync } from './state.js';
import { api } from './api.js';
import { escapeHtml } from './utils.js';
import {
  audio, debouncedNext, setPauseIcon, updateVolumeUi, syncPlayEnabledUi,
  setNowPlaying, syncLocalAudioToRoom, onPlaybackUpdated,
  getDesiredPositionMs, getCurrentPositionMs, updateTimeDisplay,
} from './player.js';
import { showView, setChromeVisible, setUserLabel, toggleUserMenu } from './ui.js';
import { refreshQueue, refreshHistory } from './queue.js';
import { loadPlaylists } from './playlist.js';
import { openSearchOverlay, closeSearchOverlay, runSearch, renderHistory, loadTrending } from './search.js';
import { loadRooms, handleRoomGone, startRoomsRefresh, stopRoomsRefresh } from './rooms.js';
import { connectWs } from './ws.js';

// --- Periodic sync ---

async function syncRoomState() {
  if (!state.roomId) return;
  try {
    const data = await api(`/api/rooms/${state.roomId}/state`);
    if (!data || !data.playback_state) return;
    await onPlaybackUpdated(data.playback_state, data.current_track, data.ordered_by);
  } catch (e) {
    if (e.status === 404 || e.status === 401) {
      handleRoomGone();
    }
  }
}

function startPeriodicSync() {
  stopPeriodicSync();
  state.syncTimer = setInterval(() => {
    syncRoomState().catch(() => {});
  }, SYNC_INTERVAL_MS);
  state.trendingSyncTimer = setInterval(() => {
    loadTrending().catch(() => {});
    loadPlaylists().catch(() => {});
  }, TRENDING_SYNC_INTERVAL_MS);
}

// --- Bootstrap ---

export async function bootstrap() {
  if (!state.token) {
    showView("viewAuth");
    setChromeVisible(false);
    stopRoomsRefresh();
    return;
  }
  state.me = null;
  setUserLabel(null);
  try {
    state.me = await api("/api/me");
    setUserLabel(state.me.username);
  } catch (e) {
    if (e.status === 401) {
      state.token = null;
      localStorage.removeItem("token");
      localStorage.removeItem("roomId");
      state.roomId = null;
      showView("viewAuth");
      setChromeVisible(false);
      return;
    }
  }

  if (!state.roomId) {
    showView("viewRooms");
    setChromeVisible(false);
    await loadRooms();
    startRoomsRefresh();
    return;
  }
  stopRoomsRefresh();
  showView("viewApp");
  setChromeVisible(true);
  document.getElementById("leaveRoom").disabled = false;
  connectWs();
  syncPlayEnabledUi();
  updateVolumeUi();
  try {
    await refreshQueue();
    await refreshHistory();
    await syncRoomState();
  } catch (e) {
    if (e.status === 404) {
      handleRoomGone();
      return;
    }
  }
  if (!state.roomId) return;
  await loadPlaylists();
  await loadTrending();
  startPeriodicSync();
  if (state.playEnabled) syncLocalAudioToRoom();
}

// --- Initial UI setup ---

setUserLabel(null);

document.getElementById("pauseBtn").innerHTML =
  '<svg viewBox="0 0 24 24"><path d="M8 6v12"/><path d="M16 6v12"/></svg>';
document.getElementById("nextBtn").innerHTML =
  '<svg viewBox="0 0 24 24"><path d="M18 5v14"/><path d="M4 6l10 6-10 6z"/></svg>';
updateVolumeUi();

// --- Event listeners: mode switch ---

document.getElementById("modeSwitch").addEventListener("change", (e) => {
  state.playEnabled = !!e.target.checked;
  localStorage.setItem("playEnabled", state.playEnabled ? "1" : "0");
  syncPlayEnabledUi();
  if (!state.roomId) return;

  if (state.playEnabled) {
    syncLocalAudioToRoom();
  } else {
    audio.pause();
    setPauseIcon(!!(state.lastPb && state.lastPb.current_queue_item_id && state.lastPb.is_playing));
  }
});

// --- Event listeners: topbar ---

document.getElementById("userButton").addEventListener("click", () => toggleUserMenu());

document.getElementById("leaveRoom").addEventListener("click", async () => {
  if (!state.roomId) return;
  await api(`/api/rooms/${state.roomId}/leave`, { method: "POST" });
  localStorage.removeItem("roomId");
  state.roomId = null;
  stopPeriodicSync();
  document.getElementById("leaveRoom").disabled = true;
  document.getElementById("queueList").innerHTML = "";
  setNowPlaying(null);
  setChromeVisible(false);
  showView("viewRooms");
  await loadRooms();
  startRoomsRefresh();
});

// --- Event listeners: search ---

document.getElementById("searchInput").addEventListener("focus", () => openSearchOverlay());
document.getElementById("searchBtn").addEventListener("click", () => openSearchOverlay());
document.getElementById("btnCloseSearch").addEventListener("click", () => closeSearchOverlay());
document.getElementById("searchBtn2").addEventListener("click", () => runSearch());
document.getElementById("searchInput2").addEventListener("keydown", (e) => {
  if (e.key === "Enter") runSearch();
});
document.getElementById("searchInput2").addEventListener("focus", () => {
  document.getElementById("historyWrap").classList.remove("hidden");
  renderHistory();
});

// --- Event listeners: tabs ---

document.querySelectorAll(".tab[data-tab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-tab]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.getAttribute("data-tab");
    document.getElementById("tabTrending").classList.toggle("hidden", tab !== "trending");
    document.getElementById("tabPlaylists").classList.toggle("hidden", tab !== "playlists");
    const q = document.getElementById("tabQueue");
    if (q) q.classList.toggle("hidden", tab !== "queue");
    if (tab === "playlists") loadPlaylists().catch(() => {});
    if (tab === "trending") loadTrending().catch(() => {});
  });
});

document.querySelectorAll(".tab[data-rtab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-rtab]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.getAttribute("data-rtab");
    document.getElementById("rightTabQueue").classList.toggle("hidden", tab !== "queue");
    document.getElementById("rightTabHistory").classList.toggle("hidden", tab !== "history");
  });
});

document.querySelectorAll(".tab[data-mtab]").forEach((btn) => {
  btn.addEventListener("click", () => {
    document.querySelectorAll(".tab[data-mtab]").forEach((b) => b.classList.remove("active"));
    btn.classList.add("active");
    const tab = btn.getAttribute("data-mtab");
    document.getElementById("mobileTabQueue").classList.toggle("hidden", tab !== "queue");
    document.getElementById("mobileTabHistory").classList.toggle("hidden", tab !== "history");
  });
});

// --- Event listeners: player controls ---

document.getElementById("pauseBtn").addEventListener("click", async () => {
  if (!state.roomId) return;
  const isPlaying = !!(state.lastPb && state.lastPb.current_queue_item_id && state.lastPb.is_playing);
  if (isPlaying) {
    const posMs = Math.round(getCurrentPositionMs());
    await api(`/api/rooms/${state.roomId}/controls/pause`, { method: "POST", json: { position_ms: posMs } });
  } else {
    await api(`/api/rooms/${state.roomId}/controls/play`, { method: "POST" });
  }
});
document.getElementById("nextBtn").addEventListener("click", () => debouncedNext());

document.getElementById("volume").addEventListener("input", (e) => {
  const v = Number(e.target.value);
  state.volume = Math.max(0, Math.min(100, v));
  if (state.volume > 0) {
    state.previousVolume = state.volume;
    localStorage.setItem("previousVolume", String(state.previousVolume));
  }
  localStorage.setItem("volume", String(state.volume));
  updateVolumeUi();
});
document.getElementById("muteBtn").addEventListener("click", () => {
  if (!state.playEnabled) return;
  if (state.volume > 0) {
    state.previousVolume = state.volume;
    state.volume = 0;
  } else {
    state.volume = state.previousVolume || 50;
  }
  localStorage.setItem("volume", String(state.volume));
  localStorage.setItem("previousVolume", String(state.previousVolume || 50));
  updateVolumeUi();
});

document.getElementById("progress").addEventListener("input", () => {
  const desiredMs = getDesiredPositionMs();
  state.suppressNextSeek = true;
  if (state.playEnabled && audio.duration && isFinite(audio.duration)) {
    try {
      audio.currentTime = desiredMs / 1000;
    } catch (_) {}
  }
  updateTimeDisplay();
});
document.getElementById("progress").addEventListener("change", async () => {
  if (!state.roomId) return;
  const desiredMs = getDesiredPositionMs();
  await api(`/api/rooms/${state.roomId}/controls/position`, { method: "PATCH", json: { position_ms: desiredMs } }).catch(() => {});
});

// --- Event listeners: auth ---

document.getElementById("btnLogin").addEventListener("click", async () => {
  const username = document.getElementById("authUsername").value.trim();
  const password = document.getElementById("authPassword").value;
  if (!username || !password) return;
  try {
    const out = await api("/api/auth/login", { method: "POST", json: { username, password } });
    state.token = out.token;
    localStorage.setItem("token", state.token);
    document.getElementById("authHint").textContent = "";
    await bootstrap();
  } catch (e) {
    document.getElementById("authHint").textContent = `登录失败：${e.message}`;
  }
});

document.getElementById("btnRegister").addEventListener("click", async () => {
  const username = document.getElementById("authUsername").value.trim();
  const password = document.getElementById("authPassword").value;
  if (!username || !password) return;
  try {
    await api("/api/auth/register", { method: "POST", json: { username, password } });
    document.getElementById("authHint").textContent = "注册成功，请登录";
  } catch (e) {
    document.getElementById("authHint").textContent = `注册失败：${e.message}`;
  }
});

// --- Event listeners: rooms ---

document.getElementById("btnCreateRoom").addEventListener("click", async () => {
  const name = document.getElementById("roomName").value.trim();
  const created = await api("/api/rooms", { method: "POST", json: { name: name || null } });
  state.roomId = created.id;
  localStorage.setItem("roomId", String(state.roomId));
  await bootstrap();
});

// --- Event listeners: user menu ---

document.addEventListener("mousedown", (e) => {
  const menu = document.getElementById("userMenu");
  if (menu.classList.contains("hidden")) return;
  const btn = document.getElementById("userButton");
  if (menu.contains(e.target) || btn.contains(e.target)) return;
  toggleUserMenu(true);
});

document.getElementById("btnOpenRename").addEventListener("click", () => {
  document.getElementById("renameBox").classList.toggle("hidden");
  document.getElementById("passwordBox").classList.add("hidden");
});
document.getElementById("btnOpenPassword").addEventListener("click", () => {
  document.getElementById("passwordBox").classList.toggle("hidden");
  document.getElementById("renameBox").classList.add("hidden");
});
document.getElementById("btnRename").addEventListener("click", async () => {
  const username = document.getElementById("newUsername").value.trim();
  if (!username) return;
  try {
    const me = await api("/api/me", { method: "PATCH", json: { username } });
    setUserLabel(me.username);
    document.getElementById("userMenuHint").textContent = "已更新用户名";
    document.getElementById("renameBox").classList.add("hidden");
  } catch (e) {
    document.getElementById("userMenuHint").textContent = `失败：${e.message}`;
  }
});
document.getElementById("btnChangePassword").addEventListener("click", async () => {
  const old_password = document.getElementById("oldPassword").value;
  const new_password = document.getElementById("newPassword").value;
  if (!old_password || !new_password) return;
  try {
    await api("/api/me/password", { method: "PATCH", json: { old_password, new_password } });
    document.getElementById("userMenuHint").textContent = "已更新密码";
    document.getElementById("passwordBox").classList.add("hidden");
  } catch (e) {
    document.getElementById("userMenuHint").textContent = `失败：${e.message}`;
  }
});
document.getElementById("btnLogout").addEventListener("click", async () => {
  state.token = null;
  state.me = null;
  localStorage.removeItem("token");
  localStorage.removeItem("roomId");
  state.roomId = null;
  stopPeriodicSync();
  stopRoomsRefresh();
  setUserLabel(null);
  toggleUserMenu(true);
  showView("viewAuth");
  setChromeVisible(false);
});

// --- Event listeners: members panel ---

document.getElementById("btnMembers").addEventListener("click", async () => {
  const panel = document.getElementById("membersPanel");
  if (!panel.classList.contains("hidden")) {
    panel.classList.add("hidden");
    return;
  }
  const btn = document.getElementById("btnMembers");
  const r = btn.getBoundingClientRect();
  panel.style.right = `${Math.max(8, window.innerWidth - r.right)}px`;
  panel.style.left = "auto";
  panel.style.top = `${r.bottom + 8}px`;
  panel.classList.remove("hidden");
  const list = document.getElementById("membersList");
  list.innerHTML = `<div class="muted">加载中…</div>`;
  try {
    const members = await api(`/api/rooms/${state.roomId}/members`);
    list.innerHTML = "";
    if (!members.length) {
      list.innerHTML = `<div class="muted">暂无成员</div>`;
      return;
    }
    members.forEach((m, i) => {
      const row = document.createElement("div");
      row.className = "item";
      row.style.animationDelay = `${i * 50}ms`;
      row.innerHTML = `<div><div class="title">${escapeHtml(m.username)}</div></div>`;
      list.appendChild(row);
    });
  } catch (_) {
    list.innerHTML = `<div class="muted">加载失败</div>`;
  }
});

document.addEventListener("mousedown", (e) => {
  const panel = document.getElementById("membersPanel");
  if (panel.classList.contains("hidden")) return;
  const btn = document.getElementById("btnMembers");
  if (panel.contains(e.target) || btn.contains(e.target)) return;
  panel.classList.add("hidden");
});

// --- Start ---

bootstrap().catch(() => {});
