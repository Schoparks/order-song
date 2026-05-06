import asyncio
import random
import subprocess
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, HTTPException
from starlette.responses import Response
from sqlmodel import Session, select

from app.audio_loudness import analyze_remote_audio_loudness, is_loudness_analysis_available
from app.bilibili import (
    bilibili_page_url,
    find_bilibili_page,
    normalize_bilibili_pic,
    parse_bilibili_source_track_id,
)
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
from app.schemas import PlaybackControlIn, PlaybackStateOut, TrackOut, VolumeControlIn
from app.ws import hub


router = APIRouter(prefix="/api", tags=["queue"])


_UNSET: Any = object()
_PLAYBACK_LOCKS: dict[int, asyncio.Lock] = {}
_AUDIO_URL_RESOLVE_ERRORS: dict[int, float] = {}
_AUDIO_URL_ERROR_RETRY_SECONDS = 30
_LOUDNESS_ANALYSIS_TASKS: dict[int, asyncio.Task] = {}
_LOUDNESS_ERROR_RETRY_SECONDS = 3600
_LOUDNESS_ANALYSIS_SEMAPHORE = asyncio.Semaphore(1)
_LOUDNESS_PREFETCH_TASKS: dict[int, asyncio.Task] = {}
_LOUDNESS_PREFETCH_SEMAPHORE = asyncio.Semaphore(1)
_LOUDNESS_PREFETCH_LAST_AT = 0.0
_ROOM_NORMALIZER_PREFS: dict[tuple[int, int], float] = {}
_ROOM_NORMALIZER_PREF_TTL_SECONDS = 600
_ROOM_LOUDNESS_WAITING: dict[int, int] = {}
_COVER_PROXY_HOST_SUFFIXES = (
    "bilibili.com",
    "biliimg.com",
    "hdslb.com",
)


@dataclass
class AudioResolveResult:
    audio_url: str | None = None
    analysis_audio_url: str | None = None
    loudness_gain_db: float | None = None
    loudness_peak: float | None = None
    loudness_source: str | None = None


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


def _direct_playback_audio_url(track: Track) -> str | None:
    if track.audio_url:
        return track.audio_url
    if track.source == TrackSource.netease:
        return f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
    return None


def _is_bilibili_browser_direct_url(audio_url: str | None) -> bool:
    if not audio_url:
        return False
    path = urlparse(audio_url).path.lower()
    return path.endswith(".mp4")


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (number == number and number not in (float("inf"), float("-inf"))):
        return None
    return number


def _clamp_loudness_gain_db(value: float | None) -> float | None:
    if value is None:
        return None
    return max(-12.0, min(12.0, value))


def _store_loudness_metadata(track: Track, result: AudioResolveResult) -> None:
    if result.loudness_gain_db is None and result.loudness_peak is None:
        return
    track.loudness_gain_db = _clamp_loudness_gain_db(result.loudness_gain_db)
    track.loudness_peak = result.loudness_peak
    track.loudness_source = result.loudness_source
    track.loudness_fetched_at = datetime.utcnow()
    track.loudness_error = None


def _store_loudness_error(track: Track, error: str) -> None:
    track.loudness_error = error[:240]
    track.loudness_fetched_at = datetime.utcnow()


def _result_has_loudness(result: AudioResolveResult) -> bool:
    return result.loudness_gain_db is not None or result.loudness_peak is not None


def _recent_loudness_error(track: Track) -> bool:
    if not track.loudness_error or not track.loudness_fetched_at:
        return False
    age_s = (datetime.utcnow() - track.loudness_fetched_at).total_seconds()
    return age_s < _LOUDNESS_ERROR_RETRY_SECONDS


def _track_has_loudness(track: Track) -> bool:
    return track.loudness_gain_db is not None or track.loudness_peak is not None or bool(track.loudness_source)


def _track_needs_backend_loudness(track: Track) -> bool:
    return track.source == TrackSource.bilibili and not _track_has_loudness(track) and not _recent_loudness_error(track)


def _prune_room_normalizer_prefs(now: float | None = None) -> None:
    current = now or time.monotonic()
    stale = [
        key
        for key, updated_at in _ROOM_NORMALIZER_PREFS.items()
        if current - updated_at > _ROOM_NORMALIZER_PREF_TTL_SECONDS
    ]
    for key in stale:
        _ROOM_NORMALIZER_PREFS.pop(key, None)


def _room_prefers_loudness_wait(room_id: int) -> bool:
    _prune_room_normalizer_prefs()
    return any(stored_room_id == room_id for stored_room_id, _ in _ROOM_NORMALIZER_PREFS)


