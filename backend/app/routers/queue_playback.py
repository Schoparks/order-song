import asyncio
import random
import subprocess
from datetime import datetime, timedelta
from typing import Any, Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request
from starlette.responses import StreamingResponse
from sqlmodel import Session, func, select

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.models import (
    QueueStatus,
    Room,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackOrderStats,
    TrackSource,
    User,
    UserPlaylist,
    UserPlaylistItem,
)
from app.schemas import PlaybackStateOut, TrackOut
from app.ws import hub


router = APIRouter(prefix="/api", tags=["queue"])


_UNSET: Any = object()


def _get_room(db: Session, room_id: int) -> Room:
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
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


async def _resolve_audio_url(db: Session, track: Track, *, force: bool = False) -> Optional[str]:
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
        audio_url = await _resolve_bilibili_audio(track.source_track_id)

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


async def _resolve_bilibili_audio(bv: str) -> Optional[str]:
    """Resolve audio stream URL via bilibili's playurl API (no yt-dlp needed)."""
    try:
        async with httpx.AsyncClient(timeout=settings.upstream.bilibili_audio_timeout_s, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }) as client:
            r = await client.get(
                "https://api.bilibili.com/x/web-interface/view",
                params={"bvid": bv},
            )
            r.raise_for_status()
            data = r.json()
            if data.get("code") != 0:
                return None
            vid = data["data"]
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
    _get_room(db, room_id)
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
    _get_room(db, room_id)
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
    _get_room(db, room_id)

    qi, added = _enqueue_track_payload(db, room_id, payload, user.id)
    if not added:
        return {"ok": True, "queue_item_id": qi.id, "already_queued": True}

    pb = db.get(RoomPlaybackState, room_id)
    if pb and pb.current_queue_item_id is None:
        next_id = _pick_next_queue_item_id(db, room_id) or qi.id
        await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
    else:
        await hub.broadcast(room_id, {"type": "queue_updated"})

    return {"ok": True, "queue_item_id": qi.id}


@router.post("/rooms/{room_id}/queue/batch")
async def add_to_queue_batch(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_room(db, room_id)
    items = payload.get("items")
    if not isinstance(items, list):
        raise HTTPException(status_code=400, detail="items must be list")

    seen: set[tuple[str, str]] = set()
    queue_item_ids: list[int] = []
    added_count = 0
    skipped_count = 0
    now = datetime.utcnow()

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
        else:
            skipped_count += 1

    if added_count:
        pb = db.get(RoomPlaybackState, room_id)
        if pb and pb.current_queue_item_id is None:
            next_id = _pick_next_queue_item_id(db, room_id) or queue_item_ids[0]
            await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
        else:
            await hub.broadcast(room_id, {"type": "queue_updated"})

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count,
        "queue_item_ids": queue_item_ids,
    }


@router.post("/rooms/{room_id}/queue/playlist")
async def add_playlist_to_queue(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_room(db, room_id)
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

    queue_item_ids: list[int] = []
    added_count = 0
    skipped_count = 0
    now = datetime.utcnow()

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
        else:
            skipped_count += 1

    if added_count:
        pb = db.get(RoomPlaybackState, room_id)
        if pb and pb.current_queue_item_id is None:
            next_id = _pick_next_queue_item_id(db, room_id) or queue_item_ids[0]
            await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
        else:
            await hub.broadcast(room_id, {"type": "queue_updated"})

    return {
        "ok": True,
        "added": added_count,
        "skipped": skipped_count,
        "queue_item_ids": queue_item_ids,
    }


@router.delete("/rooms/{room_id}/queue/{queue_item_id}")
async def remove_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_room(db, room_id)
    qi = db.get(RoomQueueItem, queue_item_id)
    if not qi or qi.room_id != room_id:
        raise HTTPException(status_code=404, detail="queue item not found")
    qi.status = QueueStatus.removed
    db.add(qi)
    db.commit()
    await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.post("/rooms/{room_id}/queue/{queue_item_id}/bump")
async def bump_queue_item(room_id: int, queue_item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_room(db, room_id)
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
                if tr.source == TrackSource.bilibili and tr.audio_url:
                    current_track["audio_url"] = f"/api/tracks/{tr.id}/stream"
    await hub.broadcast(
        room_id,
        {
            "type": "playback_updated",
            "room_id": room_id,
            "playback_state": PlaybackStateOut.model_validate(pb).model_dump(mode="json"),
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
async def pause(room_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pos = None
    try:
        body = await request.json()
        if isinstance(body, dict):
            pos = body.get("position_ms")
            if pos is not None:
                pos = max(0, int(pos))
    except Exception:
        pass
    if pos is None:
        pb = db.get(RoomPlaybackState, room_id)
        if pb and pb.is_playing and pb.current_queue_item_id:
            elapsed_ms = max(0, int((datetime.utcnow() - pb.updated_at).total_seconds() * 1000))
            pos = pb.position_ms + elapsed_ms
    if pos is not None:
        await _set_playback(db, room_id, is_playing=False, position_ms=pos)
    else:
        await _set_playback(db, room_id, is_playing=False)
    return {"ok": True}


@router.post("/rooms/{room_id}/controls/next")
async def next_track(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        raise HTTPException(status_code=404, detail="room not found")
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
    # Minimal behavior: restart current track
    await _set_playback(db, room_id, position_ms=0)
    return {"ok": True}


@router.post("/rooms/{room_id}/queue/shuffle")
async def shuffle_queue(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    _get_room(db, room_id)
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
async def set_position(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _set_playback(db, room_id, position_ms=payload.get("position_ms", 0))
    return {"ok": True}


@router.patch("/rooms/{room_id}/controls/volume")
async def set_volume(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        raise HTTPException(status_code=404, detail="room not found")
    if pb.mode.value != "play_enabled":
        raise HTTPException(status_code=403, detail="volume only allowed in play_enabled mode")
    await _set_playback(db, room_id, volume=payload.get("volume", settings.rooms.default_volume))
    return {"ok": True}

