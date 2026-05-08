export const TOKEN_STORAGE_KEY = "token";
export const SESSION_USER_ID_KEY = "sessionUserId";
export const LEGACY_ROOM_ID_KEY = "roomId";

export function roomStorageKey(userId: number | string) {
  return `roomId:${userId}`;
}

export function numericStorageValue(key: string): number | null {
  const value = Number(localStorage.getItem(key) || "");
  return Number.isFinite(value) && value > 0 ? value : null;
}

export function readInitialRoomId(): number | null {
  const userId = numericStorageValue(SESSION_USER_ID_KEY);
  if (userId) return numericStorageValue(roomStorageKey(userId));
  return numericStorageValue(LEGACY_ROOM_ID_KEY);
}

export function writeRoomIdForUser(roomId: number | null, userId?: number | null) {
  localStorage.removeItem(LEGACY_ROOM_ID_KEY);
  if (!userId) return;
  const key = roomStorageKey(userId);
  if (roomId) localStorage.setItem(key, String(roomId));
  else localStorage.removeItem(key);
}
