import asyncio
import json
from collections import defaultdict
from datetime import datetime
from typing import Any, Optional

from fastapi.encoders import jsonable_encoder
from fastapi import WebSocket


class RoomHub:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._room_sockets: dict[int, set[WebSocket]] = defaultdict(set)
        self._ws_user: dict[WebSocket, int] = {}
        self._disconnected: dict[int, dict[int, datetime]] = defaultdict(dict)

    async def join(self, room_id: int, ws: WebSocket, user_id: Optional[int] = None) -> None:
        async with self._lock:
            self._room_sockets[room_id].add(ws)
            if user_id is not None:
                self._ws_user[ws] = user_id
                self._disconnected.get(room_id, {}).pop(user_id, None)

    async def leave(self, room_id: int, ws: WebSocket) -> None:
        async with self._lock:
            socks = self._room_sockets.get(room_id)
            if not socks:
                return
            socks.discard(ws)
            user_id = self._ws_user.pop(ws, None)
            if user_id is not None:
                has_other = any(self._ws_user.get(s) == user_id for s in socks)
                if not has_other:
                    self._disconnected[room_id][user_id] = datetime.utcnow()
            if not socks:
                self._room_sockets.pop(room_id, None)

    async def broadcast(self, room_id: int, event: dict[str, Any]) -> None:
        payload = json.dumps(jsonable_encoder(event), ensure_ascii=False)
        async with self._lock:
            sockets = list(self._room_sockets.get(room_id, set()))
        for ws in sockets:
            try:
                await ws.send_text(payload)
            except Exception:
                pass

    async def get_stale_users(self, timeout_seconds: int = 1800) -> list[tuple[int, int]]:
        now = datetime.utcnow()
        stale: list[tuple[int, int]] = []
        async with self._lock:
            for room_id, users in list(self._disconnected.items()):
                for user_id, disc_time in list(users.items()):
                    if (now - disc_time).total_seconds() > timeout_seconds:
                        stale.append((room_id, user_id))
        return stale

    async def clear_stale_user(self, room_id: int, user_id: int) -> None:
        async with self._lock:
            d = self._disconnected.get(room_id)
            if d:
                d.pop(user_id, None)
                if not d:
                    self._disconnected.pop(room_id, None)


hub = RoomHub()