def _is_loudness_waiting(room_id: int, queue_item_id: int | None) -> bool:
    return bool(queue_item_id and _ROOM_LOUDNESS_WAITING.get(room_id) == queue_item_id)


def _clear_loudness_wait_if_changed(room_id: int, queue_item_id: int | None) -> None:
    if _ROOM_LOUDNESS_WAITING.get(room_id) and _ROOM_LOUDNESS_WAITING.get(room_id) != queue_item_id:
        _ROOM_LOUDNESS_WAITING.pop(room_id, None)


def _schedule_bilibili_loudness_analysis(track_id: int | None, analysis_audio_url: str | None, *, force: bool = False) -> None:
    if not track_id or not analysis_audio_url:
        return
    if not is_loudness_analysis_available():
        return
    existing = _LOUDNESS_ANALYSIS_TASKS.get(track_id)
    if existing and not existing.done():
        return

    async def _run() -> None:
        try:
            async with _LOUDNESS_ANALYSIS_SEMAPHORE:
                result = await analyze_remote_audio_loudness(
                    analysis_audio_url,
                    headers={
                        "User-Agent": "Mozilla/5.0",
                        "Referer": "https://www.bilibili.com/",
                    },
                )
            with Session(engine) as db:
                track = db.get(Track, track_id)
                if not track:
                    return
                if result.analysis:
                    track.loudness_gain_db = _clamp_loudness_gain_db(result.analysis.gain_db)
                    track.loudness_peak = result.analysis.peak
                    track.loudness_source = result.analysis.source
                    track.loudness_error = None
                    track.loudness_fetched_at = datetime.utcnow()
                elif result.error and (force or not _recent_loudness_error(track)):
                    _store_loudness_error(track, result.error)
                db.add(track)
                db.commit()
            await _handle_loudness_analysis_finished(track_id)
        finally:
            _LOUDNESS_ANALYSIS_TASKS.pop(track_id, None)

    _LOUDNESS_ANALYSIS_TASKS[track_id] = asyncio.create_task(_run())


def _bilibili_loudness_from_playurl(data: dict[str, Any] | None) -> AudioResolveResult:
    volume = (data or {}).get("volume")
    if not isinstance(volume, dict):
        return AudioResolveResult()
    measured_i = _finite_float(volume.get("measured_i"))
    target_i = _finite_float(volume.get("target_i"))
    target_offset = _finite_float(volume.get("target_offset"))
    measured_tp = _finite_float(volume.get("measured_tp"))
    gain_db = None
    if measured_i is not None and target_i is not None:
        gain_db = target_i - measured_i
    elif target_offset is not None:
        gain_db = target_offset
    peak = None
    if measured_tp is not None:
        # true peak is normally reported in dBTP.
        peak = 10 ** (measured_tp / 20)
    return AudioResolveResult(
        loudness_gain_db=_clamp_loudness_gain_db(gain_db),
        loudness_peak=peak,
        loudness_source="bilibili:playurl-volume",
    )


async def _ensure_playback_audio_url(db: Session, track: Track) -> None:
    if track.audio_url:
        if track.source == TrackSource.bilibili and not _is_bilibili_browser_direct_url(track.audio_url):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.bilibili and track.loudness_source is None and not _recent_loudness_error(track):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.netease and track.loudness_source is None:
            await _resolve_audio_url(db, track, force=True)
            return
        if track.id:
            _AUDIO_URL_RESOLVE_ERRORS.pop(track.id, None)
        return
    if track.source in (TrackSource.netease, TrackSource.bilibili):
        if track.source == TrackSource.bilibili and track.id:
            last_error_at = _AUDIO_URL_RESOLVE_ERRORS.get(track.id)
            if last_error_at and time.monotonic() - last_error_at < _AUDIO_URL_ERROR_RETRY_SECONDS:
                return
        await _resolve_audio_url(db, track)
        if track.source == TrackSource.bilibili and track.id:
            if track.audio_url:
                _AUDIO_URL_RESOLVE_ERRORS.pop(track.id, None)
            else:
                _AUDIO_URL_RESOLVE_ERRORS[track.id] = time.monotonic()


def _playback_track_payload(track: Track) -> dict[str, Any]:
    payload = TrackOut.model_validate(track).model_dump(mode="json")
    payload["audio_url"] = _direct_playback_audio_url(track)
    return payload


def _record_track_order(db: Session, track_id: int, ordered_at: datetime) -> None:
    stats = db.get(TrackOrderStats, track_id)
    if not stats:
        stats = TrackOrderStats(track_id=track_id, order_count=0, last_ordered_at=ordered_at)
    stats.order_count += 1
    stats.last_ordered_at = ordered_at
    db.add(stats)


