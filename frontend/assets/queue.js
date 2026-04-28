import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, trackKey, reconcileList } from './utils.js';
import { rerenderSearchButtons, rerenderAllPlaylistButtons } from './buttons.js';

export function buildQueueRow(it, index = 0) {
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

function buildShuffleBar() {
  const bar = document.createElement("div");
  bar.className = "listToolbar";
  const btn = document.createElement("button");
  btn.className = "btn small";
  btn.textContent = "一键打乱";
  btn.addEventListener("click", async () => {
    if (!state.roomId) return;
    btn.disabled = true;
    try {
      await api(`/api/rooms/${state.roomId}/queue/shuffle`, { method: "POST" });
      await refreshQueue();
    } finally {
      btn.disabled = false;
    }
  });
  bar.appendChild(btn);
  return bar;
}

export function getQueueCount() {
  return state.queuedKeys ? state.queuedKeys.size : 0;
}

export function updateQueueCountBadge() {
  const count = getQueueCount();
  for (const el of document.querySelectorAll(".queueCount")) {
    el.textContent = count > 0 ? `${count}首` : "";
  }
}

export async function refreshQueue() {
  if (!state.roomId) return;
  const items = await api(`/api/rooms/${state.roomId}/queue`);
  state.queuedKeys = new Set(items.map((it) => `${it.track.source}:${it.track.source_track_id}`));
  const queuedOnly = items.filter((it) => it.status === "queued");
  for (const container of [document.getElementById("queueList"), document.getElementById("queueListMobile")]) {
    if (!container) continue;
    const prepend = queuedOnly.length > 1 ? [buildShuffleBar()] : [];
    reconcileList(
      container,
      items,
      (it) => String(it.id),
      (it, i) => buildQueueRow(it, i),
      { prepend }
    );
  }
  rerenderSearchButtons();
  rerenderAllPlaylistButtons();
  updateQueueCountBadge();
}

export function buildHistoryRow(it, index = 0) {
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

function buildHistoryEmptyRow() {
  const row = document.createElement("div");
  row.className = "item";
  row.innerHTML = `<div><div class="title">暂无</div><div class="meta">播完 / 切歌后会出现在这里</div></div>`;
  return row;
}

export async function refreshHistory() {
  if (!state.roomId) return;
  const items = await api(`/api/rooms/${state.roomId}/history`);
  const filtered = items.filter((it) => !state.queuedKeys.has(`${it.track.source}:${it.track.source_track_id}`));
  for (const container of [document.getElementById("historyList"), document.getElementById("historyListMobile")]) {
    if (!container) continue;
    if (!filtered.length) {
      reconcileList(
        container,
        [{ id: "__history_empty" }],
        (it) => it.id,
        () => buildHistoryEmptyRow()
      );
      continue;
    }
    reconcileList(
      container,
      filtered,
      (it) => String(it.id),
      (it, i) => buildHistoryRow(it, i)
    );
  }
}
