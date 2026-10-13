import asyncio
import random
from datetime import datetime, timedelta
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select

from app.core.config import settings
from app.db import engine
from app.deps import get_current_user, get_db
from app.models import (
    QueueStatus,
    Room,
    RoomMember,
    RoomMode,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackOrderStats,
    TrackSource,
    User,
    UserPlaylist,
    UserPlaylistItem,
)
from app.routers.playback_audio import (
    _direct_playback_audio_url,
    _ensure_bilibili_metadata,
)
from app.routers.media_cover import fetch_cover_response
from app.routers.playback_core import (
    _broadcast_playback_snapshot,
    _clear_loudness_wait_if_changed,
    _effective_position_ms,
    _is_stale_control,
    _pick_next_queue_item_id,
    _playback_lock,
    _playback_track_payload,
    _resolve_audio_url,
    _schedule_next_loudness_prefetch,
    _set_normalizer_preference_for_user,
    _set_playback,
    _stale_control_response,
    _track_has_loudness,
)
from app.schemas import PlaybackControlIn, VolumeControlIn
from app.track_availability import availability_fields, check_inputs, require_available
from app.ws import hub


router = APIRouter(prefix="/api", tags=["queue"])

class _QueueTrackInput(BaseModel):
    """Client-provided display metadata; playback URLs are deliberately absent."""

    model_config = ConfigDict(extra="ignore")

    source: TrackSource
    source_track_id: str = Field(min_length=1, max_length=128)
    title: str = Field(min_length=1, max_length=200)
    artist: str | None = Field(default=None, max_length=120)
    duration_ms: int | None = Field(default=None, ge=0, le=86_400_000)
    cover_url: str | None = Field(default=None, max_length=2048)


def _get_room(db: Session, room_id: int) -> Room:
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    return room


def _require_room_member(db: Session, room_id: int, user: User) -> Room:
    room = _get_room(db, room_id)
    existing = db.exec(
        select(RoomMember)
        .where(RoomMember.room_id == room_id, RoomMember.user_id == user.id)
        .limit(1)
    ).first()
    if not existing:
        raise HTTPException(status_code=403, detail="not a room member")
    return room


async def _require_active_room_member(db: Session, room_id: int, user: User) -> Room:
    room = _require_room_member(db, room_id, user)
    await hub.note_activity(room_id, user.id)
    return room


def _get_or_create_track(
    db: Session,
    *,
    source: TrackSource,
    source_track_id: str,
    title: str,
    artist: Optional[str] = None,
    duration_ms: Optional[int] = None,
    cover_url: Optional[str] = None,
) -> Track:
    # One global Track is shared by every room. Client display metadata may seed a
    # new row, but must never overwrite a row already populated by another room or
    # by a trusted upstream resolver.
    statement = sqlite_insert(Track).values(
        source=source,
        source_track_id=source_track_id,
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        cover_url=cover_url,
        audio_url=None,
    )
    statement = statement.on_conflict_do_nothing(index_elements=["source", "source_track_id"])
    db.execute(statement)
    track = db.exec(
        select(Track).where(Track.source == source, Track.source_track_id == source_track_id)
    ).first()
    if not track:
        raise RuntimeError("track upsert did not return a row")
    return track


def _validate_queue_track_payload(payload: object) -> _QueueTrackInput:
    try:
        track_input = _QueueTrackInput.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail="invalid track payload") from exc
    if track_input.source == TrackSource.netease and not track_input.source_track_id.isdigit():
        raise HTTPException(status_code=400, detail="invalid netease source_track_id")
    return track_input


def _parse_requested_count(raw_count: object) -> int | None:
    if raw_count is None:
        return None
    if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
        raise HTTPException(status_code=400, detail="count must be a positive integer")
    return raw_count


def _get_existing_queue_item(db: Session, room_id: int, track_id: int) -> RoomQueueItem | None:
    return db.exec(
        select(RoomQueueItem)
        .where(
            RoomQueueItem.room_id == room_id,
            RoomQueueItem.track_id == track_id,
            RoomQueueItem.status != QueueStatus.removed,
        )
        .order_by(RoomQueueItem.created_at.desc())
        .limit(1)
    ).first()


