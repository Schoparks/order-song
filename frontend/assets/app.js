function readStoredNumber(key, fallback, min = 0, max = 100) {
  const value = Number(localStorage.getItem(key) ?? String(fallback));
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

const NEXT_DEBOUNCE_MS = 2500;
const SYNC_INTERVAL_MS = 5000;
const TRENDING_SYNC_INTERVAL_MS = 60000;
const SEEK_TOLERANCE_S = 10;

const state = {
  token: localStorage.getItem("token") || null,
  playEnabled: (localStorage.getItem("playEnabled") || "0") === "1",
  roomId: Number(localStorage.getItem("roomId") || "") || null,
  ws: null,
  me: null,
  volume: readStoredNumber("volume", 50),
  previousVolume: readStoredNumber("previousVolume", 50, 1, 100),
  queuedKeys: new Set(),
  lastPb: null,
  lastTrack: null,
  suppressNextSeek: false,
  progressTimer: null,
  lastNextAt: 0,
  playlistKeys: new Map(),
  playlistItems: [],
  defaultPlaylistId: null,
  syncTimer: null,
  trendingSyncTimer: null,
  roomsRefreshTimer: null,
};

function setUserLabel(username = null) {
  const btn = document.getElementById("userButton");
  btn.textContent = username || (state.token ? "账号" : "登录");
}

function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.getElementById(id).classList.remove("hidden");
}

function setChromeVisible(isInRoom) {
  document.getElementById("topbar").classList.toggle("hidden", !isInRoom);
  document.getElementById("playerBar").classList.toggle("hidden", !isInRoom);
}

