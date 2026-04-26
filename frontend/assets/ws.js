import { state } from './state.js';
import { refreshQueue, refreshHistory } from './queue.js';
import { onPlaybackUpdated } from './player.js';
import { handleRoomGone } from './rooms.js';

export function connectWs() {
  if (!state.roomId) return;
  if (state.ws) {
    try { state.ws.close(); } catch (_) {}
  }
  state._wsBackoff = 0;
  const proto = location.protocol === "https:" ? "wss" : "ws";
  state.ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws.addEventListener("open", () => {
    state._wsBackoff = 0;
    state.ws.send(JSON.stringify({ type: "join_room", room_id: state.roomId, token: state.token }));
  });
  state.ws.addEventListener("message", async (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === "queue_updated") {
        await refreshQueue();
        await refreshHistory();
      }
      if (msg.type === "playback_updated") {
        await onPlaybackUpdated(msg.playback_state, msg.current_track, msg.ordered_by);
        await refreshQueue();
        await refreshHistory();
      }
      if (msg.type === "room_destroyed") {
        handleRoomGone();
      }
      if (msg.type === "room_member_left" && state.me && msg.user_id === state.me.id) {
        handleRoomGone();
      }
    } catch (_) {}
  });
  state.ws.addEventListener("close", () => {
    if (state.roomId) {
      const delay = Math.min(30000, 1000 * Math.pow(2, state._wsBackoff || 0));
      state._wsBackoff = (state._wsBackoff || 0) + 1;
      setTimeout(() => connectWs(), delay);
    }
  });
}
