import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, trackKey, reconcileList } from './utils.js';
import { syncOrderButtonState, syncPlaylistButtonState } from './buttons.js';
import { refreshQueue } from './queue.js';
import { togglePlaylistItem, showPlaylistPicker } from './playlist.js';

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

export function renderHistory() {
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

export function openSearchOverlay() {
  if (!state.roomId) return;
  document.getElementById("searchInput2").value = document.getElementById("searchInput").value || "";
  document.getElementById("viewSearch").classList.remove("hidden");
  renderHistory();
  document.getElementById("historyWrap").classList.remove("hidden");
  document.getElementById("searchResults2").innerHTML = "";
  document.getElementById("searchInput2").focus();
}

export function closeSearchOverlay() {
  document.getElementById("viewSearch").classList.add("hidden");
}

function buildSearchRow(t, idx) {
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
  favBtn.addEventListener("click", () => showPlaylistPicker(t, favBtn));
  return el;
}

export async function runSearch() {
  const q = document.getElementById("searchInput2").value.trim();
  if (!q) return;
  const hist = [q, ...getHistory().filter((x) => x !== q)];
  saveHistory(hist);
  document.getElementById("historyWrap").classList.add("hidden");
  const resultsEl = document.getElementById("searchResults2");
  resultsEl.innerHTML = "";
  try {
    const items = await api(`/api/search?q=${encodeURIComponent(q)}`);
    items.forEach((t, idx) => {
      resultsEl.appendChild(buildSearchRow(t, idx));
    });
  } catch (e) {
    resultsEl.innerHTML = `<div class="item"><div><div class="title">搜索失败</div><div class="meta">${escapeHtml(e.message)}</div></div></div>`;
  }
}

function buildTrendingRow(it, idx) {
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
    if (!state.roomId) throw new Error("not in room");
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
  favBtn.addEventListener("click", () => showPlaylistPicker(t, favBtn));
  return row;
}

export async function loadTrending() {
  if (!state.token) return;
  const items = await api("/api/trending?limit=20");
  const el = document.getElementById("tabTrending");
  reconcileList(
    el,
    items,
    (it) => `${it.track.source}:${it.track.source_track_id}`,
    (it, idx) => buildTrendingRow(it, idx)
  );
}
