import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, trackKey, reconcileList } from './utils.js';
import { syncOrderButtonState, syncPlaylistButtonState, rerenderAllPlaylistButtons } from './buttons.js';
import { refreshQueue } from './queue.js';

let _currentPlaylistId = null;

export async function loadPlaylistData() {
  if (!state.token) return;
  const pls = await api("/api/playlists").catch(() => []);
  state.playlists = pls;

  const trackMap = await api("/api/playlists/track-map").catch(() => ({}));
  const newKeys = new Map();
  for (const [key, info] of Object.entries(trackMap)) {
    newKeys.set(key, info);
  }
  state.playlistKeys = newKeys;
}

export async function addToPlaylist(playlistId, t) {
  const result = await api(`/api/playlists/${playlistId}/items`, {
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
  const pl = state.playlists.find((p) => p.id === playlistId);
  state.playlistKeys.set(trackKey(t), {
    item_id: result.id,
    playlist_id: playlistId,
    playlist_name: pl ? pl.name : "",
  });
  rerenderAllPlaylistButtons();
}

export async function removeFromPlaylist(t) {
  const key = trackKey(t);
  const info = state.playlistKeys.get(key);
  if (!info) return;
  await api(`/api/playlists/${info.playlist_id}/items/${info.item_id}`, { method: "DELETE" });
  state.playlistKeys.delete(key);
  rerenderAllPlaylistButtons();
}

export function showPlaylistPicker(t, triggerBtn) {
  const key = trackKey(t);
  const existing = state.playlistKeys.get(key);
  const isInPlaylist = !!existing;

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `
    <div class="sectionTitle">${isInPlaylist ? "管理歌单收藏" : "添加到歌单"}</div>
    <div class="playlistPickerList list"></div>
    <div class="row" style="margin-top:12px">
      <input class="input js-new-name" placeholder="新歌单名称" />
      <button class="btn small js-create-pl">创建</button>
    </div>
    ${isInPlaylist ? '<div style="margin-top:12px"><button class="btn dangerWide js-remove">从歌单移除</button></div>' : ''}
    <div class="actions"><button class="btn small js-cancel">取消</button></div>
  `;

  const listEl = modal.querySelector(".playlistPickerList");

  function renderOptions() {
    listEl.innerHTML = "";
    if (!state.playlists.length) {
      listEl.innerHTML = `<div class="muted" style="padding:8px 0">暂无歌单，请先创建</div>`;
      return;
    }
    state.playlists.forEach((pl) => {
      const opt = document.createElement("div");
      opt.className = "playlistOption";
      if (existing && existing.playlist_id === pl.id) opt.classList.add("selected");
      opt.innerHTML = `<span>${escapeHtml(pl.name)}</span><span class="muted">${pl.item_count || 0}首</span>`;
      opt.addEventListener("click", async () => {
        if (isInPlaylist && existing.playlist_id === pl.id) return;
        try {
          if (isInPlaylist) {
            await api(`/api/playlists/${existing.playlist_id}/items/${existing.item_id}/move`, {
              method: "POST",
              json: { target_playlist_id: pl.id },
            });
            state.playlistKeys.set(key, { item_id: existing.item_id, playlist_id: pl.id, playlist_name: pl.name });
          } else {
            await addToPlaylist(pl.id, t);
          }
          overlay.remove();
          rerenderAllPlaylistButtons();
          if (triggerBtn) syncPlaylistButtonState(triggerBtn, t);
          await loadPlaylists();
        } catch (e) {
          console.error(e);
        }
      });
      listEl.appendChild(opt);
    });
  }

  renderOptions();

  modal.querySelector(".js-create-pl").addEventListener("click", async () => {
    const name = modal.querySelector(".js-new-name").value.trim();
    if (!name) return;
    const pl = await api("/api/playlists", { method: "POST", json: { name } });
    state.playlists.push(pl);
    modal.querySelector(".js-new-name").value = "";
    renderOptions();
  });

  if (isInPlaylist) {
    modal.querySelector(".js-remove").addEventListener("click", async () => {
      await removeFromPlaylist(t);
      overlay.remove();
      if (triggerBtn) syncPlaylistButtonState(triggerBtn, t);
      await loadPlaylists();
    });
  }

  modal.querySelector(".js-cancel").addEventListener("click", () => overlay.remove());
  overlay.addEventListener("click", (e) => { if (e.target === overlay) overlay.remove(); });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);
}

function renderPlaylistListView() {
  const el = document.getElementById("tabPlaylists");
  el.innerHTML = "";

  const header = document.createElement("div");
  header.className = "playlistHeader";
  header.innerHTML = `
    <div class="sectionTitle">我的歌单</div>
    <button class="btn small js-import-netease">导入网易云歌单</button>
    <button class="btn small js-create">创建歌单</button>
  `;

  header.querySelector(".js-create").addEventListener("click", () => {
    const name = prompt("请输入歌单名称");
    if (!name || !name.trim()) return;
    api("/api/playlists", { method: "POST", json: { name: name.trim() } }).then(() => loadPlaylists());
  });

  header.querySelector(".js-import-netease").addEventListener("click", () => {
    import('./netease_import.js').then((m) => m.showNeteaseImportModal());
  });

  el.appendChild(header);

  if (!state.playlists.length) {
    const empty = document.createElement("div");
    empty.className = "item";
    empty.innerHTML = `<div><div class="title">暂无歌单</div><div class="meta">点击上方按钮创建歌单</div></div>`;
    el.appendChild(empty);
    return;
  }

  state.playlists.forEach((pl) => {
    const row = document.createElement("div");
    row.className = "item";
    row.setAttribute("data-list-key", `pl-${pl.id}`);
    row.innerHTML = `
      <div>
        <div class="title">${escapeHtml(pl.name)}</div>
        <div class="meta">${pl.item_count || 0}首歌曲</div>
      </div>
      <div class="actions">
        <button class="btn small js-queue-all">一键点歌</button>
        <button class="btn small danger js-delete">删除</button>
        <button class="btn small js-enter">进入</button>
      </div>
    `;
    row.querySelector(".js-enter").addEventListener("click", () => {
      _currentPlaylistId = pl.id;
      renderPlaylistDetailView(pl.id, pl.name);
    });
    row.querySelector(".js-queue-all").addEventListener("click", async () => {
      if (!state.roomId) return;
      const btn = row.querySelector(".js-queue-all");
      btn.disabled = true;
      btn.textContent = "添加中…";
      try {
        const items = await api(`/api/playlists/${pl.id}/items`);
        for (const it of items) {
          const t = it.track;
          const k = trackKey(t);
          if (state.queuedKeys.has(k)) continue;
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
        btn.disabled = false;
        btn.textContent = "一键点歌";
      }
    });
    row.querySelector(".js-delete").addEventListener("click", async () => {
      if (!confirm(`确定删除歌单「${pl.name}」及其所有歌曲？`)) return;
      await api(`/api/playlists/${pl.id}`, { method: "DELETE" });
      await loadPlaylists();
    });
    el.appendChild(row);
  });
}

async function renderPlaylistDetailView(playlistId, playlistName) {
  const el = document.getElementById("tabPlaylists");
  el.innerHTML = "";

  const header = document.createElement("div");
  header.className = "playlistHeader";
  header.innerHTML = `
    <button class="backLink js-back">&larr; 返回歌单列表</button>
    <div class="sectionTitle" style="margin:0">${escapeHtml(playlistName)}</div>
    <button class="btn small js-rename">重命名</button>
  `;
  header.querySelector(".js-back").addEventListener("click", () => {
    _currentPlaylistId = null;
    loadPlaylists();
  });
  header.querySelector(".js-rename").addEventListener("click", async () => {
    const name = prompt("请输入新歌单名称", playlistName);
    if (!name || !name.trim()) return;
    await api(`/api/playlists/${playlistId}`, { method: "PATCH", json: { name: name.trim() } });
    await loadPlaylistData();
    renderPlaylistDetailView(playlistId, name.trim());
  });
  el.appendChild(header);

  const loading = document.createElement("div");
  loading.className = "muted";
  loading.textContent = "加载中…";
  el.appendChild(loading);

  try {
    const items = await api(`/api/playlists/${playlistId}/items`);
    loading.remove();

    if (!items.length) {
      const empty = document.createElement("div");
      empty.className = "item";
      empty.innerHTML = `<div><div class="title">暂无歌曲</div><div class="meta">搜索后可将歌曲加入此歌单</div></div>`;
      el.appendChild(empty);
      return;
    }

    const listContainer = document.createElement("div");
    listContainer.className = "list";
    el.appendChild(listContainer);

    items.forEach((it, idx) => {
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
          <button class="btn small js-manage">管理</button>
        </div>
      `;
      const orderBtn = row.querySelector(".js-order");
      orderBtn.setAttribute("data-track-key", trackKey(t));
      syncOrderButtonState(orderBtn, t);
      orderBtn.addEventListener("click", async () => {
        if (!state.roomId) return;
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
      row.querySelector(".js-manage").addEventListener("click", () => {
        showPlaylistPicker(t, null);
      });
      listContainer.appendChild(row);
    });
  } catch (e) {
    loading.textContent = "加载失败";
  }
}

export async function loadPlaylists() {
  if (!state.token) return;
  await loadPlaylistData();
  if (_currentPlaylistId) {
    const pl = state.playlists.find((p) => p.id === _currentPlaylistId);
    if (pl) {
      renderPlaylistDetailView(_currentPlaylistId, pl.name);
      return;
    }
    _currentPlaylistId = null;
  }
  renderPlaylistListView();
}

export async function togglePlaylistItem(t, btn) {
  showPlaylistPicker(t, btn);
}
