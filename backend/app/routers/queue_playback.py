import asyncio
import random
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import Response, StreamingResponse
from sqlmodel import Session, select

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.models import (
    QueueStatus,
    Room,
    RoomMember,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackOrderStats,
    TrackSource,
    User,
    UserPlaylist,
    UserPlaylistItem,
)
from app.schemas import PlaybackControlIn, PlaybackStateOut, TrackOut, VolumeControlIn
from app.ws import hub


router = APIRouter(prefix="/api", tags=["queue"])


_UNSET: Any = object()
_PLAYBACK_LOCKS: dict[int, asyncio.Lock] = {}
_COVER_PROXY_HOST_SUFFIXES = (
    "bilibili.com",
    "biliimg.com",
    "hdslb.com",
)


def _playback_lock(room_id: int) -> asyncio.Lock:
    lock = _PLAYBACK_LOCKS.get(room_id)
    if lock is None:
        lock = asyncio.Lock()
        _PLAYBACK_LOCKS[room_id] = lock
    return lock


def _effective_position_ms(pb: RoomPlaybackState, *, now: datetime | None = None) -> int:
    current = now or datetime.utcnow()
    position = max(0, int(pb.position_ms or 0))
    if pb.is_playing and pb.current_queue_item_id and pb.updated_at:
        elapsed_ms = max(0, int((current - pb.updated_at).total_seconds() * 1000))
        position += elapsed_ms
    return position


def _utc_timestamp_ms(value: datetime) -> int:
    current = value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    return int(current.astimezone(timezone.utc).timestamp() * 1000)


def _playback_state_payload(pb: RoomPlaybackState, *, now: datetime | None = None) -> dict[str, Any]:
    current = now or datetime.utcnow()
    return {
        "playback_state": PlaybackStateOut.model_validate(pb).model_dump(mode="json"),
        "server_time": current.isoformat() + "Z",
        "server_ts_ms": _utc_timestamp_ms(current),
        "effective_position_ms": _effective_position_ms(pb, now=current),
    }


def _is_stale_control(pb: RoomPlaybackState, payload: PlaybackControlIn | None) -> bool:
    return (
        payload is not None
        and payload.expected_queue_item_id is not None
        and pb.current_queue_item_id != payload.expected_queue_item_id
    )


def _stale_control_response(pb: RoomPlaybackState) -> dict[str, Any]:
    return {
        "ok": True,
        "ignored": True,
        "reason": "stale_current_queue_item",
        **_playback_state_payload(pb),
    }


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


def _get_or_create_track(db: Session, *, source: TrackSource, source_track_id: str, title: str, artist: Optional[str] = None, duration_ms: Optional[int] = None, cover_url: Optional[str] = None, audio_url: Optional[str] = None) -> Track:
    track = db.exec(select(Track).where(Track.source == source, Track.source_track_id == source_track_id)).first()
    if track:
        # update soft fields if missing
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


def _normalize_bilibili_pic(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    pic = value.strip()
    if pic.startswith("//"):
        return f"https:{pic}"
    return pic


async def _fetch_bilibili_video_metadata(bv: str) -> dict[str, Any] | None:
    try:
        async with httpx.AsyncClient(
            timeout=settings.upstream.bilibili_audio_timeout_s,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://www.bilibili.com/",
            },
        ) as client:
            r = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bv},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                return None
            return data.get("data") or None
    except Exception:
        return None


async def _ensure_bilibili_metadata(db: Session, track: Track) -> dict[str, Any] | None:
    if track.source != TrackSource.bilibili:
        return None
    video = await _fetch_bilibili_video_metadata(track.source_track_id)
    if not video:
        return None

    changed = False
    cover_url = _normalize_bilibili_pic(video.get("pic"))
    owner = video.get("owner") or {}
    duration = video.get("duration")

    if cover_url and not track.cover_url:
        track.cover_url = cover_url
        changed = True
    if isinstance(owner, dict) and owner.get("name") and not track.artist:
        track.artist = owner["name"]
        changed = True
    if duration is not None and not track.duration_ms:
        track.duration_ms = int(duration) * 1000
        changed = True
    if video.get("title") and (not track.title or track.title == track.source_track_id):
        track.title = video["title"]
        changed = True

    if changed:
        db.add(track)
        db.commit()
        db.refresh(track)
    return video


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
            await hub.broadcast(room_id, {"type": "queue_updated"})