def _record_track_order(db: Session, track_id: int, ordered_at: datetime) -> None:
    statement = sqlite_insert(TrackOrderStats).values(
        track_id=track_id,
        order_count=1,
        last_ordered_at=ordered_at,
    )
    statement = statement.on_conflict_do_update(
        index_elements=["track_id"],
        set_={
            "order_count": TrackOrderStats.order_count + 1,
            "last_ordered_at": ordered_at,
        },
    )
    db.execute(statement)


def _enqueue_track_payload(
    db: Session,
    room_id: int,
    payload: dict | _QueueTrackInput,
    user_id: int,
    *,
    created_at: datetime | None = None,
) -> tuple[RoomQueueItem, bool]:
    track_input = payload if isinstance(payload, _QueueTrackInput) else _validate_queue_track_payload(payload)
    queued_at = created_at or datetime.utcnow()
    track = _get_or_create_track(
        db,
        source=track_input.source,
        source_track_id=track_input.source_track_id,
        title=track_input.title,
        artist=track_input.artist,
        duration_ms=track_input.duration_ms,
        cover_url=track_input.cover_url,
    )

    require_available(track)
    existing = _get_existing_queue_item(db, room_id, track.id)
    if existing and existing.status in (QueueStatus.playing, QueueStatus.queued):
        return existing, False

    if existing and existing.status == QueueStatus.played:
        existing.status = QueueStatus.queued
        existing.ordered_by_user_id = user_id
        existing.created_at = queued_at
        db.add(existing)
        db.flush()
        qi = existing
    else:
        qi = RoomQueueItem(
            room_id=room_id,
            track_id=track.id,
            ordered_by_user_id=user_id,
            status=QueueStatus.queued,
            created_at=queued_at,
        )
        db.add(qi)
        db.flush()

    _record_track_order(db, track.id, queued_at)
    return qi, True


def _enqueue_track_inputs_transaction(
    room_id: int,
    track_inputs: list[_QueueTrackInput],
    user_id: int,
    *,
    requested_count: int | None = None,
) -> tuple[list[int], int, int, int | None]:
    """Run the whole SQLite write transaction in one worker-owned Session."""
    queue_item_ids: list[int] = []
    added_count = 0
    skipped_count = 0
    first_queue_item_id: int | None = None
    seen: set[tuple[TrackSource, str]] = set()
    now = datetime.utcnow()
    with Session(engine) as write_db:
        try:
            for track_input in track_inputs:
                if requested_count is not None and added_count >= requested_count:
                    break
                key = (track_input.source, track_input.source_track_id)
                if key in seen:
                    skipped_count += 1
                    continue
                seen.add(key)
                queue_item, added = _enqueue_track_payload(
                    write_db,
                    room_id,
                    track_input,
                    user_id,
                    created_at=now + timedelta(milliseconds=added_count),
                )
                if first_queue_item_id is None:
                    first_queue_item_id = queue_item.id
                if added:
                    added_count += 1
                    queue_item_ids.append(queue_item.id)
                else:
                    skipped_count += 1
            write_db.commit()
        except BaseException:
            write_db.rollback()
            raise
    return queue_item_ids, added_count, skipped_count, first_queue_item_id


def _remove_queue_item_transaction(room_id: int, queue_item_id: int) -> tuple[str, int | None]:
    with Session(engine) as write_db:
        try:
            qi = write_db.get(RoomQueueItem, queue_item_id)
            if not qi or qi.room_id != room_id:
                return "not_found", None
            pb = write_db.get(RoomPlaybackState, room_id)
            removing_current = bool(pb and pb.current_queue_item_id == qi.id)
            qi.status = QueueStatus.removed
            write_db.add(qi)
            write_db.flush()
            next_id = _pick_next_queue_item_id(write_db, room_id) if removing_current else None

            if removing_current and pb:
                pb.current_queue_item_id = next_id
                pb.is_playing = next_id is not None
                pb.position_ms = 0
                pb.updated_at = datetime.utcnow()
                write_db.add(pb)

                playing_items = write_db.exec(
                    select(RoomQueueItem).where(
                        RoomQueueItem.room_id == room_id,
                        RoomQueueItem.status == QueueStatus.playing,
                    )
                ).all()
                for playing_item in playing_items:
                    if playing_item.id != next_id and playing_item.id != qi.id:
                        playing_item.status = QueueStatus.played
                        write_db.add(playing_item)
                if next_id is not None:
                    next_item = write_db.get(RoomQueueItem, next_id)
                    if not next_item:
                        raise RuntimeError("selected next queue item disappeared")
                    next_item.status = QueueStatus.playing
                    write_db.add(next_item)

            write_db.commit()
            return ("current" if removing_current else "removed"), next_id
        except BaseException:
            write_db.rollback()
            raise


