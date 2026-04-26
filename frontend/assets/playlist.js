import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, trackKey, reconcileList } from './utils.js';
import { syncOrderButtonState, syncPlaylistButtonState, rerenderAllPlaylistButtons } from './buttons.js';
import { refreshQueue } from './queue.js';

let _currentPlaylistId = null;

function openBaseModal(innerHtml) {
  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = innerHtml;

  function close() {
    overlay.remove();
  }

  overlay.addEventListener("click", (e) => {
    if (e.target === overlay) close();
  });

  overlay.appendChild(modal);
  document.body.appendChild(overlay);

  return { overlay, modal, close };
}

function openTextInputModal({
  title,
  initialValue = "",
  placeholder = "",
  confirmText = "确定",
  confirmBusyText = "保存中…",
  onConfirm,
}) {
  const { modal, close } = openBaseModal(`
    <div class="sectionTitle">${escapeHtml(title)}</div>
    <div class="row">
      <input class="input js-value" placeholder="${escapeHtml(placeholder)}" />
    </div>
    <div class="actions">
      <button class="btn small js-cancel">取消</button>
      <button class="btn small js-confirm">${escapeHtml(confirmText)}</button>
    </div>
  `);

  const input = modal.querySelector(".js-value");
  input.value = initialValue || "";
  input.focus();
  input.select?.();

  const cancelBtn = modal.querySelector(".js-cancel");
  const confirmBtn = modal.querySelector(".js-confirm");

  cancelBtn.addEventListener("click", () => close());

  async function doConfirm() {
    const value = input.value.trim();
    if (!value) return;
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    const oldText = confirmBtn.textContent;
    confirmBtn.textContent = confirmBusyText;
    try {
      await onConfirm(value);
      close();
    } catch (e) {
      console.error(e);
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      confirmBtn.textContent = oldText;
    }
  }

  confirmBtn.addEventListener("click", doConfirm);
  input.addEventListener("keydown", (e) => {
    if (e.key === "Enter") doConfirm();
  });
}

function openConfirmModal({
  title,
  bodyHtml = "",
  confirmText = "确定",
  confirmBusyText = "处理中…",
  confirmBtnClass = "btn small",
  onConfirm,
}) {
  const { modal, close } = openBaseModal(`
    <div class="sectionTitle">${escapeHtml(title)}</div>
    <div class="js-body">${bodyHtml}</div>
    <div class="actions">
      <button class="btn small js-cancel">取消</button>
      <button class="${confirmBtnClass} js-confirm">${escapeHtml(confirmText)}</button>
    </div>
  `);

  const cancelBtn = modal.querySelector(".js-cancel");
  const confirmBtn = modal.querySelector(".js-confirm");

  cancelBtn.addEventListener("click", () => close());

  confirmBtn.addEventListener("click", async () => {
    confirmBtn.disabled = true;
    cancelBtn.disabled = true;
    const oldText = confirmBtn.textContent;
    confirmBtn.textContent = confirmBusyText;
    try {
      await onConfirm();
      close();
    } catch (e) {
      console.error(e);
      confirmBtn.disabled = false;
      cancelBtn.disabled = false;
      confirmBtn.textContent = oldText;
    }
  });
}

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
  rerenderAllPlaylistButtons();
}

export async function addToPlaylist(playlistId, t) {
  const key = trackKey(t);
  const arr = state.playlistKeys.get(key) || [];
  if (arr.some((x) => x.playlist_id === playlistId)) return;
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
  arr.push({ item_id: result.id, playlist_id: playlistId, playlist_name: pl ? pl.name : "" });
  state.playlistKeys.set(key, arr);
  rerenderAllPlaylistButtons();
}

export async function removeFromPlaylist(t, playlistId) {
  const key = trackKey(t);
  const arr = state.playlistKeys.get(key) || [];
  const entry = playlistId != null
    ? arr.find((x) => x.playlist_id === playlistId)
    : arr[0];
  if (!entry) return;
  await api(`/api/playlists/${entry.playlist_id}/items/${entry.item_id}`, { method: "DELETE" });
  const newArr = arr.filter((x) => x !== entry);
  if (newArr.length === 0) state.playlistKeys.delete(key);
  else state.playlistKeys.set(key, newArr);
  rerenderAllPlaylistButtons();
}

