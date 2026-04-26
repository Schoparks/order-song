import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, trackKey } from './utils.js';
import { syncOrderButtonState, syncPlaylistButtonState, rerenderAllPlaylistButtons } from './buttons.js';
import { refreshQueue } from './queue.js';

export async function ensurePlaylist() {
  const pls = await api("/api/playlists");
  if (pls.length) return pls[0];
  return await api("/api/playlists", { method: "POST", json: { name: "我的歌单" } });
}

export async function togglePlaylistItem(t, btn) {
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

export function renderPlaylistView() {
  const el = document.getElementById("tabPlaylists");
  el.innerHTML = "";

  if (!state.playlistItems.length) {
    el.innerHTML = `<div class="item"><div><div class="title">暂无歌单</div><div class="meta">搜索后可加入歌单</div></div></div>`;
    return;
  }

  const toolbar = document.createElement("div");
  toolbar.className = "listToolbar";
  const queueAllBtn = document.createElement("button");
  queueAllBtn.className = "btn small";
  queueAllBtn.textContent = "全部点歌";
  queueAllBtn.addEventListener("click", async () => {
    ensureRoom();
    queueAllBtn.disabled = true;
    queueAllBtn.textContent = "添加中…";
    try {
      for (const it of state.playlistItems) {
        const t = it.track;
        const key = trackKey(t);
        if (state.queuedKeys.has(key)) continue;
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
      }
      await refreshQueue();
    } finally {
      queueAllBtn.disabled = false;
      queueAllBtn.textContent = "全部点歌";
    }
  });
  toolbar.appendChild(queueAllBtn);
  el.appendChild(toolbar);

  state.playlistItems.forEach((it, idx) => {
    const t = it.track;
    const row = document.createElement("div");
    row.className = "item";
    row.style.animationDelay = `${idx * 40}ms`;
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
  });
}

function ensureRoom() {
  if (!state.roomId) throw new Error("not in room");
  return state.roomId;
}

export async function loadPlaylists() {
  if (!state.token) return;
  await loadPlaylistData();
  renderPlaylistView();
}