async def _fetch_bilibili_video_metadata(source_track_id: str) -> dict[str, Any] | None:
    ref = parse_bilibili_source_track_id(source_track_id)
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
                params={"bvid": ref.bvid},
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

    ref = parse_bilibili_source_track_id(track.source_track_id)
    page = find_bilibili_page(video, ref)
    changed = False
    cover_url = normalize_bilibili_pic(video.get("pic"))
    owner = video.get("owner") or {}
    video_duration = video.get("duration")
    page_duration = page.get("duration") if page else None
    target_duration = page_duration if ref.is_part and page_duration is not None else video_duration
    video_duration_ms = int(video_duration) * 1000 if isinstance(video_duration, (int, float)) else None
    target_duration_ms = int(target_duration) * 1000 if isinstance(target_duration, (int, float)) else None
    part_title = str(page.get("part") or "").strip() if page else ""

    if cover_url and not track.cover_url:
        track.cover_url = cover_url
        changed = True
    if isinstance(owner, dict) and owner.get("name") and not track.artist:
        track.artist = owner["name"]
        changed = True
    if target_duration_ms and (not track.duration_ms or (ref.is_part and track.duration_ms == video_duration_ms)):
        track.duration_ms = target_duration_ms
        changed = True
    if ref.is_part and part_title and (
        not track.title
        or track.title == track.source_track_id
        or track.title == video.get("title")
    ):
        track.title = part_title
        changed = True
    elif video.get("title") and (not track.title or track.title == track.source_track_id):
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
            _schedule_next_loudness_prefetch(db, room_id)
            await hub.broadcast(room_id, {"type": "queue_updated"})


async def _resolve_audio_url(db: Session, track: Track, *, force: bool = False) -> Optional[str]:
    video: dict[str, Any] | None = None
    if track.source == TrackSource.bilibili and not track.cover_url:
        video = await _ensure_bilibili_metadata(db, track)

    if track.audio_url and not force:
        return track.audio_url

    if track.source == TrackSource.netease:
        result = await _resolve_netease_audio(track.source_track_id)
        track.audio_url = result.audio_url or f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
        _store_loudness_metadata(track, result)
        db.add(track)
        db.commit()
        db.refresh(track)
        return track.audio_url

    if track.source == TrackSource.bilibili:
        if video is None:
            video = await _ensure_bilibili_metadata(db, track)
        result = await _resolve_bilibili_audio(track.source_track_id, video_data=video)
        audio_url = result.audio_url

        if not audio_url:
            page_url = bilibili_page_url(track.source_track_id)

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
            _store_loudness_metadata(track, result)
            db.add(track)
            db.commit()
            db.refresh(track)
            if not _result_has_loudness(result) and (force or not _recent_loudness_error(track)):
                _schedule_bilibili_loudness_analysis(track.id, result.analysis_audio_url, force=force)
        return track.audio_url

    return None


async def _resolve_netease_audio(source_track_id: str) -> AudioResolveResult:
    try:
        async with httpx.AsyncClient(
            timeout=settings.upstream.netease_playlist_timeout_s,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
            follow_redirects=True,
        ) as client:
            r = await client.get(
                "https://music.163.com/api/song/enhance/player/url/v1",
                params={"ids": f"[{source_track_id}]", "level": "standard", "encodeType": "aac"},
            )
            r.raise_for_status()
            data = r.json()
            items = data.get("data") if isinstance(data, dict) else None
            item = items[0] if isinstance(items, list) and items and isinstance(items[0], dict) else {}
            gain_db = _finite_float(item.get("gain"))
            peak = _finite_float(item.get("peak"))
            return AudioResolveResult(
                audio_url=item.get("url") if isinstance(item.get("url"), str) else None,
                loudness_gain_db=_clamp_loudness_gain_db(gain_db),
                loudness_peak=peak,
                loudness_source="netease:player-url" if gain_db is not None or peak is not None else None,
            )
    except Exception:
        return AudioResolveResult()


