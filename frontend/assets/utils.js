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
 * Diff-based list rendering. Updates a container by key, only touching rows
 * that are added, removed, moved, or changed. This keeps scroll position stable
 * and avoids flicker when refreshed data is unchanged.
 * @param {HTMLElement} container
 * @param {Array} items - data items
 * @param {Function} keyFn - (item) => string unique key
 * @param {Function} buildFn - (item, index) => HTMLElement
 * @param {Object} [opts]
 * @param {HTMLElement[]} [opts.prepend] - elements to prepend (toolbars etc)
 * @param {Function} [opts.signatureFn] - (item) => string row content signature
 */
export function reconcileList(container, items, keyFn, buildFn, opts = {}) {
  if (!container) return false;

  const dataAttr = "data-list-key";
  const signatureFn = opts.signatureFn || ((item) => JSON.stringify(item));
  const existingMap = new Map();

  function captureScrollPositions() {
    const positions = [];
    let el = container;
    while (el && el.nodeType === 1) {
      if (el.scrollTop || el.scrollLeft || el.scrollHeight > el.clientHeight || el.scrollWidth > el.clientWidth) {
        positions.push({ el, top: el.scrollTop, left: el.scrollLeft });
      }
      el = el.parentElement;
    }
    return positions;
  }

  function restoreScrollPositions(positions) {
    for (const pos of positions) {
      pos.el.scrollTop = pos.top;
      pos.el.scrollLeft = pos.left;
    }
  }

  function setManagedAttrs(el, key, signature) {
    el.setAttribute(dataAttr, key);
    el.dataset.listSig = signature;
  }

  function prependSignature(el) {
    return el.outerHTML;
  }

  function buildPrependEntries() {
    return (opts.prepend || []).map((el, i) => {
      const key = el.getAttribute(dataAttr) || `__prepend_${i}`;
      return {
        key,
        signature: prependSignature(el),
        build: () => el,
      };
    });
  }

  function buildItemEntries() {
    return items.map((item, i) => ({
      key: String(keyFn(item, i)),
      signature: signatureFn(item, i),
      build: () => buildFn(item, i),
    }));
  }

  for (const child of Array.from(container.children)) {
    const k = child.getAttribute(dataAttr);
    if (k) existingMap.set(k, child);
  }

  const entries = [...buildPrependEntries(), ...buildItemEntries()];
  const wantedKeys = new Set(entries.map((entry) => entry.key));
  const desiredEls = [];
  let changed = false;
  const scrollPositions = captureScrollPositions();

  for (const child of Array.from(container.children)) {
    const key = child.getAttribute(dataAttr);
    if (!key || !wantedKeys.has(key)) {
      changed = true;
      child.remove();
    }
  }

  for (const entry of entries) {
    const key = entry.key;
    const signature = entry.signature;
    let el = existingMap.get(key);
    if (!el) {
      el = entry.build();
      setManagedAttrs(el, key, signature);
      el.classList.add("item-enter");
      changed = true;
    } else if (el.dataset.listSig !== signature) {
      const newEl = entry.build();
      setManagedAttrs(newEl, key, signature);
      el.replaceWith(newEl);
      el = newEl;
      changed = true;
    }
    desiredEls.push(el);
  }

  const currentEls = Array.from(container.children);
  if (
    !changed &&
    currentEls.length === desiredEls.length &&
    currentEls.every((el, i) => el === desiredEls[i])
  ) {
    return false;
  }

  desiredEls.forEach((el, i) => {
    if (container.children[i] !== el) {
      container.insertBefore(el, container.children[i] || null);
      changed = true;
    }
  });
  restoreScrollPositions(scrollPositions);
  return changed;
}