def _bump_queue_item_transaction(room_id: int, queue_item_id: int) -> str:
    with Session(engine) as write_db:
        qi = write_db.get(RoomQueueItem, queue_item_id)
        if not qi or qi.room_id != room_id:
            return "not_found"
        if qi.status != QueueStatus.queued:
            return "ignored"
        first = write_db.exec(
            select(RoomQueueItem)
            .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
            .order_by(RoomQueueItem.created_at.asc())
            .limit(1)
        ).first()
        if not first or first.id == qi.id:
            return "unchanged"
        qi.created_at = first.created_at - timedelta(microseconds=1)
        write_db.add(qi)
        write_db.commit()
        return "moved"


def _shuffle_queue_transaction(room_id: int) -> bool:
    with Session(engine) as write_db:
        items = write_db.exec(
            select(RoomQueueItem)
            .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
            .order_by(RoomQueueItem.created_at.asc())
        ).all()
        if len(items) <= 1:
            return False
        random.shuffle(items)
        now = datetime.utcnow()
        for position, item in enumerate(items):
            item.created_at = now + timedelta(milliseconds=position)
            write_db.add(item)
        write_db.commit()
        return True


async def _ensure_queue_item_metadata(db: Session, queue_item: RoomQueueItem) -> None:
    track = db.get(Track, queue_item.track_id)
    if track and track.source == TrackSource.bilibili and not track.cover_url:
        await _ensure_bilibili_metadata(db, track)


async def _broadcast_or_start_after_enqueue(db: Session, room_id: int, fallback_queue_item_id: int) -> None:
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb or pb.current_queue_item_id is None:
            next_id = _pick_next_queue_item_id(db, room_id) or fallback_queue_item_id
            await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
        else:
            _schedule_next_loudness_prefetch(db, room_id)
            await hub.broadcast(room_id, {"type": "queue_updated"})


@router.get("/media/cover")
async def proxy_cover(url: str):
    return await fetch_cover_response(url)