export async function showPlaylistPicker(t, triggerBtn) {
  await loadPlaylistData();
  const key = trackKey(t);
  const existing = state.playlistKeys.get(key) || [];
  const originalPlIds = new Set(existing.map((x) => x.playlist_id));

  const overlay = document.createElement("div");
  overlay.className = "modal-overlay";

  const modal = document.createElement("div");
  modal.className = "modal";
  modal.innerHTML = `
    <div class="sectionTitle">${existing.length ? "在歌单中" : "添加到歌单"}</div>
    <div class="playlistPickerList list"></div>
    <div class="row" style="margin-top:12px">
      <input class="input js-new-name" placeholder="新歌单名称" />
      <button class="btn small js-create-pl">创建</button>
    </div>
    <div class="actions">
      <button class="btn small js-cancel">取消</button>
      <button class="btn small js-confirm">完成</button>
    </div>
  `;

  const listEl = modal.querySelector(".playlistPickerList");

  function renderOptions() {
    listEl.innerHTML = "";
    if (!state.playlists.length) {
      listEl.innerHTML = `<div class="muted" style="padding:8px 0">暂无歌单，请先创建</div>`;
      return;
    }
    state.playlists.forEach((pl) => {
      const opt = document.createElement("label");
      opt.className = "playlistOption";
      const checked = originalPlIds.has(pl.id);
      if (checked) opt.classList.add("selected");
      opt.innerHTML = `
        <input type="checkbox" class="playlistCheck" data-pl-id="${pl.id}" ${checked ? "checked" : ""} />
        <span class="playlistOptName">${escapeHtml(pl.name)}</span>
        <span class="muted">${pl.item_count || 0}首</span>
      `;
      opt.querySelector("input").addEventListener("change", (e) => {
        opt.classList.toggle("selected", e.target.checked);
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
    const newCb = listEl.querySelector(`input[data-pl-id="${pl.id}"]`);
    if (newCb) {
      newCb.checked = true;
      newCb.closest(".playlistOption").classList.add("selected");
    }
  });

  modal.querySelector(".js-confirm").addEventListener("click", async () => {
    const confirmBtn = modal.querySelector(".js-confirm");
    confirmBtn.disabled = true;
    confirmBtn.textContent = "保存中…";
    try {
      const checkedIds = new Set();
      listEl.querySelectorAll(".playlistCheck").forEach((cb) => {
        if (cb.checked) checkedIds.add(Number(cb.dataset.plId));
      });
      for (const plId of checkedIds) {
        if (!originalPlIds.has(plId)) await addToPlaylist(plId, t);
      }
      for (const plId of originalPlIds) {
        if (!checkedIds.has(plId)) await removeFromPlaylist(t, plId);
      }
      overlay.remove();
      rerenderAllPlaylistButtons();
      if (triggerBtn) syncPlaylistButtonState(triggerBtn, t);
      await loadPlaylists();
    } catch (e) {
      console.error(e);
      confirmBtn.disabled = false;
      confirmBtn.textContent = "完成";
    }
  });

  modal.querySelector(".js-cancel").addEventListener("click", () => overlay.remove());

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
    openTextInputModal({
      title: "创建歌单",
      placeholder: "歌单名称",
      confirmText: "创建",
      confirmBusyText: "创建中…",
      onConfirm: async (name) => {
        await api("/api/playlists", { method: "POST", json: { name } });
        await loadPlaylists();
      },
    });
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
      openConfirmModal({
        title: "删除歌单？",
        bodyHtml: `
          <div class="muted" style="margin-top:-4px">
            将删除歌单「<b>${escapeHtml(pl.name)}</b>」及其所有歌曲，且不可恢复。
          </div>
        `,
        confirmText: "删除",
        confirmBusyText: "删除中…",
        confirmBtnClass: "btn small danger",
        onConfirm: async () => {
          await api(`/api/playlists/${pl.id}`, { method: "DELETE" });
          await loadPlaylists();
        },
      });
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
    <button class="iconBtn small js-back" title="返回歌单列表" aria-label="返回歌单列表">
      <svg viewBox="0 0 24 24" aria-hidden="true">
        <path d="M15 18l-6-6 6-6"></path>
      </svg>
    </button>
    <div class="sectionTitle" style="margin:0">${escapeHtml(playlistName)}</div>
    <button class="btn small js-rename">重命名</button>
  `;
  header.querySelector(".js-back").addEventListener("click", () => {
    _currentPlaylistId = null;
    loadPlaylists();
  });
  header.querySelector(".js-rename").addEventListener("click", async () => {
    openTextInputModal({
      title: "重命名歌单",
      initialValue: playlistName,
      placeholder: "歌单名称",
      confirmText: "保存",
      confirmBusyText: "保存中…",
      onConfirm: async (name) => {
        await api(`/api/playlists/${playlistId}`, { method: "PATCH", json: { name } });
        await loadPlaylistData();
        renderPlaylistDetailView(playlistId, name);
      },
    });
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
          <button class="btn small js-manage">在歌单中</button>
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
