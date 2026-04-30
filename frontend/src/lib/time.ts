export function parsePlaybackTime(value?: string | null): number {
  if (!value) return Number.NaN;
  let raw = String(value);
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(raw)) raw += "Z";
  return new Date(raw).getTime();
}

export function formatTime(ms?: number | null): string {
  if (!ms || !Number.isFinite(ms) || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function clamp(value: number, min: number, max: number): number {
  return Math.max(min, Math.min(max, value));
}
