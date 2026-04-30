export function readStoredNumber(key: string, fallback: number, min = 0, max = 100): number {
  const value = Number(localStorage.getItem(key) ?? String(fallback));
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

export function readStoredBoolean(key: string, fallback = false): boolean {
  const raw = localStorage.getItem(key);
  if (raw == null) return fallback;
  return raw === "1" || raw === "true";
}

export function writeStoredBoolean(key: string, value: boolean): void {
  localStorage.setItem(key, value ? "1" : "0");
}
