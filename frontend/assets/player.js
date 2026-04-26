import { state, NEXT_DEBOUNCE_MS, SEEK_TOLERANCE_S } from './state.js';
import { api } from './api.js';
import { formatTime, parsePlaybackTime } from './utils.js';

export const audio = new Audio();
audio.preload = "none";

export function debouncedNext() {
  if (!state.roomId) return;
  const now = Date.now();
  if (now - state.lastNextAt < NEXT_DEBOUNCE_MS) return;
  state.lastNextAt = now;
  api(`/api/rooms/${state.roomId}/controls/next`, { method: "POST" }).catch(() => {});
}

export function setPauseIcon(isPlaying) {
  document.getElementById("pauseBtn").innerHTML = isPlaying
    ? '<svg viewBox="0 0 24 24"><path d="M8 6v12"/><path d="M16 6v12"/></svg>'
    : '<svg viewBox="0 0 24 24"><path d="M8 5l12 7-12 7z"/></svg>';
}

export function updateVolumeUi() {
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

export function syncPlayEnabledUi() {
  document.querySelector(".switchLabel").textContent = state.playEnabled ? "可放歌" : "仅点歌";
  document.getElementById("modeSwitch").checked = !!state.playEnabled;
  document.getElementById("volume").disabled = !state.playEnabled;
  document.getElementById("muteBtn").disabled = !state.playEnabled;
  updateVolumeUi();
}

function buildNowMeta() {
  const tr = state.lastTrack;
  const ob = state.lastOrderedBy;
  const parts = [];
  if (tr) {
    parts.push(tr.artist || "-");
    parts.push(tr.source);
  }
  if (ob && ob.username) parts.push(ob.username + " 点播");
  return parts.join(" · ") || "-";
}

export function setNowPlaying(pb) {
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
  } else {
    document.getElementById("nowTitle").textContent = "播放中";
  }
  document.getElementById("nowMeta").textContent = buildNowMeta();
  syncPlayEnabledUi();
  document.getElementById("volume").value = String(state.volume);
  updateTimeDisplay();
}

export function audioSrcChanged(src) {
  if (!src) return !!audio.src;
  const next = new URL(src, location.href).href;
  return audio.src !== next;
}

export function getTrackDurationMs() {
  if (audio.duration && isFinite(audio.duration) && audio.duration > 0) {
    return audio.duration * 1000;
  }
  const durationMs = Number((state.lastTrack || {}).duration_ms || 0);
  return durationMs > 0 ? durationMs : 0;
}

export function getRoomPositionMs(pb = state.lastPb) {
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

export function getSafeRoomPositionMs(pb = state.lastPb) {
  const pos = getRoomPositionMs(pb);
  const durationMs = getTrackDurationMs();
  if (!durationMs || durationMs <= 1000) return Math.max(0, pos);
  return Math.max(0, Math.min(pos, durationMs - 1000));
}

export function getCurrentPositionMs() {
  if (state.playEnabled && audio.duration && isFinite(audio.duration) && audio.duration > 0 && !audio.paused) {
    return (audio.currentTime || 0) * 1000;
  }
  return getRoomPositionMs();
}

export function getDesiredPositionMs() {
  const bar = document.getElementById("progress");
  const dur = getTrackDurationMs();
  if (!bar || !dur || !isFinite(dur) || dur <= 0) return 0;
  const ratio = Number(bar.value || 0) / 1000;
  return Math.max(0, Math.floor(ratio * dur));
}

export function updateProgressFromAudio(force = false) {
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

export function updateProgressFromRoom(force = false) {
  const bar = document.getElementById("progress");
  if (!bar || document.activeElement === bar) return;
  const durationMs = getTrackDurationMs();
  if (!durationMs) {
    if (force) bar.value = "0";
    return;
  }
  bar.value = String(Math.floor((getRoomPositionMs() / durationMs) * 1000));
}

export function updateTimeDisplay() {
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

export function updateProgressTimer() {
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

export function forceProgressUpdate() {
  const bar = document.getElementById("progress");
  if (!bar || document.activeElement === bar) return;
  if (state.playEnabled && audio.duration && isFinite(audio.duration) && audio.duration > 0) {
    bar.value = String(Math.floor(((audio.currentTime || 0) / audio.duration) * 1000));
  } else {
    updateProgressFromRoom(true);
  }
  updateTimeDisplay();
}

export async function onPlaybackUpdated(pb, currentTrack, orderedBy) {
  state.lastPb = pb;
  state.lastTrack = currentTrack || (pb && pb.current_queue_item_id ? state.lastTrack : null);
  state.lastOrderedBy = orderedBy !== undefined ? orderedBy : (pb && pb.current_queue_item_id ? state.lastOrderedBy : null);
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
    document.getElementById("nowMeta").textContent = buildNowMeta();
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

export function syncLocalAudioToRoom() {
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

audio.addEventListener("ended", () => debouncedNext());
audio.addEventListener("timeupdate", () => {
  updateProgressFromAudio();
  updateTimeDisplay();
});
audio.addEventListener("loadedmetadata", () => {
  updateProgressFromAudio(true);
  updateTimeDisplay();
});
