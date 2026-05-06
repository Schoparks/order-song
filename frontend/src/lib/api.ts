import type { PublicConfig } from "../types";

export interface AppConfig {
  trending: {
    limit: number;
  };
  client: {
    sync_interval_ms: number;
    trending_sync_interval_ms: number;
    room_check_interval_ms: number;
    rooms_refresh_interval_ms: number;
    search_history_limit: number;
  };
  audio_loudness: {
    enabled: boolean;
    ffmpeg_available: boolean;
  };
}

export const BASE_PATH = (() => {
  const p = location.pathname || "/";
  return p === "/order-song" || p.startsWith("/order-song/") ? "/order-song" : "";
})();

export function withBase(path: string): string {
  if (!BASE_PATH) return path;
  if (!path) return path;
  if (/^[a-zA-Z][a-zA-Z0-9+.-]*:\/\//.test(path)) return path;
  if (path.startsWith(BASE_PATH + "/")) return path;
  if (path.startsWith("/")) return BASE_PATH + path;
  return `${BASE_PATH}/${path}`;
}

export class ApiError extends Error {
  status: number;

  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export async function api<T>(path: string, options: RequestInit & { token?: string | null; json?: unknown } = {}): Promise<T> {
  const headers = new Headers(options.headers);
  if (options.token) headers.set("Authorization", `Bearer ${options.token}`);
  const init: RequestInit = { ...options, headers };
  if (options.json !== undefined) {
    headers.set("Content-Type", "application/json");
    init.body = JSON.stringify(options.json);
  }
  delete (init as RequestInit & { json?: unknown; token?: string | null }).json;
  delete (init as RequestInit & { json?: unknown; token?: string | null }).token;

  const res = await fetch(withBase(path), init);
  if (!res.ok) {
    const text = await res.text();
    let message = text || `HTTP ${res.status}`;
    try {
      const parsed = JSON.parse(text);
      message = parsed.detail || message;
    } catch {
      // Keep plain text.
    }
    throw new ApiError(message, res.status);
  }
  const contentType = res.headers.get("content-type") || "";
  if (contentType.includes("application/json")) return (await res.json()) as T;
  return (await res.text()) as T;
}

export const defaultConfig: AppConfig = {
  trending: { limit: 50 },
  client: {
    sync_interval_ms: 2000,
    trending_sync_interval_ms: 60000,
    room_check_interval_ms: 10000,
    rooms_refresh_interval_ms: 5000,
    search_history_limit: 30,
  },
  audio_loudness: {
    enabled: false,
    ffmpeg_available: false,
  },
};

export function mergeConfig(config?: PublicConfig): AppConfig {
  return {
    trending: {
      limit: Number(config?.trending?.limit || defaultConfig.trending.limit),
    },
    client: {
      ...defaultConfig.client,
      ...(config?.client || {}),
    },
    audio_loudness: {
      ...defaultConfig.audio_loudness,
      ...(config?.audio_loudness || {}),
    },
  };
}
