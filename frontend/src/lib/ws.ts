import type { WsMessage } from "../types";

export const WS_HEARTBEAT_INTERVAL_MS = 25000;
export const WS_HEARTBEAT_TIMEOUT_MS = 55000;
export const WS_BUFFERED_CLOSE_THRESHOLD = 512 * 1024;

export function isPlaybackMessage(msg: WsMessage): msg is Extract<WsMessage, { type: "playback_updated" }> {
  return msg.type === "playback_updated" && !!(msg as { playback_state?: unknown }).playback_state;
}
