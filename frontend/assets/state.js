function readStoredNumber(key, fallback, min = 0, max = 100) {
  const value = Number(localStorage.getItem(key) ?? String(fallback));
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

export const NEXT_DEBOUNCE_MS = 2500;
export const SYNC_INTERVAL_MS = 5000;
export const TRENDING_SYNC_INTERVAL_MS = 60000;
export const SEEK_TOLERANCE_S = 10;

export const state = {
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
  lastOrderedBy: null,
  suppressNextSeek: false,
  progressTimer: null,
  lastNextAt: 0,
  playlistKeys: new Map(),
  playlistItems: [],
  defaultPlaylistId: null,
  syncTimer: null,
  trendingSyncTimer: null,
  roomsRefreshTimer: null,
  roomCheckTimer: null,
  playlists: [],
};

export function stopPeriodicSync() {
  if (state.syncTimer) {
    clearInterval(state.syncTimer);
    state.syncTimer = null;
  }
  if (state.trendingSyncTimer) {
    clearInterval(state.trendingSyncTimer);
    state.trendingSyncTimer = null;
  }
  if (state.roomCheckTimer) {
    clearInterval(state.roomCheckTimer);
    state.roomCheckTimer = null;
  }
}
