import random
from datetime import datetime, timedelta
from typing import Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, select
from starlette.responses import Response

from app.core.config import settings
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
from app.routers.playback_core import (
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
from app.ws import hub


router = APIRouter(prefix="/api", tags=["queue"])

_COVER_PROXY_HOST_SUFFIXES = (
    "bilibili.com",
    "biliimg.com",
    "hdslb.com",
)


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
    audio_url: Optional[str] = None,
) -> Track:
    track = db.exec(select(Track).where(Track.source == source, Track.source_track_id == source_track_id)).first()
    if track:
        if title and track.title != title:
            track.title = title
        track.artist = track.artist or artist
        track.duration_ms = track.duration_ms or duration_ms
        track.cover_url = track.cover_url or cover_url
        track.audio_url = track.audio_url or audio_url
        db.add(track)
        db.commit()
        db.refresh(track)
        return track
    track = Track(
        source=source,
        source_track_id=source_track_id,
        title=title,
        artist=artist,
        duration_ms=duration_ms,
        cover_url=cover_url,
        audio_url=audio_url,
    )
    db.add(track)
    db.commit()
    db.refresh(track)
    return track


def _validate_queue_track_payload(payload: dict) -> tuple[TrackSource, str, str]:
    source = payload.get("source")
    source_track_id = payload.get("source_track_id")
    title = payload.get("title")
    if source not in {s.value for s in TrackSource}:
        raise HTTPException(status_code=400, detail="invalid source")
    if not source_track_id or not isinstance(source_track_id, str):
        raise HTTPException(status_code=400, detail="invalid source_track_id")
    if not title or not isinstance(title, str):
        raise HTTPException(status_code=400, detail="invalid title")
    return TrackSource(source), source_track_id, title


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
    stats = db.get(TrackOrderStats, track_id)
    if not stats:
        stats = TrackOrderStats(track_id=track_id, order_count=0, last_ordered_at=ordered_at)
    stats.order_count += 1
    stats.last_ordered_at = ordered_at
    db.add(stats)


def _enqueue_track_payload(
    db: Session,
    room_id: int,
    payload: dict,
    user_id: int,
    *,
    created_at: datetime | None = None,
) -> tuple[RoomQueueItem, bool]:
    source, source_track_id, title = _validate_queue_track_payload(payload)
    queued_at = created_at or datetime.utcnow()
    track = _get_or_create_track(
        db,
        source=source,
        source_track_id=source_track_id,
        title=title,
        artist=payload.get("artist"),
        duration_ms=payload.get("duration_ms"),
        cover_url=payload.get("cover_url"),
        audio_url=payload.get("audio_url"),
    )

    existing = _get_existing_queue_item(db, room_id, track.id)
    if existing and existing.status in (QueueStatus.playing, QueueStatus.queued):
        return existing, False

    if existing and existing.status == QueueStatus.played:
        existing.status = QueueStatus.queued
        existing.ordered_by_user_id = user_id
        existing.created_at = queued_at
        db.add(existing)
        db.commit()
        db.refresh(existing)
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
        db.commit()
        db.refresh(qi)

    _record_track_order(db, track.id, queued_at)
    db.commit()
    return qi, True


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
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if parsed.scheme not in {"http", "https"} or not any(host == suffix or host.endswith(f".{suffix}") for suffix in _COVER_PROXY_HOST_SUFFIXES):
        raise HTTPException(status_code=400, detail="unsupported cover host")

    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    try:
        async with httpx.AsyncClient(timeout=settings.upstream.bilibili_audio_timeout_s, follow_redirects=True, headers=headers) as client:
            r = await client.get(url)
            r.raise_for_status()
    except Exception:
        raise HTTPException(status_code=502, detail="cover fetch failed")

    content_type = r.headers.get("content-type") or "image/jpeg"
    return Response(
        content=r.content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )


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

    async with _playback_lock(room_id):
        qi, added = _enqueue_track_payload(db, room_id, payload, user.id)
    await _ensure_queue_item_metadata(db, qi)
    if not added:
        return {"ok": True, "queue_item_id": qi.id, "already_queued": True}

    await _broadcast_or_start_after_enqueue(db, room_id, qi.id)
    return {"ok": True, "queue_item_id": qi.id}


