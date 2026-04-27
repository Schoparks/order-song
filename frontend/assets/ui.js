import { state } from './state.js';

const SCROLL_TOP_THRESHOLD = 180;
const SCROLL_TOP_BUTTON_SIZE = 44;
const SCROLL_TOP_BUTTON_MARGIN = 18;
const SCROLL_TOP_TARGETS = [
  "searchResults2",
  "tabTrending",
  "tabPlaylists",
  "rightTabQueue",
  "rightTabHistory",
  "mobileTabQueue",
  "mobileTabHistory",
  "roomList",
];

function getScrollTopHost(el) {
  return el.closest(".rightPane, .searchOverlay, #viewRooms, .leftPane > .block, #tabQueue") || el;
}

function positionScrollTopButton(btn, host) {
  const rect = host.getBoundingClientRect();
  const size = SCROLL_TOP_BUTTON_SIZE;
  const margin = SCROLL_TOP_BUTTON_MARGIN;
  const maxLeft = Math.max(margin, window.innerWidth - size - margin);
  const minLeft = Math.max(margin, rect.left + margin);
  const desiredLeft = rect.right - size - margin;
  const left = Math.min(Math.max(desiredLeft, minLeft), maxLeft);
  const maxTop = Math.max(margin, window.innerHeight - size - margin);
  const minTop = Math.max(margin, rect.top + margin);
  const desiredTop = rect.bottom - size - margin;
  const top = Math.min(Math.max(desiredTop, minTop), maxTop);

  btn.style.left = `${Math.round(left)}px`;
  btn.style.top = `${Math.round(top)}px`;
}

export function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.getElementById(id).classList.remove("hidden");
}

export function setChromeVisible(isInRoom) {
  document.getElementById("topbar").classList.toggle("hidden", !isInRoom);
  document.getElementById("playerBar").classList.toggle("hidden", !isInRoom);
}

export function setUserLabel(username = null) {
  const label = username || (state.token ? "账号" : "登录");
  document.getElementById("userButton").textContent = label;
  const roomBtn = document.getElementById("roomUserButton");
  if (roomBtn) roomBtn.textContent = label;
}

function makeScrollTopButton() {
  const btn = document.createElement("button");
  btn.type = "button";
  btn.className = "iconBtn small scrollTopBtn";
  btn.title = "回到顶部";
  btn.setAttribute("aria-label", "回到顶部");
  btn.innerHTML = `
    <svg viewBox="0 0 24 24" aria-hidden="true">
      <path d="M18 15l-6-6-6 6"></path>
    </svg>
  `;
  return btn;
}

export function initScrollTopButtons() {
  SCROLL_TOP_TARGETS.forEach((id) => {
    const el = document.getElementById(id);
    if (!el || el.dataset.scrollTopReady === "1") return;

    el.dataset.scrollTopReady = "1";
    el.classList.add("scrollTopScope");

    const host = getScrollTopHost(el);

    const btn = makeScrollTopButton();
    const sync = () => {
      const isVisible = !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
      const shouldShow = isVisible && el.scrollHeight > el.clientHeight + 1 && el.scrollTop > SCROLL_TOP_THRESHOLD;
      if (shouldShow) positionScrollTopButton(btn, host);
      btn.classList.toggle("visible", shouldShow);
    };

    btn.addEventListener("click", () => {
      el.scrollTo({ top: 0, behavior: "smooth" });
    });
    el.addEventListener("scroll", sync, { passive: true });
    window.addEventListener("resize", sync, { passive: true });
    document.addEventListener("click", () => requestAnimationFrame(sync), { passive: true });

    document.body.appendChild(btn);
    requestAnimationFrame(sync);
  });
}

export function toggleUserMenu(forceClose = false, anchorEl = null) {
  const menu = document.getElementById("userMenu");
  if (forceClose) {
    menu.classList.add("hidden");
    return;
  }
  const willOpen = menu.classList.contains("hidden");
  if (!willOpen) {
    menu.classList.add("hidden");
    return;
  }
  const btn = anchorEl || document.getElementById("userButton");
  const r = btn.getBoundingClientRect();
  menu.classList.remove("hidden");
  menu.style.visibility = "hidden";

  const margin = 8;
  const menuWidth = menu.offsetWidth;
  const menuHeight = menu.offsetHeight;
  const maxLeft = Math.max(margin, window.innerWidth - menuWidth - margin);
  const maxTop = Math.max(margin, window.innerHeight - menuHeight - margin);
  const left = Math.min(Math.max(margin, r.left), maxLeft);
  let top = r.bottom + margin;
  if (top > maxTop && r.top - menuHeight - margin >= margin) {
    top = r.top - menuHeight - margin;
  }

  menu.style.left = `${left}px`;
  menu.style.top = `${Math.min(Math.max(margin, top), maxTop)}px`;
  menu.style.visibility = "";
  document.getElementById("userMenuHint").textContent = "";
  document.getElementById("renameBox").classList.add("hidden");
  document.getElementById("passwordBox").classList.add("hidden");
}
