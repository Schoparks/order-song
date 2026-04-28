function readStoredNumber(key, fallback, min = 0, max = 100) {
  const value = Number(localStorage.getItem(key) ?? String(fallback));
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

export const NEXT_DEBOUNCE_MS = 2500;
export const SYNC_INTERVAL_MS = 5000;
export const TRENDING_SYNC_INTERVAL_MS = 60000;
export const SEEK_TOLERANCE_S = 10;

export const clientConfig = {
  trending: {
    limit: 50,
  },
  client: {
    sync_interval_ms: SYNC_INTERVAL_MS,
    trending_sync_interval_ms: TRENDING_SYNC_INTERVAL_MS,
    room_check_interval_ms: 15000,
    rooms_refresh_interval_ms: 5000,
    search_history_limit: 30,
  },
};

function readConfigNumber(value, fallback, min = 1) {
  const num = Number(value);
  if (!Number.isFinite(num)) return fallback;
  return Math.max(min, Math.floor(num));
}

export function applyClientConfig(config = {}) {
  const trending = config.trending || {};
  const client = config.client || {};
  clientConfig.trending.limit = readConfigNumber(trending.limit, clientConfig.trending.limit);
  clientConfig.client.sync_interval_ms = readConfigNumber(client.sync_interval_ms, clientConfig.client.sync_interval_ms);
  clientConfig.client.trending_sync_interval_ms = readConfigNumber(client.trending_sync_interval_ms, clientConfig.client.trending_sync_interval_ms);
  clientConfig.client.room_check_interval_ms = readConfigNumber(client.room_check_interval_ms, clientConfig.client.room_check_interval_ms);
  clientConfig.client.rooms_refresh_interval_ms = readConfigNumber(client.rooms_refresh_interval_ms, clientConfig.client.rooms_refresh_interval_ms);
  clientConfig.client.search_history_limit = readConfigNumber(client.search_history_limit, clientConfig.client.search_history_limit);
}

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
