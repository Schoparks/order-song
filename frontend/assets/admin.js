import { state } from './state.js';
import { api } from './api.js';
import { escapeHtml, reconcileList } from './utils.js';
import { showView, setChromeVisible } from './ui.js';

let _adminToken = null;

const BASE_PATH = (() => {
  const p = location.pathname || "/";
  return p === "/order-song" || p.startsWith("/order-song/") ? "/order-song" : "";
})();

function withBase(path) {
  if (!BASE_PATH) return path;
  if (typeof path !== "string" || !path) return path;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(path)) return path;
  if (path.startsWith(BASE_PATH + "/")) return path;
  if (path.startsWith("/")) return BASE_PATH + path;
  return BASE_PATH + "/" + path;
}

function adminApi(path, options = {}) {
  const headers = options.headers || {};
  if (_adminToken) headers["Authorization"] = `Bearer ${_adminToken}`;
  if (options.json) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  return fetch(withBase(path), { ...options, headers }).then(async (res) => {
    if (!res.ok) {
      const txt = await res.text();
      const err = new Error(txt || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    const ct = res.headers.get("content-type") || "";
    if (ct.includes("application/json")) return res.json();
    return res.text();
  });
}

export async function adminLogin(username, password) {
  const out = await api("/api/auth/admin-login", {
    method: "POST",
    json: { username, password },
  });
  _adminToken = out.token;
  showAdminPanel();
}

export function showAdminPanel() {
  showView("viewAdmin");
  setChromeVisible(false);
  setupAdminTabs();
  loadAdminUsers();
}

function setupAdminTabs() {
  document.querySelectorAll(".tab[data-admintab]").forEach((btn) => {
    btn.onclick = () => {
      document.querySelectorAll(".tab[data-admintab]").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      const tab = btn.getAttribute("data-admintab");
      document.getElementById("adminTabUsers").classList.toggle("hidden", tab !== "users");
      document.getElementById("adminTabRooms").classList.toggle("hidden", tab !== "rooms");
      if (tab === "users") loadAdminUsers();
      if (tab === "rooms") loadAdminRooms();
    };
  });
}

async function loadAdminUsers() {
  const el = document.getElementById("adminTabUsers");
  el.innerHTML = `<div class="muted">加载中…</div>`;
  try {
    const users = await adminApi("/api/admin/users");
    el.innerHTML = "";
    const list = document.createElement("div");
    list.className = "list";
    users.forEach((u) => {
      const row = document.createElement("div");
      row.className = "item";
      row.innerHTML = `
        <div>
          <div class="title">${escapeHtml(u.username)} ${u.is_admin ? '<span style="color:var(--accent);font-size:12px">[管理员]</span>' : ''}</div>
          <div class="meta">ID: ${u.id} · 创建于 ${new Date(u.created_at).toLocaleString()}</div>
        </div>
        <div class="actions">
          <button class="btn small js-toggle-admin">${u.is_admin ? '取消管理员' : '设为管理员'}</button>
          <button class="btn small danger js-delete">删除</button>
        </div>
      `;
      row.querySelector(".js-toggle-admin").addEventListener("click", async () => {
        await adminApi(`/api/admin/users/${u.id}`, {
          method: "PATCH",
          json: { is_admin: !u.is_admin },
        });
        await loadAdminUsers();
      });
      row.querySelector(".js-delete").addEventListener("click", async () => {
        if (!confirm(`确定删除用户「${u.username}」？此操作不可恢复。`)) return;
        try {
          await adminApi(`/api/admin/users/${u.id}`, { method: "DELETE" });
          await loadAdminUsers();
        } catch (e) {
          let msg = "删除失败";
          try { msg = JSON.parse(e.message).detail || msg; } catch (_) { msg = e.message; }
          alert(msg);
        }
      });
      list.appendChild(row);
    });
    el.appendChild(list);
  } catch (e) {
    el.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

async function loadAdminRooms() {
  const el = document.getElementById("adminTabRooms");
  el.innerHTML = `<div class="muted">加载中…</div>`;
  try {
    const rooms = await adminApi("/api/admin/rooms");
    el.innerHTML = "";
    if (!rooms.length) {
      el.innerHTML = `<div class="muted">暂无房间</div>`;
      return;
    }
    const list = document.createElement("div");
    list.className = "list";
    rooms.forEach((r) => {
      const row = document.createElement("div");
      row.className = "item";
      row.style.flexDirection = "column";
      row.style.alignItems = "stretch";
      row.style.gap = "8px";

      const memberHtml = r.members.map((m) =>
        `<span class="chip" style="display:inline-flex;align-items:center;gap:4px">
          ${escapeHtml(m.username)}
          <button class="js-kick" data-uid="${m.id}" data-uname="${escapeHtml(m.username)}" style="background:none;border:none;color:var(--danger);cursor:pointer;font-size:14px;padding:0 2px" title="移除成员">&times;</button>
        </span>`
      ).join(" ");

      row.innerHTML = `
        <div style="display:flex;justify-content:space-between;align-items:center">
          <div>
            <div class="title">${escapeHtml(r.name)}</div>
            <div class="meta">创建者: ${escapeHtml(r.created_by)} · ${r.members.length}人 · ${new Date(r.created_at).toLocaleString()}</div>
          </div>
          <div class="actions">
            <button class="btn small danger js-delete-room">删除房间</button>
          </div>
        </div>
        <div style="display:flex;flex-wrap:wrap;gap:4px">${memberHtml || '<span class="muted">无成员</span>'}</div>
      `;

      row.querySelector(".js-delete-room").addEventListener("click", async () => {
        if (!confirm(`确定删除房间「${r.name}」？`)) return;
        await adminApi(`/api/admin/rooms/${r.id}`, { method: "DELETE" });
        await loadAdminRooms();
      });

      row.querySelectorAll(".js-kick").forEach((btn) => {
        btn.addEventListener("click", async (e) => {
          e.stopPropagation();
          const uid = Number(btn.dataset.uid);
          const uname = btn.dataset.uname;
          if (!confirm(`确定将「${uname}」移出房间「${r.name}」？`)) return;
          await adminApi(`/api/admin/rooms/${r.id}/members/${uid}`, { method: "DELETE" });
          await loadAdminRooms();
        });
      });

      list.appendChild(row);
    });
    el.appendChild(list);
  } catch (e) {
    el.innerHTML = `<div class="muted">加载失败：${escapeHtml(e.message)}</div>`;
  }
}

export function exitAdmin() {
  _adminToken = null;
  showView("viewAuth");
}

export function initAdmin() {
  document.getElementById("btnAdminLogout").addEventListener("click", () => exitAdmin());
}