async def _resolve_audio_url(db: Session, track: Track, *, force: bool = False) -> Optional[str]:
    video: dict[str, Any] | None = None
    if track.source == TrackSource.bilibili and not track.cover_url:
        video = await _ensure_bilibili_metadata(db, track)

    if track.audio_url and not force:
        return track.audio_url

    if track.source == TrackSource.netease:
        # Best-effort: often redirects to playable audio
        track.audio_url = f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
        db.add(track)
        db.commit()
        db.refresh(track)
        return track.audio_url

    if track.source == TrackSource.bilibili:
        if video is None:
            video = await _ensure_bilibili_metadata(db, track)
        audio_url = await _resolve_bilibili_audio(track.source_track_id, video_data=video)

        if not audio_url:
            page_url = f"https://www.bilibili.com/video/{track.source_track_id}"

            def _run() -> Optional[str]:
                try:
                    r = subprocess.run(
                        ["yt-dlp", "-f", "ba", "-g", "--no-playlist", page_url],
                        capture_output=True,
                        text=True,
                        timeout=settings.upstream.yt_dlp_timeout_s,
                        check=False,
                    )
                    if r.returncode != 0:
                        return None
                    out = (r.stdout or "").strip().splitlines()
                    return out[-1].strip() if out else None
                except Exception:
                    return None

            audio_url = await asyncio.to_thread(_run)

        if audio_url:
            track.audio_url = audio_url
            db.add(track)
            db.commit()
            db.refresh(track)
        return track.audio_url

    return None