async def _resolve_bilibili_audio(source_track_id: str, *, video_data: dict[str, Any] | None = None) -> AudioResolveResult:
    """Resolve a browser-direct Bilibili media URL, preferring no-referrer-friendly html5 durl mp4."""
    ref = parse_bilibili_source_track_id(source_track_id)
    try:
        async with httpx.AsyncClient(timeout=settings.upstream.bilibili_audio_timeout_s, headers={
            "User-Agent": "Mozilla/5.0",
            "Referer": "https://www.bilibili.com/",
        }) as client:
            vid = video_data
            if not vid:
                r = await client.get(
                    "https://api.bilibili.com/x/web-interface/view",
                    params={"bvid": ref.bvid},
                )
                r.raise_for_status()
                data = r.json()
                if data.get("code") != 0:
                    return AudioResolveResult()
                vid = data["data"]
            if not vid:
                return AudioResolveResult()
            page = find_bilibili_page(vid, ref)
            cid = ref.cid or (page.get("cid") if page else None) or vid.get("cid")
            if not cid:
                return AudioResolveResult()

            async def _playurl(params: dict[str, Any]) -> dict[str, Any] | None:
                r = await client.get("https://api.bilibili.com/x/player/playurl", params=params)
                r.raise_for_status()
                data = r.json()
                if data.get("code") != 0:
                    return None
                return data.get("data") or None

            html5 = await _playurl(
                {
                    "bvid": ref.bvid,
                    "cid": cid,
                    "qn": 16,
                    "fnval": 16,
                    "fnver": 0,
                    "platform": "html5",
                    "high_quality": 1,
                }
            )
            loudness = _bilibili_loudness_from_playurl(html5)
            playback_url = None
            durl = (html5 or {}).get("durl") or []
            for item in durl:
                direct_url = item.get("url") if isinstance(item, dict) else None
                if direct_url:
                    playback_url = direct_url
                    break
            if playback_url and _result_has_loudness(loudness):
                loudness.audio_url = playback_url
                return loudness

            dash_data = await _playurl({"bvid": ref.bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1})
            if not _result_has_loudness(loudness):
                loudness = _bilibili_loudness_from_playurl(dash_data)
            dash = (dash_data or {}).get("dash")
            if not dash:
                if playback_url:
                    loudness.audio_url = playback_url
                    return loudness
                return AudioResolveResult()
            audio_list = dash.get("audio") or []
            if not audio_list:
                if playback_url:
                    loudness.audio_url = playback_url
                    return loudness
                return AudioResolveResult()
            aac = [a for a in audio_list if "mp4a" in (a.get("codecs") or "")]
            candidates = aac if aac else audio_list
            candidates.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            loudness.analysis_audio_url = candidates[0].get("baseUrl") or candidates[0].get("base_url")
            loudness.audio_url = playback_url or loudness.analysis_audio_url
            return loudness
    except Exception:
        return AudioResolveResult()


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
    return {
        "track_id": track_id,
        "audio_url": _direct_playback_audio_url(tr),
        "loudness_gain_db": tr.loudness_gain_db,
        "loudness_peak": tr.loudness_peak,
        "loudness_source": tr.loudness_source,
        "loudness_error": tr.loudness_error,
    }


