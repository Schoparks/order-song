import asyncio
import json
import logging
from contextlib import asynccontextmanager, suppress
from pathlib import Path

from fastapi import FastAPI
from fastapi import WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from sqlmodel import Session, select

from app.audio_loudness import is_loudness_analysis_available
from app.core.config import settings, validate_auth_configuration
from app.core.security import decode_token
from app.db import engine, init_db
from app.models import RoomMember
from app.routers.auth import router as auth_router
from app.routers.rooms import router as rooms_router, remove_member_from_room
from app.routers.search import router as search_router
from app.routers.queue_playback import router as queue_router
from app.routers.playlists_trending import router as playlists_router
from app.routers.admin import router as admin_router
from app.ws import hub


logger = logging.getLogger(__name__)


async def _cleanup_stale_members() -> None:
    """Remove members after both socket presence and REST activity go stale."""
    while True:
        await asyncio.sleep(settings.rooms.stale_check_interval_s)
        try:
            stale = await hub.get_stale_users(timeout_seconds=settings.rooms.stale_timeout_s)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Failed to collect stale room members")
            continue
        for room_id, user_id in stale:
            try:
                await remove_member_from_room(room_id, user_id)
                await hub.clear_stale_user(room_id, user_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception(
                    "Failed to remove stale room member room_id=%s user_id=%s",
                    room_id,
                    user_id,
                )


@asynccontextmanager
async def _lifespan(_app: FastAPI):
    validate_auth_configuration(settings)
    init_db()
    cleanup_task = asyncio.create_task(_cleanup_stale_members(), name="stale-room-member-cleanup")
    try:
        yield
    finally:
        cleanup_task.cancel()
        with suppress(asyncio.CancelledError):
            await cleanup_task


app = FastAPI(title=settings.app_name, lifespan=_lifespan)

_origins = [o.strip() for o in settings.cors_allow_origins.split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_origins,
    allow_credentials="*" not in _origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/health")
def health():
    return {"ok": True, "app": settings.app_name}


@app.get("/api/config")
def public_config():
    config = settings.public_config()
    config["audio_loudness"] = {
        "enabled": settings.audio_loudness.enabled,
        "ffmpeg_available": is_loudness_analysis_available(),
    }
    return config


app.include_router(auth_router)
app.include_router(rooms_router)
app.include_router(search_router)
app.include_router(queue_router)
app.include_router(playlists_router)
app.include_router(admin_router)


FRONTEND_DIR = Path(__file__).resolve().parents[2] / "frontend"
FRONTEND_DIST_DIR = FRONTEND_DIR / "dist"


def _frontend_index_path() -> Path:
    return FRONTEND_DIST_DIR / "index.html"


if (FRONTEND_DIST_DIR / "assets").exists():
    app.mount("/assets", StaticFiles(directory=str(FRONTEND_DIST_DIR / "assets")), name="assets")


@app.get("/favicon.ico", include_in_schema=False)
def favicon():
    ico_path = FRONTEND_DIR / "favicon.ico"
    if ico_path.exists():
        return FileResponse(str(ico_path), media_type="image/x-icon")
    return Response(status_code=404)


@app.get("/")
def index():
    index_path = _frontend_index_path()
    if index_path.exists():
        return FileResponse(str(index_path))
    return {"message": "frontend not found"}


@app.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    joined_room_id: int | None = None
    joined_user_id: int | None = None
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
                if not token:
                    await ws.send_text('{"type":"error","message":"auth required"}')
                    continue
                try:
                    payload = decode_token(token)
                    user_id = int(payload.get("sub", 0)) or None
                except Exception:
                    await ws.send_text('{"type":"error","message":"invalid token"}')
                    continue
                if user_id is None:
                    await ws.send_text('{"type":"error","message":"invalid token"}')
                    continue
                with Session(engine) as db:
                    member = db.exec(
                        select(RoomMember)
                        .where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)
                        .limit(1)
                    ).first()
                if not member:
                    await ws.send_text('{"type":"error","message":"not a room member"}')
                    continue
                if joined_room_id is not None:
                    await hub.leave(joined_room_id, ws)
                joined_room_id = room_id
                joined_user_id = user_id
                await hub.join(room_id, ws, user_id=user_id)
                await ws.send_text('{"type":"joined","ok":true}')
            elif mtype == "ping":
                if joined_room_id is not None and joined_user_id is not None:
                    await hub.note_activity(joined_room_id, joined_user_id)
                await ws.send_text('{"type":"pong"}')
            else:
                await ws.send_text('{"type":"error","message":"unknown message type"}')
    except WebSocketDisconnect:
        pass
    finally:
        if joined_room_id is not None:
            await hub.leave(joined_room_id, ws)