async def _resolve_bilibili_audio(bv: str, *, video_data: dict[str, Any] | None = None) -> Optional[str]:
    """Resolve audio stream URL via bilibili's playurl API (no yt-dlp needed)."""
    try:
        async with httpx.AsyncClient(timeout=settings.upstream.bilibili_audio_timeout_s, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }) as client:
            vid = video_data
            if not vid:
                r = await client.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": bv},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("code") != 0:
                    return None
                vid = data["data"]
            if not vid:
                return None
            cid = vid.get("cid")
            if not cid and vid.get("pages"):
                cid = vid["pages"][0].get("cid")
            if not cid:
                return None

            r = await client.get(
                "https://api.bilibili.com/x/player/playurl",
                params={"bvid": bv, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                return None

            dash = (data.get("data") or {}).get("dash")
            if not dash:
                return None
            audio_list = dash.get("audio") or []
            if not audio_list:
                return None
            aac = [a for a in audio_list if "mp4a" in (a.get("codecs") or "")]
            candidates = aac if aac else audio_list
            candidates.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            return candidates[0].get("baseUrl") or candidates[0].get("base_url")
    except Exception:
        return None


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


@router.get("/tracks/{track_id}/stream")
async def stream_track(track_id: int, request: Request, db: Session = Depends(get_db)):
    tr = db.get(Track, track_id)
    if not tr:
        raise HTTPException(status_code=404, detail="track not found")

    max_attempts = 2 if tr.source == TrackSource.bilibili else 1
    rng = request.headers.get("range")

    for attempt in range(max_attempts):
        await _resolve_audio_url(db, tr, force=(attempt > 0))
        if not tr.audio_url:
            raise HTTPException(status_code=404, detail="audio url not available")

        headers: dict[str, str] = {"User-Agent": "Mozilla/5.0"}
        if tr.source == TrackSource.bilibili:
            headers["Referer"] = "https://www.bilibili.com/"
            headers["Origin"] = "https://www.bilibili.com"
            headers["Accept"] = "*/*"
            headers["Accept-Language"] = "zh-CN,zh;q=0.9,en;q=0.6"
        if tr.source == TrackSource.netease:
            headers["Referer"] = "https://music.163.com/"

        if rng:
            headers["Range"] = rng

        client = httpx.AsyncClient(timeout=settings.upstream.stream_timeout_s, follow_redirects=True, headers=headers)
        try:
            req = client.build_request("GET", tr.audio_url)
            r = await client.send(req, stream=True)
        except Exception:
            await client.aclose()
            if attempt < max_attempts - 1:
                tr.audio_url = None
                db.add(tr)
                db.commit()
                continue
            raise HTTPException(status_code=502, detail="upstream fetch failed")

        if r.status_code >= 400:
            await r.aclose()
            await client.aclose()
            if attempt < max_attempts - 1:
                tr.audio_url = None
                db.add(tr)
                db.commit()
                continue
            raise HTTPException(status_code=502, detail=f"upstream error {r.status_code}")

        media_type = r.headers.get("content-type") or "audio/mpeg"
        if tr.source == TrackSource.bilibili:
            media_type = "audio/mp4"
        resp_headers: dict[str, str] = {}
        for k in ("accept-ranges", "content-range", "content-length", "etag", "last-modified"):
            v = r.headers.get(k)
            if v:
                resp_headers[k] = v
        resp_headers.setdefault("accept-ranges", "bytes")
        resp_headers["cache-control"] = "no-store"

        async def _iter():
            try:
                async for chunk in r.aiter_bytes():
                    yield chunk
            finally:
                await r.aclose()
                await client.aclose()

        return StreamingResponse(_iter(), media_type=media_type, headers=resp_headers, status_code=r.status_code)

    raise HTTPException(status_code=502, detail="all stream attempts failed")


@router.get("/rooms/{room_id}/queue")
def get_queue(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_room_member(db, room_id, user)
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
                "track": TrackOut.model_validate(tr).model_dump(mode="json"),
            }
        )
    return out


@router.get("/rooms/{room_id}/history")
def get_history(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _require_room_member(db, room_id, user)
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
                "track": TrackOut.model_validate(tr).model_dump(mode="json"),
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
    random.shuffle(rows)

    queue_item_ids: list[int] = []
    queue_items: list[RoomQueueItem] = []
    added_count = 0
    skipped_count = 0
    now = datetime.utcnow()

    async with _playback_lock(room_id):
        for _, tr in rows:
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

    # Move to top by shifting created_at to just before the current first item.
    qi.created_at = first.created_at - timedelta(microseconds=1)
    db.add(qi)
    db.commit()
    await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


async def _set_playback(
    db: Session,
    room_id: int,
    *,
    is_playing: Optional[bool] = None,
    position_ms: Optional[int] = None,
    volume: Optional[int] = None,
    current_queue_item_id: Any = _UNSET,  # Optional[int] or _UNSET
) -> RoomPlaybackState:
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        pb = RoomPlaybackState(room_id=room_id, volume=settings.rooms.default_volume)
    old_queue_item_id = pb.current_queue_item_id
    if is_playing is not None:
        pb.is_playing = is_playing
    if position_ms is not None:
        pb.position_ms = max(0, int(position_ms))
    if volume is not None:
        pb.volume = max(0, min(100, int(volume)))
    if current_queue_item_id is not _UNSET:
        pb.current_queue_item_id = current_queue_item_id
    pb.updated_at = datetime.utcnow()
    db.add(pb)
    db.commit()
    db.refresh(pb)

    current_track = None
    if current_queue_item_id is not _UNSET and old_queue_item_id and old_queue_item_id != pb.current_queue_item_id:
        old_qi = db.get(RoomQueueItem, old_queue_item_id)
        if old_qi and old_qi.status == QueueStatus.playing:
            old_qi.status = QueueStatus.played
            db.add(old_qi)
            db.commit()
    ordered_by = None
    if pb.current_queue_item_id:
        playing_items = db.exec(
            select(RoomQueueItem).where(
                RoomQueueItem.room_id == room_id,
                RoomQueueItem.status == QueueStatus.playing,
                RoomQueueItem.id != pb.current_queue_item_id,
            )
        ).all()
        for item in playing_items:
            item.status = QueueStatus.played
            db.add(item)
        if playing_items:
            db.commit()
        qi = db.get(RoomQueueItem, pb.current_queue_item_id)
        if qi:
            qi.status = QueueStatus.playing
            db.add(qi)
            db.commit()
            u = db.get(User, qi.ordered_by_user_id)
            if u:
                ordered_by = {"id": u.id, "username": u.username}
            tr = db.get(Track, qi.track_id)
            if tr:
                await _resolve_audio_url(db, tr)
                current_track = TrackOut.model_validate(tr).model_dump(mode="json")
                if tr.audio_url:
                    current_track["audio_url"] = f"/api/tracks/{tr.id}/stream"
    elif current_queue_item_id is not _UNSET:
        playing_items = db.exec(
            select(RoomQueueItem).where(
                RoomQueueItem.room_id == room_id,
                RoomQueueItem.status == QueueStatus.playing,
            )
        ).all()
        for item in playing_items:
            item.status = QueueStatus.played
            db.add(item)
        if playing_items:
            db.commit()
    await hub.broadcast(
        room_id,
        {
            "type": "playback_updated",
            "room_id": room_id,
            **_playback_state_payload(pb),
            "current_track": current_track,
            "ordered_by": ordered_by,
        },
    )
    if current_queue_item_id is not _UNSET:
        await hub.broadcast(room_id, {"type": "queue_updated"})
    return pb


def _pick_next_queue_item_id(db: Session, room_id: int) -> Optional[int]:
    qi = db.exec(
        select(RoomQueueItem)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
        .order_by(RoomQueueItem.created_at.asc())
        .limit(1)
    ).first()
    return qi.id if qi else None


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
        if (
            _is_stale_control(pb, payload)
        ):
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
    # Minimal behavior: restart current track
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
