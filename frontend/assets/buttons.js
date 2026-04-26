import { state } from './state.js';
import { trackKey } from './utils.js';

export function syncOrderButtonState(btn, t) {
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

export function rerenderSearchButtons() {
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

export function syncPlaylistButtonState(btn, t) {
  const key = trackKey(t);
  const info = state.playlistKeys.get(key);
  if (info) {
    btn.textContent = "已添加";
    btn.classList.add("subtle");
  } else {
    btn.textContent = "加入歌单";
    btn.classList.remove("subtle");
  }
}

export function rerenderAllPlaylistButtons() {
  document.querySelectorAll(".js-fav[data-track-key]").forEach((btn) => {
    const key = btn.getAttribute("data-track-key");
    if (!key) return;
    const info = state.playlistKeys.get(key);
    if (info) {
      btn.textContent = "已添加";
      btn.classList.add("subtle");
    } else {
      btn.textContent = "加入歌单";
      btn.classList.remove("subtle");
    }
  });
}
