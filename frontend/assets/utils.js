export function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;" }[c]));
}

export function formatTime(ms) {
  if (!ms || !isFinite(ms) || ms < 0) return "0:00";
  const totalSeconds = Math.floor(ms / 1000);
  const minutes = Math.floor(totalSeconds / 60);
  const seconds = totalSeconds % 60;
  return `${minutes}:${seconds.toString().padStart(2, "0")}`;
}

export function trackKey(t) {
  return `${t.source}:${t.source_track_id}`;
}

export function parsePlaybackTime(value) {
  if (!value) return NaN;
  let s = String(value);
  if (!/[zZ]|[+-]\d\d:?\d\d$/.test(s)) s += "Z";
  return new Date(s).getTime();
}