function handleRoomGone() {
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

async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (options.json) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const res = await fetch(path, { ...options, headers });
  if (!res.ok) {
    const txt = await res.text();
    const err = new Error(txt || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return await res.json();
  return await res.text();
}

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

document.getElementById("userButton").addEventListener("click", async () => {
  toggleUserMenu();
});

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

// open search overlay
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

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
}

setUserLabel(null);

document.getElementById("pauseBtn").innerHTML =
  '<svg viewBox="0 0 24 24"><path d="M8 6v12"/><path d="M16 6v12"/></svg>';
document.getElementById("nextBtn").innerHTML =
  '<svg viewBox="0 0 24 24"><path d="M18 5v14"/><path d="M4 6l10 6-10 6z"/></svg>';
updateVolumeUi();

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

async function ensureRoom() {
  if (!state.roomId) throw new Error("not in room");
  return state.roomId;
}

async function ensurePlaylist() {
  const pls = await api("/api/playlists");
  if (pls.length) return pls[0];
  return await api("/api/playlists", { method: "POST", json: { name: "我的歌单" } });
}

function buildQueueRow(it) {
  const row = document.createElement("div");
  row.className = "item";
  row.innerHTML = `
    <div>
      <div class="title">${escapeHtml(it.track.title)}</div>
      <div class="meta">${escapeHtml(it.track.artist || "-")} · ${escapeHtml(it.track.source)}</div>
    </div>
    <div class="actions">
      <span class="meta">${escapeHtml(it.ordered_by.username)}</span>
      <button class="iconBtn small" title="顶歌" aria-label="顶歌" data-action="bump">
        <svg viewBox="0 0 24 24"><path d="M12 5l7 7H5z"/><path d="M5 19h14"/></svg>
      </button>
      <button class="iconBtn small danger" title="删除" aria-label="删除" data-action="remove">
        <svg viewBox="0 0 24 24"><path d="M18 6L6 18"/><path d="M6 6l12 12"/></svg>
      </button>
    </div>
  `;
  row.querySelector('[data-action="remove"]').addEventListener("click", async () => {
    await api(`/api/rooms/${state.roomId}/queue/${it.id}`, { method: "DELETE" });
    await refreshQueue();
    await refreshHistory();
  });
  row.querySelector('[data-action="bump"]').addEventListener("click", async () => {
    await api(`/api/rooms/${state.roomId}/queue/${it.id}/bump`, { method: "POST" });
    await refreshQueue();
  });
  return row;
}

async function refreshQueue() {
  if (!state.roomId) return;
  const items = await api(`/api/rooms/${state.roomId}/queue`);
  state.queuedKeys = new Set(items.map((it) => `${it.track.source}:${it.track.source_track_id}`));
  for (const container of [document.getElementById("queueList"), document.getElementById("queueListMobile")]) {
    if (!container) continue;
    container.innerHTML = "";
    for (const it of items) container.appendChild(buildQueueRow(it));
  }
  rerenderSearchButtons();
  rerenderAllPlaylistButtons();
}

function buildHistoryRow(it) {
  const row = document.createElement("div");
  row.className = "item";
  row.innerHTML = `
    <div>
      <div class="title">${escapeHtml(it.track.title)}</div>
      <div class="meta">${escapeHtml(it.track.artist || "-")} · ${escapeHtml(it.track.source)} · ${escapeHtml(it.ordered_by.username)}</div>
    </div>
    <div class="actions"><button class="btn small">再点一次</button></div>
  `;
  row.querySelector("button").addEventListener("click", async () => {
    await api(`/api/rooms/${state.roomId}/queue`, {
      method: "POST",
      json: {
        source: it.track.source,
        source_track_id: it.track.source_track_id,
        title: it.track.title,
        artist: it.track.artist,
        duration_ms: it.track.duration_ms,
        cover_url: it.track.cover_url,
      },
    });
    await refreshQueue();
    await refreshHistory();
  });
  return row;
}

async function refreshHistory() {
  if (!state.roomId) return;
  const items = await api(`/api/rooms/${state.roomId}/history`);
  const filtered = items.filter((it) => !state.queuedKeys.has(`${it.track.source}:${it.track.source_track_id}`));
  const emptyHtml = `<div class="item"><div><div class="title">暂无</div><div class="meta">播完 / 切歌后会出现在这里</div></div></div>`;
  for (const container of [document.getElementById("historyList"), document.getElementById("historyListMobile")]) {
    if (!container) continue;
    container.innerHTML = "";
    if (!filtered.length) {
      container.innerHTML = emptyHtml;
      continue;
    }
    for (const it of filtered) container.appendChild(buildHistoryRow(it));
  }
}

function setNowPlaying(pb) {
  if (!pb || !pb.current_queue_item_id) {
    document.getElementById("nowTitle").textContent = "未播放";
    document.getElementById("nowMeta").textContent = "-";
    document.getElementById("progress").value = "0";
    updateTimeDisplay();
    return;
  }
  const tr = state.lastTrack;
  if (tr) {
    document.getElementById("nowTitle").textContent = tr.title || "播放中";
    document.getElementById("nowMeta").textContent = `${tr.artist || "-"} · ${tr.source}`;
  } else {
    document.getElementById("nowTitle").textContent = "播放中";
    document.getElementById("nowMeta").textContent = "-";
  }
  syncPlayEnabledUi();
  document.getElementById("volume").value = String(state.volume);
  updateTimeDisplay();
}

function connectWs() {
  if (!state.roomId) return;
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
  }
  state._wsBackoff = 0;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.addEventListener("open", () => {
    state._wsBackoff = 0;
    state.ws.send(JSON.stringify({ type: "join_room", room_id: state.roomId, token: state.token }));
  });
  state.ws.addEventListener("message", async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "queue_updated") {
        await refreshQueue();
        await refreshHistory();
      }
      if (msg.type === "playback_updated") {
        await onPlaybackUpdated(msg.playback_state, msg.current_track);
        await refreshQueue();
        await refreshHistory();
      }
      if (msg.type === "room_destroyed") {
        handleRoomGone();
      }
      if (msg.type === "room_member_left" && state.me && msg.user_id === state.me.id) {
        handleRoomGone();
      }
    } catch (_) {}
  });
  state.ws.addEventListener("close", () => {
    if (state.roomId) {
      const delay = Math.min(30000, 1000 * Math.pow(2, state._wsBackoff || 0));
      state._wsBackoff = (state._wsBackoff || 0) + 1;
      setTimeout(() => connectWs(), delay);
    }
  });
}

