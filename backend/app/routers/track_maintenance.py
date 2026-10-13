"""Owner-scoped repair of unavailable songs across a user's playlists."""
import asyncio

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select

from app.db import engine
from app.deps import get_current_user, get_db
from app.models import Track, User, UserPlaylist, UserPlaylistItem
from app.routers.queue_playback import _validate_queue_track_payload
from app.track_availability import _read_or_create, check_track, require_available, track_summary

router = APIRouter(prefix="/api", tags=["tracks"])


def _owned_items(db: Session, user_id: int, track_id: int) -> list[UserPlaylistItem]:
    return list(db.exec(select(UserPlaylistItem).join(UserPlaylist, UserPlaylist.id == UserPlaylistItem.playlist_id).where(UserPlaylist.user_id == user_id, UserPlaylistItem.track_id == track_id)).all())


def _require_owned_unavailable(db: Session, user_id: int, track_id: int) -> list[UserPlaylistItem]:
    items = _owned_items(db, user_id, track_id)
    track = db.get(Track, track_id)
    if not items or not track:
        raise HTTPException(status_code=404, detail="歌曲不在你的歌单中")
    if track.availability != "unavailable":
        raise HTTPException(status_code=409, detail="只有已确认失效的歌曲可以批量移除或替换")
    return items


def _remove_all(user_id: int, track_id: int) -> int:
    with Session(engine) as db:
        items = _require_owned_unavailable(db, user_id, track_id)
        for item in items:
            db.delete(item)
        db.commit()
        return len(items)


def _replace_all(user_id: int, track_id: int, replacement_id: int) -> int:
    with Session(engine) as db:
        items = _require_owned_unavailable(db, user_id, track_id)
        replacement = db.get(Track, replacement_id)
        if not replacement or replacement_id == track_id:
            raise HTTPException(status_code=400, detail="请选择另一首歌曲")
        require_available(replacement)
        if replacement.availability != "available":
            raise HTTPException(status_code=503, detail="暂时无法确认替换歌曲可播放，请稍后重试")
        for item in items:
            duplicate = db.exec(select(UserPlaylistItem).where(UserPlaylistItem.playlist_id == item.playlist_id, UserPlaylistItem.track_id == replacement_id)).first()
            if duplicate:
                db.delete(duplicate)
                db.flush()
            # Keep the old membership id and timestamp, preserving its exact slot.
            item.track_id = replacement_id
            db.add(item)
        db.commit()
        return len(items)


@router.post("/tracks/{track_id}/availability")
async def refresh_availability(track_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    track = db.get(Track, track_id)
    if not track:
        raise HTTPException(status_code=404, detail="track not found")
    db.expunge(track)
    db.rollback()
    return track_summary(await check_track(track, force=True))


@router.delete("/playlists/tracks/{track_id}")
async def remove_from_all_playlists(track_id: int, user: User = Depends(get_current_user)):
    removed = await asyncio.to_thread(_remove_all, user.id, track_id)
    return {"ok": True, "removed": removed}


@router.post("/playlists/tracks/{track_id}/replace")
async def replace_in_all_playlists(track_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_owned_unavailable(db, user.id, track_id)
    track_input = _validate_queue_track_payload(payload)
    old_track = db.get(Track, track_id)
    if (track_input.source, track_input.source_track_id) == (old_track.source, old_track.source_track_id):
        raise HTTPException(status_code=400, detail="请选择另一首歌曲")
    db.rollback()
    replacement = await asyncio.to_thread(_read_or_create, track_input.model_dump())
    replacement = await check_track(replacement, force=True)
    replaced = await asyncio.to_thread(_replace_all, user.id, track_id, replacement.id)
    return {"ok": True, "replaced": replaced, "track": track_summary(replacement)}
