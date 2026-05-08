import asyncio
import subprocess
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from app.audio_loudness import analyze_remote_audio_loudness, is_loudness_analysis_available
from app.bilibili import bilibili_page_url
from app.core.config import settings
from app.db import engine
from app.models import QueueStatus, RoomMode, RoomPlaybackState, RoomQueueItem, Track, TrackSource, User
from app.routers.playback_audio import (
    AudioResolveResult,
    _clamp_loudness_gain_db,
    _direct_playback_audio_url,
    _ensure_bilibili_metadata,
    _is_bilibili_browser_direct_url,
    _resolve_bilibili_audio,
    _resolve_netease_audio,
)
from app.schemas import PlaybackControlIn, PlaybackStateOut, TrackOut
from app.ws import hub


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
_BACKEND_LOUDNESS_SOURCES = {TrackSource.netease, TrackSource.bilibili}
_UNTRUSTED_LOUDNESS_SOURCES = {"netease:player-url"}


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
    if track.loudness_source in _UNTRUSTED_LOUDNESS_SOURCES:
        return False
    return track.loudness_gain_db is not None or track.loudness_peak is not None or bool(track.loudness_source)


def _clear_untrusted_loudness_metadata(track: Track) -> bool:
    if track.loudness_source not in _UNTRUSTED_LOUDNESS_SOURCES:
        return False
    track.loudness_gain_db = None
    track.loudness_peak = None
    track.loudness_source = None
    track.loudness_fetched_at = None
    return True


def _track_needs_backend_loudness(track: Track) -> bool:
    return track.source in _BACKEND_LOUDNESS_SOURCES and not _track_has_loudness(track) and not _recent_loudness_error(track)


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


def _loudness_analysis_headers(source: TrackSource) -> dict[str, str]:
    headers = {"User-Agent": "Mozilla/5.0"}
    if source == TrackSource.bilibili:
        headers["Referer"] = "https://www.bilibili.com/"
    elif source == TrackSource.netease:
        headers["Referer"] = "https://music.163.com/"
    return headers


def _schedule_loudness_analysis(track_id: int | None, source: TrackSource, analysis_audio_url: str | None, *, force: bool = False) -> None:
    if not track_id or not analysis_audio_url:
        return
    if source not in _BACKEND_LOUDNESS_SOURCES:
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
                    headers=_loudness_analysis_headers(source),
                    source=f"{source.value}:ffmpeg-ebur128",
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


async def _ensure_playback_audio_url(db: Session, track: Track) -> None:
    if _clear_untrusted_loudness_metadata(track):
        db.add(track)
        db.commit()
        db.refresh(track)

    if track.audio_url:
        if track.source == TrackSource.bilibili and not _is_bilibili_browser_direct_url(track.audio_url):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.bilibili and track.loudness_source is None and not _recent_loudness_error(track):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.netease and _track_needs_backend_loudness(track):
            _schedule_loudness_analysis(track.id, track.source, track.audio_url, force=True)
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
    if not _track_has_loudness(track):
        payload["loudness_gain_db"] = None
        payload["loudness_peak"] = None
        payload["loudness_source"] = None
    return payload


async def _resolve_audio_url(db: Session, track: Track, *, force: bool = False) -> Optional[str]:
    video: dict[str, Any] | None = None
    if track.source == TrackSource.bilibili and not track.cover_url:
        video = await _ensure_bilibili_metadata(db, track)

    if track.audio_url and not force:
        return track.audio_url

    if track.source == TrackSource.netease:
        result = await _resolve_netease_audio(track.source_track_id)
        track.audio_url = result.audio_url or f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
        _clear_untrusted_loudness_metadata(track)
        db.add(track)
        db.commit()
        db.refresh(track)
        if _track_needs_backend_loudness(track):
            _schedule_loudness_analysis(track.id, track.source, result.analysis_audio_url or track.audio_url, force=force)
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
                _schedule_loudness_analysis(track.id, track.source, result.analysis_audio_url, force=force)
        return track.audio_url

    return None


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
    current_queue_item_id: Any = _UNSET,
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


async def _set_normalizer_preference_for_user(db: Session, room_id: int, user_id: int, enabled_requested: bool) -> dict[str, Any]:
    enabled = enabled_requested and is_loudness_analysis_available()
    key = (room_id, user_id)
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
