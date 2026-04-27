import { state } from './state.js';

export const BASE_PATH = (() => {
  const p = location.pathname || "/";
  return p === "/order-song" || p.startsWith("/order-song/") ? "/order-song" : "";
})();

export function withBase(path) {
  if (!BASE_PATH) return path;
  if (typeof path !== "string" || !path) return path;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(path)) return path; // http(s):// etc.
  if (path.startsWith(BASE_PATH + "/")) return path;
  if (path.startsWith("/")) return BASE_PATH + path;
  return BASE_PATH + "/" + path;
}

export async function api(path, options = {}) {
  const headers = options.headers || {};
  if (state.token) headers["Authorization"] = `Bearer ${state.token}`;
  if (options.json) {
    headers["Content-Type"] = "application/json";
    options.body = JSON.stringify(options.json);
  }
  const res = await fetch(withBase(path), { ...options, headers });
  if (!res.ok) {
    const txt = await res.text();
    const err = new Error(txt || `HTTP ${res.status}`);
    err.status = res.status;
    throw err;
  }
  const ct = res.headers.get("content-type") || "";
  if (ct.includes("application/json")) return await res.json();
  return await res.text();
}