@router.patch("/rooms/{room_id}/normalizer-preference")
async def set_normalizer_preference(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    enabled = payload.get("enabled") is True and is_loudness_analysis_available()
    key = (room_id, user.id)
    if enabled:
        _ROOM_NORMALIZER_PREFS[key] = time.monotonic()
        _schedule_next_loudness_prefetch(db, room_id)
    else:
        _ROOM_NORMALIZER_PREFS.pop(key, None)
        if not _room_prefers_loudness_wait(room_id) and _ROOM_LOUDNESS_WAITING.get(room_id):
            async with _playback_lock(room_id):
                pb = db.get(RoomPlaybackState, room_id)
                if pb and _is_loudness_waiting(room_id, pb.current_queue_item_id):
                    _ROOM_LOUDNESS_WAITING.pop(room_id, None)
                    await _set_playback(db, room_id, is_playing=True, position_ms=0)
    return {
        "ok": True,
        "enabled": enabled,
        "backend_loudness_available": is_loudness_analysis_available(),
    }


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


def _schedule_track_loudness_prefetch(track_id: int | None) -> None:
    if not track_id or not is_loudness_analysis_available():
        return
    existing = _LOUDNESS_PREFETCH_TASKS.get(track_id)
    if existing and not existing.done():
        return

    async def _run() -> None:
        global _LOUDNESS_PREFETCH_LAST_AT
        try:
            async with _LOUDNESS_PREFETCH_SEMAPHORE:
                elapsed = time.monotonic() - _LOUDNESS_PREFETCH_LAST_AT
                wait_s = settings.audio_loudness.prefetch_min_interval_s - elapsed
                if wait_s > 0:
                    await asyncio.sleep(wait_s)
                _LOUDNESS_PREFETCH_LAST_AT = time.monotonic()

                with Session(engine) as prefetch_db:
                    track = prefetch_db.get(Track, track_id)
                    if track and _track_needs_backend_loudness(track):
                        await _resolve_audio_url(prefetch_db, track, force=True)
        finally:
            _LOUDNESS_PREFETCH_TASKS.pop(track_id, None)

    _LOUDNESS_PREFETCH_TASKS[track_id] = asyncio.create_task(_run())


def _schedule_next_loudness_prefetch(db: Session, room_id: int) -> None:
    if not is_loudness_analysis_available() or not _room_prefers_loudness_wait(room_id):
        return
    qi = db.exec(
        select(RoomQueueItem)
        .where(RoomQueueItem.room_id == room_id, RoomQueueItem.status == QueueStatus.queued)
        .order_by(RoomQueueItem.created_at.asc())
        .limit(1)
    ).first()
    if not qi:
        return
    track = db.get(Track, qi.track_id)
    if track and _track_needs_backend_loudness(track):
        _schedule_track_loudness_prefetch(track.id)


async def _prepare_current_track_for_playback(db: Session, room_id: int, pb: RoomPlaybackState, qi: RoomQueueItem | None, tr: Track | None) -> None:
    room_prefers_loudness_wait = _room_prefers_loudness_wait(room_id)
    if tr and (pb.mode == RoomMode.play_enabled or room_prefers_loudness_wait):
        await _ensure_playback_audio_url(db, tr)
        db.refresh(tr)
    if not qi or not tr or not tr.id:
        return
    if not (
        pb.is_playing
        and room_prefers_loudness_wait
        and is_loudness_analysis_available()
        and _track_needs_backend_loudness(tr)
    ):
        return
    if _effective_position_ms(pb) > 1500:
        return
    if tr.id not in _LOUDNESS_ANALYSIS_TASKS:
        return

    pb.is_playing = False
    pb.position_ms = 0
    pb.updated_at = datetime.utcnow()
    db.add(pb)
    db.commit()
    db.refresh(pb)
    _ROOM_LOUDNESS_WAITING[room_id] = qi.id


async def _handle_loudness_analysis_finished(track_id: int) -> None:
    with Session(engine) as db:
        rows = db.exec(
            select(RoomPlaybackState.room_id, RoomPlaybackState.current_queue_item_id)
            .join(RoomQueueItem, RoomQueueItem.id == RoomPlaybackState.current_queue_item_id)
            .where(RoomQueueItem.track_id == track_id)
        ).all()
    for room_id, queue_item_id in rows:
        async with _playback_lock(room_id):
            with Session(engine) as db:
                pb = db.get(RoomPlaybackState, room_id)
                if not pb or pb.current_queue_item_id != queue_item_id:
                    _clear_loudness_wait_if_changed(room_id, pb.current_queue_item_id if pb else None)
                    continue
                if _is_loudness_waiting(room_id, queue_item_id):
                    _ROOM_LOUDNESS_WAITING.pop(room_id, None)
                    await _set_playback(db, room_id, is_playing=True, position_ms=0)
                else:
                    await _broadcast_playback_snapshot(db, room_id)


async def _broadcast_playback_snapshot(db: Session, room_id: int) -> None:
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        return

    current_track = None
    ordered_by = None
    current_queue_item_id = pb.current_queue_item_id
    if pb.current_queue_item_id:
        qi = db.get(RoomQueueItem, pb.current_queue_item_id)
        if qi:
            u = db.get(User, qi.ordered_by_user_id)
            if u:
                ordered_by = {"id": u.id, "username": u.username}
            tr = db.get(Track, qi.track_id)
            await _prepare_current_track_for_playback(db, room_id, pb, qi, tr)
            if tr:
                current_track = _playback_track_payload(tr)

    await hub.broadcast(
        room_id,
        {
            "type": "playback_updated",
            "room_id": room_id,
            **_playback_state_payload(pb),
            "current_track": current_track,
            "ordered_by": ordered_by,
            "loudness_waiting": _is_loudness_waiting(room_id, current_queue_item_id),
        },
    )


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
        _clear_loudness_wait_if_changed(room_id, current_queue_item_id)
    pb.updated_at = datetime.utcnow()
    db.add(pb)
    db.commit()
    db.refresh(pb)

    if current_queue_item_id is not _UNSET and old_queue_item_id and old_queue_item_id != pb.current_queue_item_id:
        old_qi = db.get(RoomQueueItem, old_queue_item_id)
        if old_qi and old_qi.status == QueueStatus.playing:
            old_qi.status = QueueStatus.played
            db.add(old_qi)
            db.commit()
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
    await _broadcast_playback_snapshot(db, room_id)
    if current_queue_item_id is not _UNSET:
        await hub.broadcast(room_id, {"type": "queue_updated"})
        _schedule_next_loudness_prefetch(db, room_id)
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