@router.post("/rooms/{room_id}/queue/batch")
async def add_to_queue_batch(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be list")

    seen: set[tuple[str, str]] = set()
    queue_item_ids: list[int] = []
    queue_items: list[RoomQueueItem] = []
    added_count = 0
    skipped_count = 0
    now = datetime.utcnow()

    async with _playback_lock(room_id):
        for raw in items:
            if not isinstance(raw, dict):
                raise HTTPException(status_code=400, detail="invalid item")
            source = raw.get("source")
            source_track_id = raw.get("source_track_id")
            key = (str(source), str(source_track_id))
            if key in seen:
                skipped_count += 1
                continue
            seen.add(key)

            qi, added = _enqueue_track_payload(
                db,
                room_id,
                raw,
                user.id,
                created_at=now + timedelta(milliseconds=added_count),
            )
            if added:
                added_count += 1
                queue_item_ids.append(qi.id)
                queue_items.append(qi)
            else:
                skipped_count += 1

    for qi in queue_items:
        await _ensure_queue_item_metadata(db, qi)

    if added_count:
        await _broadcast_or_start_after_enqueue(db, room_id, queue_item_ids[0])

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count,
        "queue_item_ids": queue_item_ids,
    }


@router.post("/rooms/{room_id}/queue/playlist")
async def add_playlist_to_queue(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    playlist_id = payload.get("playlist_id")
    if not isinstance(playlist_id, int):
        raise HTTPException(status_code=400, detail="playlist_id required")
    raw_count = payload.get("count")
    requested_count: int | None = None
    if raw_count is not None:
        if isinstance(raw_count, bool) or not isinstance(raw_count, int) or raw_count < 1:
            raise HTTPException(status_code=400, detail="count must be a positive integer")
        requested_count = raw_count

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

    queue_item_ids: list[int] = []
    queue_items: list[RoomQueueItem] = []
    added_count = 0
    skipped_count = 0
    now = datetime.utcnow()

    async with _playback_lock(room_id):
        for _, tr in rows:
            if requested_count is not None and added_count >= requested_count:
                break
            existing = _get_existing_queue_item(db, room_id, tr.id)
            if existing and existing.status in (QueueStatus.playing, QueueStatus.queued):
                skipped_count += 1
                continue
            qi, added = _enqueue_track_payload(
                db,
                room_id,
                {
                    "source": tr.source.value,
                    "source_track_id": tr.source_track_id,
                    "title": tr.title,
                    "artist": tr.artist,
                    "duration_ms": tr.duration_ms,
                    "cover_url": tr.cover_url,
                    "audio_url": tr.audio_url,
                },
                user.id,
                created_at=now + timedelta(milliseconds=added_count),
            )
            if added:
                added_count += 1
                queue_item_ids.append(qi.id)
                queue_items.append(qi)
            else:
                skipped_count += 1

    for qi in queue_items:
        await _ensure_queue_item_metadata(db, qi)

    if added_count:
        await _broadcast_or_start_after_enqueue(db, room_id, queue_item_ids[0])

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count,
        "queue_item_ids": queue_item_ids,
    }


@router.delete("/rooms/{room_id}/queue/{queue_item_id}")
async def remove_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    async with _playback_lock(room_id):
        qi = db.get(RoomQueueItem, queue_item_id)
        if not qi or qi.room_id != room_id:
            raise HTTPException(status_code=404, detail="queue item not found")
        pb = db.get(RoomPlaybackState, room_id)
        removing_current = bool(pb and pb.current_queue_item_id == qi.id)
        qi.status = QueueStatus.removed
        db.add(qi)
        db.commit()
        if removing_current:
            next_id = _pick_next_queue_item_id(db, room_id)
            await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=next_id is not None, position_ms=0)
        else:
            await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.post("/rooms/{room_id}/queue/{queue_item_id}/bump")
async def bump_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    qi = db.get(RoomQueueItem, queue_item_id)
    if not qi or qi.room_id != room_id:
        raise HTTPException(status_code=404, detail="queue item not found")
    if qi.status != QueueStatus.queued:
        return {"ok": True, "ignored": True}

    first = db.exec(
        select(RoomQueueItem)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
        .order_by(RoomQueueItem.created_at.asc())
        .limit(1)
    ).first()
    if not first:
        return {"ok": True}
    if first.id == qi.id:
        return {"ok": True}

    qi.created_at = first.created_at - timedelta(microseconds=1)
    db.add(qi)
    db.commit()
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
        if pb.current_queue_item_id:
            cur = db.get(RoomQueueItem, pb.current_queue_item_id)
            if cur:
                cur.status = QueueStatus.played
                db.add(cur)
                db.commit()
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
    items = db.exec(
        select(RoomQueueItem)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
        .order_by(RoomQueueItem.created_at.asc())
    ).all()
    if len(items) <= 1:
        return {"ok": True}
    now = datetime.utcnow()
    indices = list(range(len(items)))
    random.shuffle(indices)
    for new_pos, idx in enumerate(indices):
        items[idx].created_at = now + timedelta(milliseconds=new_pos)
        db.add(items[idx])
    db.commit()
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
