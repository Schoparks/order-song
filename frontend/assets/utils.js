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

/**
 * Diff-based list rendering. Updates a container by key, only adding/removing
 * changed items to avoid full-list flicker.
 * @param {HTMLElement} container
 * @param {Array} items - data items
 * @param {Function} keyFn - (item) => string unique key
 * @param {Function} buildFn - (item, index) => HTMLElement
 * @param {Object} [opts]
 * @param {HTMLElement[]} [opts.prepend] - elements to prepend (toolbars etc)
 */
export function reconcileList(container, items, keyFn, buildFn, opts = {}) {
  const prependEls = opts.prepend || [];
  const dataAttr = "data-list-key";
  const existingMap = new Map();
  for (const child of Array.from(container.children)) {
    const k = child.getAttribute(dataAttr);
    if (k) existingMap.set(k, child);
  }

  const newKeys = new Set(items.map(keyFn));
  for (const [k, el] of existingMap) {
    if (!newKeys.has(k)) {
      el.classList.add("item-exit");
      el.addEventListener("animationend", () => el.remove(), { once: true });
      setTimeout(() => { if (el.parentNode) el.remove(); }, 300);
      existingMap.delete(k);
    }
  }

  container.innerHTML = "";
  for (const el of prependEls) container.appendChild(el);

  items.forEach((item, i) => {
    const key = keyFn(item);
    let el = existingMap.get(key);
    if (el) {
      el.classList.remove("item-exit");
      container.appendChild(el);
    } else {
      el = buildFn(item, i);
      el.setAttribute(dataAttr, key);
      el.classList.add("item-enter");
      container.appendChild(el);
    }
  });
}