@router.get("/tracks/{track_id}/audio-url")
async def get_track_audio_url(
    track_id: int,
    force: bool = False,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    tr = db.get(Track, track_id)
    if not tr:
        raise HTTPException(status_code=404, detail="track not found")
    await _resolve_audio_url(db, tr, force=force)
    has_loudness = _track_has_loudness(tr)
    return {
        "track_id": track_id,
        **availability_fields(tr),
        "audio_url": _direct_playback_audio_url(tr),
        "loudness_gain_db": tr.loudness_gain_db if has_loudness else None,
        "loudness_peak": tr.loudness_peak if has_loudness else None,
        "loudness_source": tr.loudness_source if has_loudness else None,
        "loudness_error": tr.loudness_error,
    }


@router.patch("/rooms/{room_id}/normalizer-preference")
async def set_normalizer_preference(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    return await _set_normalizer_preference_for_user(
        db,
        room_id,
        user.id,
        payload.get("enabled") is True,
    )


@router.get("/rooms/{room_id}/queue")
async def get_queue(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    items = db.exec(
        select(RoomQueueItem, Track, User)
        .join(Track, Track.id == RoomQueueItem.track_id)
        .join(User, User.id == RoomQueueItem.ordered_by_user_id)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status.in_([QueueStatus.playing, QueueStatus.queued]))
        .order_by(RoomQueueItem.created_at.asc())
    ).all()
    out = []
    for qi, tr, u in items:
        out.append(
            {
                "id": qi.id,
                "status": qi.status,
                "created_at": qi.created_at,
                "ordered_by": {"id": u.id, "username": u.username},
                "track": _playback_track_payload(tr),
            }
        )
    return out


@router.get("/rooms/{room_id}/history")
async def get_history(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    items = db.exec(
        select(RoomQueueItem, Track, User)
        .join(Track, Track.id == RoomQueueItem.track_id)
        .join(User, User.id == RoomQueueItem.ordered_by_user_id)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.played)
        .order_by(RoomQueueItem.created_at.desc())
        .limit(settings.rooms.history_limit)
    ).all()
    out = []
    for qi, tr, u in items:
        out.append(
            {
                "id": qi.id,
                "status": qi.status,
                "created_at": qi.created_at,
                "ordered_by": {"id": u.id, "username": u.username},
                "track": _playback_track_payload(tr),
            }
        )
    return out


@router.post("/rooms/{room_id}/queue")
async def add_to_queue(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    track_input = _validate_queue_track_payload(payload)
    db.rollback()
    _, unavailable = await check_inputs([track_input])
    if unavailable:
        raise HTTPException(status_code=409, detail={"code": "track_unavailable", "message": "该歌曲已失效", "track": unavailable[0]})

    async with _playback_lock(room_id):
        queue_item_ids, added_count, _, first_queue_item_id = await asyncio.to_thread(
            _enqueue_track_inputs_transaction,
            room_id,
            [track_input],
            user.id,
        )
    db.expire_all()
    queue_item_id = queue_item_ids[0] if queue_item_ids else first_queue_item_id
    qi = db.get(RoomQueueItem, queue_item_id) if queue_item_id is not None else None
    if qi is None:
        raise RuntimeError("queue transaction did not return an item")
    await _ensure_queue_item_metadata(db, qi)
    if not added_count:
        return {"ok": True, "queue_item_id": qi.id, "already_queued": True}

    await _broadcast_or_start_after_enqueue(db, room_id, qi.id)
    return {"ok": True, "queue_item_id": qi.id}


@router.post("/rooms/{room_id}/queue/batch")
async def add_to_queue_batch(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be list")
    validated_items = [_validate_queue_track_payload(item) for item in items]
    requested_count = _parse_requested_count(payload.get("count"))
    items = list(validated_items)
    if requested_count is not None:
        requested_count = min(requested_count, len(items))
    if payload.get("random") is True:
        random.shuffle(items)

    db.rollback()
    items, unavailable = await check_inputs(items[:requested_count] if requested_count is not None else items)

    async with _playback_lock(room_id):
        queue_item_ids, added_count, skipped_count, _ = await asyncio.to_thread(
            _enqueue_track_inputs_transaction,
            room_id,
            items,
            user.id,
            requested_count=requested_count,
        )

    db.expire_all()
    for queue_item_id in queue_item_ids:
        qi = db.get(RoomQueueItem, queue_item_id)
        if qi:
            await _ensure_queue_item_metadata(db, qi)

    if added_count:
        await _broadcast_or_start_after_enqueue(db, room_id, queue_item_ids[0])

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count + len(unavailable),
        "skipped_unavailable": unavailable,
        "queue_item_ids": queue_item_ids,
    }


@router.post("/rooms/{room_id}/queue/playlist")
async def add_playlist_to_queue(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    playlist_id = payload.get("playlist_id")
    if not isinstance(playlist_id, int):
        raise HTTPException(status_code=400, detail="playlist_id required")
    requested_count = _parse_requested_count(payload.get("count"))

    playlist = db.get(UserPlaylist, playlist_id)
    if not playlist or playlist.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")

    rows = db.exec(
        select(UserPlaylistItem, Track)
        .join(Track, Track.id == UserPlaylistItem.track_id)
        .where(UserPlaylistItem.playlist_id == playlist_id)
        .order_by(UserPlaylistItem.created_at.desc())
    ).all()
    rows = list(rows)
    if requested_count is not None:
        requested_count = min(requested_count, len(rows))
    random.shuffle(rows)

    track_inputs = [
        _QueueTrackInput(
            source=tr.source,
            source_track_id=tr.source_track_id,
            title=tr.title,
            artist=tr.artist,
            duration_ms=tr.duration_ms,
            cover_url=tr.cover_url,
        )
        for _, tr in rows
    ]
    db.rollback()
    track_inputs, unavailable = await check_inputs(track_inputs[:requested_count] if requested_count is not None else track_inputs)

    async with _playback_lock(room_id):
        queue_item_ids, added_count, skipped_count, _ = await asyncio.to_thread(
            _enqueue_track_inputs_transaction,
            room_id,
            track_inputs,
            user.id,
            requested_count=requested_count,
        )

    db.expire_all()
    for queue_item_id in queue_item_ids:
        qi = db.get(RoomQueueItem, queue_item_id)
        if qi:
            await _ensure_queue_item_metadata(db, qi)

    if added_count:
        await _broadcast_or_start_after_enqueue(db, room_id, queue_item_ids[0])

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count + len(unavailable),
        "skipped_unavailable": unavailable,
        "queue_item_ids": queue_item_ids,
    }


@router.delete("/rooms/{room_id}/queue/{queue_item_id}")
async def remove_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    db.rollback()
    async with _playback_lock(room_id):
        outcome, next_id = await asyncio.to_thread(
            _remove_queue_item_transaction,
            room_id,
            queue_item_id,
        )
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="queue item not found")
        db.expire_all()
        if outcome == "current":
            _clear_loudness_wait_if_changed(room_id, next_id)
            await _broadcast_playback_snapshot(db, room_id)
            await hub.broadcast(room_id, {"type": "queue_updated"})
            _schedule_next_loudness_prefetch(db, room_id)
        else:
            await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.post("/rooms/{room_id}/queue/{queue_item_id}/bump")
async def bump_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    db.rollback()
    async with _playback_lock(room_id):
        outcome = await asyncio.to_thread(_bump_queue_item_transaction, room_id, queue_item_id)
        if outcome == "not_found":
            raise HTTPException(status_code=404, detail="queue item not found")
        if outcome == "ignored":
            return {"ok": True, "ignored": True}
        if outcome == "moved":
            await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.post("/rooms/{room_id}/controls/play")
async def play(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        if pb.current_queue_item_id is None:
            next_id = _pick_next_queue_item_id(db, room_id)
            if next_id is None:
                await _set_playback(db, room_id, current_queue_item_id=None, is_playing=False, position_ms=0)
                return {"ok": True, "empty": True}
            await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
        else:
            await _set_playback(db, room_id, is_playing=True)
    return {"ok": True}


@router.post("/rooms/{room_id}/controls/pause")
async def pause(room_id: int, payload: PlaybackControlIn | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        if _is_stale_control(pb, payload):
            return _stale_control_response(pb)
        pos = payload.position_ms if payload else None
        if pos is not None:
            pos = max(0, int(pos))
        if pos is None:
            if pb and pb.is_playing and pb.current_queue_item_id:
                pos = _effective_position_ms(pb)
        if pos is not None:
            await _set_playback(db, room_id, is_playing=False, position_ms=pos)
        else:
            await _set_playback(db, room_id, is_playing=False)
    return {"ok": True}


@router.post("/rooms/{room_id}/controls/next")
async def next_track(room_id: int, payload: PlaybackControlIn | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        if _is_stale_control(pb, payload):
            return _stale_control_response(pb)
        next_id = _pick_next_queue_item_id(db, room_id)
        await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=next_id is not None, position_ms=0)
    return {"ok": True}


@router.post("/rooms/{room_id}/controls/prev")
async def prev_track(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        await _set_playback(db, room_id, position_ms=0)
    return {"ok": True}


@router.post("/rooms/{room_id}/queue/shuffle")
async def shuffle_queue(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    db.rollback()
    async with _playback_lock(room_id):
        changed = await asyncio.to_thread(_shuffle_queue_transaction, room_id)
        if changed:
            await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.patch("/rooms/{room_id}/controls/position")
async def set_position(room_id: int, payload: PlaybackControlIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        if _is_stale_control(pb, payload):
            return _stale_control_response(pb)
        await _set_playback(db, room_id, position_ms=payload.position_ms or 0)
    return {"ok": True}


@router.patch("/rooms/{room_id}/controls/volume")
async def set_volume(room_id: int, payload: VolumeControlIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        if pb.mode.value != "play_enabled":
            raise HTTPException(status_code=403, detail="volume only allowed in play_enabled mode")
        await _set_playback(db, room_id, volume=payload.volume)
    return {"ok": True}