// Audio playback
const audio = new Audio();
audio.preload = "none";
function debouncedNext() {
  if (!state.roomId) return;
  const now = Date.now();
  if (now - state.lastNextAt < NEXT_DEBOUNCE_MS) return;
  state.lastNextAt = now;
  api(`/api/rooms/${state.roomId}/controls/next`, { method: "POST" }).catch(() => {});
}

audio.addEventListener("ended", () => debouncedNext());
audio.addEventListener("timeupdate", () => {
  updateProgressFromAudio();
  updateTimeDisplay();
});
audio.addEventListener("loadedmetadata", () => {
  updateProgressFromAudio(true);
  updateTimeDisplay();
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

function setPauseIcon(isPlaying) {
  document.getElementById("pauseBtn").innerHTML = isPlaying
    ? '<svg viewBox="0 0 24 24"><path d="M8 6v12"/><path d="M16 6v12"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M8 5l12 7-12 7z"/></svg>';
}

async function onPlaybackUpdated(pb, currentTrack) {
  state.lastPb = pb;
  state.lastTrack = currentTrack || (pb && pb.current_queue_item_id ? state.lastTrack : null);
  if (pb && pb.current_queue_item_id && state._prevQueueItemId !== pb.current_queue_item_id) {
    state.lastNextAt = 0;
  }
  state._prevQueueItemId = pb ? pb.current_queue_item_id : null;
  setNowPlaying(pb);
  setPauseIcon(!!(pb && pb.current_queue_item_id && pb.is_playing));
  updateProgressTimer();
  if (!pb || !pb.current_queue_item_id) {
    audio.pause();
    audio.src = "";
    setPauseIcon(false);
    updateProgressFromRoom(true);
    return;
  }

  const track = state.lastTrack;
  if (track && track.audio_url) {
    const newSrc = track.audio_url;
    const srcChanged = audioSrcChanged(newSrc);
    if (srcChanged) {
      audio.src = newSrc;
    }
    document.getElementById("nowTitle").textContent = track.title || "播放中";
    document.getElementById("nowMeta").textContent = `${track.artist || "-"} · ${track.source}`;
    updateVolumeUi();
    if (state.playEnabled) {
      const target = getSafeRoomPositionMs(pb) / 1000;
      if (!state.suppressNextSeek && isFinite(target) && target >= 0 && (!audio.duration || target <= audio.duration + 1)) {
        const diff = Math.abs((audio.currentTime || 0) - target);
        if (diff > SEEK_TOLERANCE_S) {
          try { audio.currentTime = target; } catch (_) {}
        }
      }
      state.suppressNextSeek = false;
    }
    if (!state.playEnabled) {
      audio.pause();
      updateProgressFromRoom(true);
      return;
    }
    if (pb.is_playing) {
      await audio.play().catch(() => {});
      setPauseIcon(true);
    } else {
      audio.pause();
      setPauseIcon(false);
    }
  } else {
    setPauseIcon(!!pb.is_playing);
    updateProgressFromRoom(true);
  }
  forceProgressUpdate();
}

function forceProgressUpdate() {
  const bar = document.getElementById("progress");
  if (!bar || document.activeElement === bar) return;
  if (state.playEnabled && audio.duration && isFinite(audio.duration) && audio.duration > 0) {
    bar.value = String(Math.floor(((audio.currentTime || 0) / audio.duration) * 1000));
  } else {
    updateProgressFromRoom(true);
  }
  updateTimeDisplay();
}

async function loadTrending() {
  if (!state.token) return;
  const items = await api("/api/trending?limit=20");
  const el = document.getElementById("tabTrending");
  el.innerHTML = "";
  for (const it of items) {
    const t = it.track;
    const row = document.createElement("div");
    row.className = "item";
    row.innerHTML = `
      <div>
        <div class="title">${escapeHtml(t.title)}</div>
        <div class="meta">${escapeHtml(t.source)} · ${escapeHtml(t.artist || "-")} · ${it.order_count}次</div>
      </div>
      <div class="actions">
        <button class="btn small js-order">点歌</button>
        <button class="btn small js-fav">加入歌单</button>
      </div>
    `;
    const orderBtn = row.querySelector(".js-order");
    orderBtn.setAttribute("data-track-key", trackKey(t));
    syncOrderButtonState(orderBtn, t);
    orderBtn.addEventListener("click", async () => {
      await ensureRoom();
      await api(`/api/rooms/${state.roomId}/queue`, {
        method: "POST",
        json: {
          source: t.source,
          source_track_id: t.source_track_id,
          title: t.title,
          artist: t.artist,
          duration_ms: t.duration_ms,
          cover_url: t.cover_url,
        },
      });
      await refreshQueue();
      syncOrderButtonState(orderBtn, t);
    });
    const favBtn = row.querySelector(".js-fav");
    favBtn.setAttribute("data-track-key", trackKey(t));
    syncPlaylistButtonState(favBtn, t);
    favBtn.addEventListener("click", () => togglePlaylistItem(t, favBtn));
    el.appendChild(row);
  }
}

async function loadPlaylistData() {
  if (!state.token) return;

  const pls = await api("/api/playlists").catch(() => []);
  if (!pls.length) {
    state.playlistKeys = new Map();
    state.playlistItems = [];
    state.defaultPlaylistId = null;
    return;
  }

  const pl = pls[0];
  const items = await api(`/api/playlists/${pl.id}/items`).catch(() => []);
  const newKeys = new Map();
  for (const it of items) {
    newKeys.set(trackKey(it.track), { item_id: it.id, playlist_id: pl.id });
  }

  state.defaultPlaylistId = pl.id;
  state.playlistItems = items;
  state.playlistKeys = newKeys;
}

async function loadPlaylists() {
  if (!state.token) return;
  await loadPlaylistData();
  renderPlaylistView();
}

function renderPlaylistView() {
  const el = document.getElementById("tabPlaylists");
  el.innerHTML = "";

  if (!state.playlistItems.length) {
    el.innerHTML = `<div class="item"><div><div class="title">暂无歌单</div><div class="meta">搜索后可加入歌单</div></div></div>`;
    return;
  }

  for (const it of state.playlistItems) {
    const t = it.track;
    const row = document.createElement("div");
    row.className = "item";
    row.innerHTML = `
      <div>
        <div class="title">${escapeHtml(t.title)}</div>
        <div class="meta">${escapeHtml(t.source)} · ${escapeHtml(t.artist || "-")}</div>
      </div>
      <div class="actions">
        <button class="btn small js-order">点歌</button>
        <button class="btn small js-fav">已添加</button>
      </div>
    `;
    const orderBtn = row.querySelector(".js-order");
    orderBtn.setAttribute("data-track-key", trackKey(t));
    syncOrderButtonState(orderBtn, t);
    orderBtn.addEventListener("click", async () => {
      await ensureRoom();
      await api(`/api/rooms/${state.roomId}/queue`, {
        method: "POST",
        json: {
          source: t.source,
          source_track_id: t.source_track_id,
          title: t.title,
          artist: t.artist,
          duration_ms: t.duration_ms,
          cover_url: t.cover_url,
        },
      });
      await refreshQueue();
      syncOrderButtonState(orderBtn, t);
    });
    const favBtn = row.querySelector(".js-fav");
    favBtn.setAttribute("data-track-key", trackKey(t));
    const key = trackKey(t);
    const inPlaylist = state.playlistKeys.has(key);
    if (inPlaylist) {
      favBtn.textContent = "已添加";
      favBtn.classList.add("subtle");
    } else {
      favBtn.textContent = "加入歌单";
      favBtn.classList.remove("subtle");
    }
    favBtn.addEventListener("click", async () => {
      await togglePlaylistItem(t, favBtn);
      renderPlaylistView();
    });
    el.appendChild(row);
  }
}

async function bootstrap() {
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

async function syncRoomState() {
  if (!state.roomId) return;
  try {
    const data = await api(`/api/rooms/${state.roomId}/state`);
    if (!data || !data.playback_state) return;
    await onPlaybackUpdated(data.playback_state, data.current_track);
  } catch (e) {
    if (e.status === 404 || e.status === 401) {
      handleRoomGone();
    }
  }
}

bootstrap().catch(() => {});

// Auth form
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

// Rooms view
function formatMemberSummary(names, count) {
  if (!count) return "暂无成员";
  const shown = names.map(escapeHtml).join("、");
  if (count <= names.length) return shown;
  return `${shown} 等${count}人`;
}

async function loadRooms() {
  const rooms = await api("/api/rooms");
  const el = document.getElementById("roomList");
  el.innerHTML = "";
  if (!rooms.length) {
    el.innerHTML = `<div class="item"><div><div class="title">暂无房间</div><div class="meta">你可以创建一个房间</div></div></div>`;
    return;
  }
  for (const r of rooms) {
    const row = document.createElement("div");
    row.className = "item";
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
      await bootstrap();
    });
    el.appendChild(row);
  }
}

function startRoomsRefresh() {
  stopRoomsRefresh();
  state.roomsRefreshTimer = setInterval(() => {
    loadRooms().catch(() => {});
  }, 5000);
}
function stopRoomsRefresh() {
  if (state.roomsRefreshTimer) {
    clearInterval(state.roomsRefreshTimer);
    state.roomsRefreshTimer = null;
  }
}

document.getElementById("btnCreateRoom").addEventListener("click", async () => {
  const name = document.getElementById("roomName").value.trim();
  const created = await api("/api/rooms", { method: "POST", json: { name: name || null } });
  state.roomId = created.id;
  localStorage.setItem("roomId", String(state.roomId));
  await bootstrap();
});

function toggleUserMenu(forceClose = false) {
  const menu = document.getElementById("userMenu");
  const btn = document.getElementById("userButton");
  if (forceClose) {
    menu.classList.add("hidden");
    return;
  }
  const willOpen = menu.classList.contains("hidden");
  if (!willOpen) {
    menu.classList.add("hidden");
    return;
  }
  const r = btn.getBoundingClientRect();
  menu.style.left = `${Math.max(8, r.left)}px`;
  menu.style.top = `${r.bottom + 8}px`;
  menu.classList.remove("hidden");
  document.getElementById("userMenuHint").textContent = "";
  document.getElementById("renameBox").classList.add("hidden");
  document.getElementById("passwordBox").classList.add("hidden");
}

document.addEventListener("mousedown", (e) => {
  const menu = document.getElementById("userMenu");
  if (menu.classList.contains("hidden")) return;
  const btn = document.getElementById("userButton");
  if (menu.contains(e.target) || btn.contains(e.target)) return;
  toggleUserMenu(true);
});

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
    for (const m of members) {
      const row = document.createElement("div");
      row.className = "item";
      row.innerHTML = `<div><div class="title">${escapeHtml(m.username)}</div></div>`;
      list.appendChild(row);
    }
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

// Search overlay + history
function getHistory() {
  try {
    return JSON.parse(localStorage.getItem("searchHistory") || "[]");
  } catch (_) {
    return [];
  }
}
function saveHistory(list) {
  localStorage.setItem("searchHistory", JSON.stringify(list.slice(0, 30)));
}
function renderHistory() {
  const el = document.getElementById("searchHistory");
  const list = getHistory();
  el.innerHTML = "";
  if (!list.length) {
    el.innerHTML = `<div class="muted">暂无记录</div>`;
    return;
  }
  for (const q of list) {
    const chip = document.createElement("button");
    chip.className = "chip";
    chip.type = "button";
    chip.textContent = q;
    chip.addEventListener("click", () => {
      document.getElementById("searchInput2").value = q;
      runSearch();
    });
    el.appendChild(chip);
  }
}
function openSearchOverlay() {
  if (!state.roomId) return;
  document.getElementById("searchInput2").value = document.getElementById("searchInput").value || "";
  document.getElementById("viewSearch").classList.remove("hidden");
  renderHistory();
  document.getElementById("historyWrap").classList.remove("hidden");
  document.getElementById("searchResults2").innerHTML = "";
  document.getElementById("searchInput2").focus();
}
function closeSearchOverlay() {
  document.getElementById("viewSearch").classList.add("hidden");
}
async function runSearch() {
  const q = document.getElementById("searchInput2").value.trim();
  if (!q) return;
  const hist = [q, ...getHistory().filter((x) => x !== q)];
  saveHistory(hist);
  document.getElementById("historyWrap").classList.add("hidden");
  const resultsEl = document.getElementById("searchResults2");
  resultsEl.innerHTML = "";
  try {
    const items = await api(`/api/search?q=${encodeURIComponent(q)}`);
    for (const t of items) {
      const el = document.createElement("div");
      el.className = "item";
      el.innerHTML = `
        <div>
          <div class="title">${escapeHtml(t.title || "-")}</div>
          <div class="meta">${escapeHtml(t.source)} · ${escapeHtml(t.artist || "-")}</div>
        </div>
        <div class="actions">
          <button class="btn small js-order">点歌</button>
          <button class="btn small js-fav">加入歌单</button>
        </div>
      `;
      const orderBtn = el.querySelector(".js-order");
      orderBtn.setAttribute("data-track-key", trackKey(t));
      syncOrderButtonState(orderBtn, t);
      orderBtn.addEventListener("click", async () => {
        await api(`/api/rooms/${state.roomId}/queue`, {
          method: "POST",
          json: {
            source: t.source,
            source_track_id: t.source_track_id,
            title: t.title,
            artist: t.artist,
            duration_ms: t.duration_ms,
            cover_url: t.cover_url,
          },
        });
        await refreshQueue();
        syncOrderButtonState(orderBtn, t);
      });
      const favBtn = el.querySelector(".js-fav");
      favBtn.setAttribute("data-track-key", trackKey(t));
      syncPlaylistButtonState(favBtn, t);
      favBtn.addEventListener("click", () => togglePlaylistItem(t, favBtn));
      resultsEl.appendChild(el);
    }
  } catch (e) {
    resultsEl.innerHTML = `<div class="item"><div><div class="title">搜索失败</div><div class="meta">${escapeHtml(e.message)}</div></div></div>`;
  }
}

function trackKey(t) {
  return `${t.source}:${t.source_track_id}`;
}

function syncOrderButtonState(btn, t) {
  const already = state.queuedKeys && state.queuedKeys.has(trackKey(t));
  if (already) {
    btn.textContent = "已点";
    btn.disabled = true;
    btn.classList.add("subtle");
  } else {
    btn.textContent = "点歌";
    btn.disabled = false;
    btn.classList.remove("subtle");
  }
}

function syncPlaylistButtonState(btn, t) {
  const key = trackKey(t);
  const inPlaylist = state.playlistKeys.has(key);
  if (inPlaylist) {
    btn.textContent = "已添加";
    btn.classList.add("subtle");
  } else {
    btn.textContent = "加入歌单";
    btn.classList.remove("subtle");
  }
}

async function togglePlaylistItem(t, btn) {
  const key = trackKey(t);
  if (state.playlistKeys.has(key)) {
    const info = state.playlistKeys.get(key);
    await api(`/api/playlists/${info.playlist_id}/items/${info.item_id}`, { method: "DELETE" });
    state.playlistKeys.delete(key);
    state.playlistItems = state.playlistItems.filter((it) => trackKey(it.track) !== key);
  } else {
    const pl = await ensurePlaylist();
    const result = await api(`/api/playlists/${pl.id}/items`, {
      method: "POST",
      json: {
        source: t.source,
        source_track_id: t.source_track_id,
        title: t.title,
        artist: t.artist,
        duration_ms: t.duration_ms,
        cover_url: t.cover_url,
      },
    });
    state.defaultPlaylistId = pl.id;
    state.playlistKeys.set(key, { item_id: result.id, playlist_id: pl.id });
    state.playlistItems.push({ id: result.id, track: t });
  }
  if (btn) syncPlaylistButtonState(btn, t);
  rerenderAllPlaylistButtons();
}

function rerenderSearchButtons() {
  document.querySelectorAll(".js-order[data-track-key]").forEach((btn) => {
    const key = btn.getAttribute("data-track-key");
    if (!key) return;
    const already = state.queuedKeys && state.queuedKeys.has(key);
    if (already) {
      btn.textContent = "已点";
      btn.disabled = true;
      btn.classList.add("subtle");
    } else {
      btn.textContent = "点歌";
      btn.disabled = false;
      btn.classList.remove("subtle");
    }
  });
}

function rerenderAllPlaylistButtons() {
  document.querySelectorAll(".js-fav[data-track-key]").forEach((btn) => {
    const key = btn.getAttribute("data-track-key");
    if (!key) return;
    const inPlaylist = state.playlistKeys.has(key);
    if (inPlaylist) {
      btn.textContent = "已添加";
      btn.classList.add("subtle");
    } else {
      btn.textContent = "加入歌单";
      btn.classList.remove("subtle");
    }
  });
}

function updateProgressFromAudio(force = false) {
  const bar = document.getElementById("progress");
  if (!bar) return;
  const dur = audio.duration;
  if (!dur || !isFinite(dur) || dur <= 0) {
    if (force) bar.value = "0";
    return;
  }
  if (document.activeElement === bar) return;
  bar.value = String(Math.floor(((audio.currentTime || 0) / dur) * 1000));
}

function audioSrcChanged(src) {
  if (!src) return !!audio.src;
  const next = new URL(src, location.href).href;
  return audio.src !== next;
}

function getTrackDurationMs() {
  if (audio.duration && isFinite(audio.duration) && audio.duration > 0) {
    return audio.duration * 1000;
  }
  const durationMs = Number((state.lastTrack || {}).duration_ms || 0);
  return durationMs > 0 ? durationMs : 0;
}

function getRoomPositionMs(pb = state.lastPb) {
  if (!pb) return 0;
  let pos = Number(pb.position_ms || 0);
  const durationMs = getTrackDurationMs();
  if (durationMs > 0 && pos > durationMs + 5000) {
    return 0;
  }
  if (pb.is_playing && pb.updated_at) {
    const updatedAt = parsePlaybackTime(pb.updated_at);
    if (Number.isFinite(updatedAt)) {
      pos += Math.max(0, Date.now() - updatedAt);
    }
  }
  return durationMs > 0 ? Math.min(pos, durationMs) : Math.max(0, pos);
}

function parsePlaybackTime(value) {
  if (!value) return NaN;
  let s = String(value);
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += "Z";
  return new Date(s).getTime();
}

function getSafeRoomPositionMs(pb = state.lastPb) {
  const pos = getRoomPositionMs(pb);
  const durationMs = getTrackDurationMs();
  if (!durationMs || durationMs <= 1000) return Math.max(0, pos);
  return Math.max(0, Math.min(pos, durationMs - 1000));
}

function updateProgressFromRoom(force = false) {
  const bar = document.getElementById("progress");
  if (!bar || document.activeElement === bar) return;
  const durationMs = getTrackDurationMs();
  if (!durationMs) {
    if (force) bar.value = "0";
    return;
  }
  bar.value = String(Math.floor((getRoomPositionMs() / durationMs) * 1000));
}

function updateProgressTimer() {
  if (state.progressTimer) {
    clearInterval(state.progressTimer);
    state.progressTimer = null;
  }
  if (state.lastPb && state.lastPb.current_queue_item_id && state.lastTrack && state.lastTrack.duration_ms) {
    state.progressTimer = setInterval(() => {
      if (!state.playEnabled || audio.paused || !audio.src) updateProgressFromRoom();
      updateTimeDisplay();
    }, 500);
  }
}

function getDesiredPositionMs() {
  const bar = document.getElementById("progress");
  const dur = getTrackDurationMs();
  if (!bar || !dur || !isFinite(dur) || dur <= 0) return 0;
  const ratio = Number(bar.value || 0) / 1000;
  return Math.max(0, Math.floor(ratio * dur));
}

function getCurrentPositionMs() {
  if (state.playEnabled && audio.duration && isFinite(audio.duration) && audio.duration > 0 && !audio.paused) {
    return (audio.currentTime || 0) * 1000;
  }
  return getRoomPositionMs();
}

function formatTime(ms) {
  if (!ms || !isFinite(ms) || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

function updateTimeDisplay() {
  const curEl = document.getElementById("timeCurrent");
  const totEl = document.getElementById("timeTotal");
  if (!curEl || !totEl) return;
  const bar = document.getElementById("progress");
  const durationMs = getTrackDurationMs();
  let currentMs;
  if (document.activeElement === bar && durationMs) {
    currentMs = getDesiredPositionMs();
  } else {
    currentMs = getCurrentPositionMs();
  }
  curEl.textContent = formatTime(currentMs);
  totEl.textContent = formatTime(durationMs);
}

function syncPlayEnabledUi() {
  document.querySelector(".switchLabel").textContent = state.playEnabled ? "可放歌" : "仅点歌";
  document.getElementById("modeSwitch").checked = !!state.playEnabled;
  document.getElementById("volume").disabled = !state.playEnabled;
  document.getElementById("muteBtn").disabled = !state.playEnabled;
  updateVolumeUi();
}

function updateVolumeUi() {
  const volume = Number.isFinite(state.volume) ? state.volume : 50;
  state.volume = Math.max(0, Math.min(100, volume));
  const slider = document.getElementById("volume");
  if (slider) slider.value = String(state.volume);
  try { audio.volume = state.volume / 100; } catch (_) {}
  const btn = document.getElementById("muteBtn");
  if (!btn) return;
  btn.innerHTML = state.volume > 0
    ? '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M16 9a4 4 0 0 1 0 6"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z"/><path d="M18 9l-4 6"/><path d="M14 9l4 6"/></svg>';
}

function syncLocalAudioToRoom() {
  if (!state.lastPb || !state.lastTrack || !state.lastTrack.audio_url) return;
  const pb = state.lastPb;
  const tr = state.lastTrack;
  if (audioSrcChanged(tr.audio_url)) audio.src = tr.audio_url;
  updateVolumeUi();
  const target = getSafeRoomPositionMs(pb) / 1000;
  if (isFinite(target) && target >= 0) {
    try { audio.currentTime = target; } catch (_) {}
  }
  if (pb.is_playing) {
    audio.play().catch(() => {});
    setPauseIcon(true);
  } else {
    audio.pause();
    setPauseIcon(false);
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

function stopPeriodicSync() {
  if (state.syncTimer) {
    clearInterval(state.syncTimer);
    state.syncTimer = null;
  }
  if (state.trendingSyncTimer) {
    clearInterval(state.trendingSyncTimer);
    state.trendingSyncTimer = null;
  }
}
