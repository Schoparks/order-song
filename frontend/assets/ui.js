import { state } from './state.js';

export function showView(id) {
  document.querySelectorAll(".view").forEach((v) => v.classList.add("hidden"));
  document.getElementById(id).classList.remove("hidden");
}

export function setChromeVisible(isInRoom) {
  document.getElementById("topbar").classList.toggle("hidden", !isInRoom);
  document.getElementById("playerBar").classList.toggle("hidden", !isInRoom);
}

export function setUserLabel(username = null) {
  const btn = document.getElementById("userButton");
  btn.textContent = username || (state.token ? "账号" : "登录");
}

export function toggleUserMenu(forceClose = false) {
  const menu = document.getElementById("userMenu");
  const btn = document.getElementById("userButton");
  if (forceClose) {
    menu.classList.add("hidden");
    return;
  }
  const willOpen = menu.classList.contains("hidden");
  if (!willOpen) {
    menu.classList.add("hidden");
    return;
  }
  const r = btn.getBoundingClientRect();
  menu.style.left = `${Math.max(8, r.left)}px`;
  menu.style.top = `${r.bottom + 8}px`;
  menu.classList.remove("hidden");
  document.getElementById("userMenuHint").textContent = "";
  document.getElementById("renameBox").classList.add("hidden");
  document.getElementById("passwordBox").classList.add("hidden");
}
