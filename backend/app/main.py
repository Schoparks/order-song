import asyncio
import json
from pathlib import Path

from fastapi import FastAPI
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session

from app.core.config import settings
from app.core.security import decode_token
from app.db import engine, init_db
from app.routers.auth import router as auth_router
from app.routers.rooms import router as rooms_router, remove_member_from_room
from app.routers.search import router as search_router
from app.routers.queue_playback import router as queue_router
from app.routers.playlists_trending import router as playlists_router
from app.ws import hub


app = FastAPI(title=settings.app_name)

_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


STALE_MEMBER_CHECK_INTERVAL_S = 300
STALE_MEMBER_TIMEOUT_S = 1800


@app.on_event("startup")
def _startup() -> None:
    init_db()


@app.on_event("startup")
async def _startup_bg_tasks() -> None:
    asyncio.create_task(_cleanup_stale_members())


async def _cleanup_stale_members() -> None:
    """Remove members who have been disconnected for 30+ minutes."""
    while True:
        await asyncio.sleep(STALE_MEMBER_CHECK_INTERVAL_S)
        try:
            stale = await hub.get_stale_users(timeout_seconds=STALE_MEMBER_TIMEOUT_S)
            for room_id, user_id in stale:
                try:
                    with Session(engine) as db:
                        await remove_member_from_room(db, room_id, user_id)
                    await hub.clear_stale_user(room_id, user_id)
                except Exception:
                    pass
        except Exception:
            pass


@app.get("/health")
def health():
    return {"ok": True, "app": settings.app_name}


app.include_router(auth_router)
app.include_router(rooms_router)
app.include_router(search_router)
app.include_router(queue_router)
app.include_router(playlists_router)


# Serve frontend static files
FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIR / "assets")), name="assets")


@app.get("/")
def index():
    index_path = FRONTEND_DIR / "index.html"
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "frontend not found"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    joined_room_id: int | None = None
    try:
        while True:
            raw = await ws.receive_text()
            try:
                msg = json.loads(raw)
            except Exception:
                await ws.send_text('{"type":"error","message":"invalid json"}')
                continue
            mtype = msg.get("type")
            if mtype == "join_room":
                room_id = msg.get("room_id")
                if not isinstance(room_id, int):
                    await ws.send_text('{"type":"error","message":"room_id must be int"}')
                    continue
                user_id = None
                token = msg.get("token")
                if token:
                    try:
                        payload = decode_token(token)
                        user_id = int(payload.get("sub", 0)) or None
                    except Exception:
                        pass
                if joined_room_id is not None:
                    await hub.leave(joined_room_id, ws)
                joined_room_id = room_id
                await hub.join(room_id, ws, user_id=user_id)
                await ws.send_text('{"type":"joined","ok":true}')
            else:
                await ws.send_text('{"type":"error","message":"unknown message type"}')
    except WebSocketDisconnect:
        pass
    finally:
        if joined_room_id is not None:
            await hub.leave(joined_room_id, ws)

