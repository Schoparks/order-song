import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from sqlmodel import Session, select

from app.audio_loudness import analyze_remote_audio_loudness, is_loudness_analysis_available
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
_LOUDNESS_RECOVERY_TASKS: dict[tuple[int, int], asyncio.Task] = {}
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
logger = logging.getLogger(__name__)


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
                    trusted_source=source.value,
                )
            await asyncio.to_thread(
                _persist_loudness_result_transaction,
                track_id,
                result.analysis.gain_db if result.analysis else None,
                result.analysis.peak if result.analysis else None,
                result.analysis.source if result.analysis else None,
                result.error,
                force,
            )
        except asyncio.CancelledError:
            logger.info("loudness analysis cancelled for track %s", track_id)
            raise
        except Exception as exc:
            logger.exception("loudness analysis failed unexpectedly for track %s", track_id)
            try:
                await asyncio.to_thread(
                    _persist_loudness_exception_transaction,
                    track_id,
                    f"unexpected analysis failure: {exc}",
                )
            except Exception:
                logger.exception("failed to persist loudness error for track %s", track_id)
        finally:
            try:
                await asyncio.shield(_handle_loudness_analysis_finished(track_id))
            except Exception:
                logger.exception("failed to release loudness wait for track %s", track_id)
            finally:
                _LOUDNESS_ANALYSIS_TASKS.pop(track_id, None)

    _LOUDNESS_ANALYSIS_TASKS[track_id] = asyncio.create_task(_run())


def _persist_loudness_result_transaction(
    track_id: int,
    gain_db: float | None,
    peak: float | None,
    source: str | None,
    error: str | None,
    force: bool,
) -> None:
    with Session(engine) as write_db:
        track = write_db.get(Track, track_id)
        if not track:
            return
        if source is not None:
            track.loudness_gain_db = _clamp_loudness_gain_db(gain_db)
            track.loudness_peak = peak
            track.loudness_source = source
            track.loudness_error = None
            track.loudness_fetched_at = datetime.utcnow()
        elif error and (force or not _recent_loudness_error(track)):
            _store_loudness_error(track, error)
        else:
            return
        write_db.add(track)
        write_db.commit()


def _persist_loudness_exception_transaction(track_id: int, error: str) -> None:
    with Session(engine) as write_db:
        track = write_db.get(Track, track_id)
        if not track:
            return
        _store_loudness_error(track, error)
        write_db.add(track)
        write_db.commit()


def _clear_untrusted_loudness_transaction(track_id: int) -> None:
    with Session(engine) as write_db:
        track = write_db.get(Track, track_id)
        if track and _clear_untrusted_loudness_metadata(track):
            write_db.add(track)
            write_db.commit()


async def _ensure_playback_audio_url(db: Session, track: Track) -> None:
    if track.id and track.loudness_source in _UNTRUSTED_LOUDNESS_SOURCES:
        db.rollback()
        await asyncio.to_thread(_clear_untrusted_loudness_transaction, track.id)
        db.expire_all()
        db.refresh(track)

    if track.audio_url:
        if track.source == TrackSource.bilibili and (
            not _is_bilibili_browser_direct_url(track.audio_url)
            or _direct_playback_audio_url(track) != track.audio_url
        ):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.bilibili and track.loudness_source is None and not _recent_loudness_error(track):
            await _resolve_audio_url(db, track, force=True)
            return
        if track.source == TrackSource.netease and _direct_playback_audio_url(track) != track.audio_url:
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
        track_id = track.id
        source_track_id = track.source_track_id
        db.rollback()
        result = await _resolve_netease_audio(source_track_id)
        await asyncio.to_thread(_persist_audio_resolution_transaction, track_id, result, force)
        db.expire_all()
        db.refresh(track)
        if _track_needs_backend_loudness(track):
            _schedule_loudness_analysis(track.id, track.source, result.analysis_audio_url or track.audio_url, force=force)
        return track.audio_url

    if track.source == TrackSource.bilibili:
        if video is None:
            video = await _ensure_bilibili_metadata(db, track)
        track_id = track.id
        source_track_id = track.source_track_id
        db.rollback()
        result = await _resolve_bilibili_audio(source_track_id, video_data=video)
        await asyncio.to_thread(_persist_audio_resolution_transaction, track_id, result, force)
        db.expire_all()
        db.refresh(track)
        if not _result_has_loudness(result) and (force or not _recent_loudness_error(track)):
            _schedule_loudness_analysis(track.id, track.source, result.analysis_audio_url, force=force)
        return track.audio_url

    return None


def _persist_audio_resolution_transaction(
    track_id: int | None,
    result: AudioResolveResult,
    force: bool,
) -> None:
    if not track_id:
        return
    with Session(engine) as write_db:
        track = write_db.get(Track, track_id)
        if not track:
            return
        if track.source == TrackSource.netease:
            track.audio_url = result.audio_url or f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
            _clear_untrusted_loudness_metadata(track)
            write_db.add(track)
            write_db.commit()
            return
        if track.source != TrackSource.bilibili:
            return
        if force or result.audio_url or _result_has_loudness(result):
            track.audio_url = result.audio_url
            _store_loudness_metadata(track, result)
            write_db.add(track)
            write_db.commit()


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


def _hold_for_loudness_transaction(room_id: int, queue_item_id: int) -> bool:
    with Session(engine) as write_db:
        pb = write_db.get(RoomPlaybackState, room_id)
        if not pb or pb.current_queue_item_id != queue_item_id or not pb.is_playing:
            return False
        pb.is_playing = False
        pb.position_ms = 0
        pb.updated_at = datetime.utcnow()
        write_db.add(pb)
        write_db.commit()
        return True


async def _prepare_current_track_for_playback(db: Session, room_id: int, pb: RoomPlaybackState, qi: RoomQueueItem | None, tr: Track | None) -> bool:
    """Prepare one expected queue item, returning False if it changed across an await."""
    expected_queue_item_id = qi.id if qi else None
    room_prefers_loudness_wait = _room_prefers_loudness_wait(room_id)
    if tr and (pb.mode == RoomMode.play_enabled or room_prefers_loudness_wait):
        await _ensure_playback_audio_url(db, tr)
        # End any read snapshot held across the upstream request, then re-read the
        # authoritative playback row before applying pause/wait state.
        db.rollback()
        db.expire_all()
        pb = db.get(RoomPlaybackState, room_id)
        if not pb or pb.current_queue_item_id != expected_queue_item_id:
            _clear_loudness_wait_if_changed(room_id, pb.current_queue_item_id if pb else None)
            return False
        qi = db.get(RoomQueueItem, expected_queue_item_id) if expected_queue_item_id else None
        tr = db.get(Track, qi.track_id) if qi else None
    if not qi or not tr or not tr.id:
        return expected_queue_item_id is None
    if (
        _is_loudness_waiting(room_id, qi.id)
        and tr.id not in _LOUDNESS_ANALYSIS_TASKS
        and not _track_needs_backend_loudness(tr)
    ):
        # Preparation is called both with and without the room lock. Schedule an
        # independent recovery task instead of trying to acquire the same lock
        # recursively or mutating playback without it.
        _schedule_loudness_wait_recovery(room_id, qi.id)
    if not (
        pb.is_playing
        and room_prefers_loudness_wait
        and is_loudness_analysis_available()
        and _track_needs_backend_loudness(tr)
    ):
        return True
    if _effective_position_ms(pb) > 1500:
        return True
    if tr.id not in _LOUDNESS_ANALYSIS_TASKS:
        return True

    db.rollback()
    held = await asyncio.to_thread(_hold_for_loudness_transaction, room_id, qi.id)
    db.expire_all()
    if held:
        _ROOM_LOUDNESS_WAITING[room_id] = qi.id
    return held


async def _handle_loudness_analysis_finished(track_id: int) -> None:
    with Session(engine) as db:
        rows = db.exec(
            select(RoomPlaybackState.room_id, RoomPlaybackState.current_queue_item_id)
            .join(RoomQueueItem, RoomQueueItem.id == RoomPlaybackState.current_queue_item_id)
            .where(RoomQueueItem.track_id == track_id)
        ).all()
    for room_id, queue_item_id in rows:
        if _is_loudness_waiting(room_id, queue_item_id):
            _schedule_loudness_wait_recovery(room_id, queue_item_id)
        else:
            async with _playback_lock(room_id):
                with Session(engine) as db:
                    await _broadcast_playback_snapshot(db, room_id)


def _resume_loudness_wait_transaction(room_id: int, queue_item_id: int) -> str:
    with Session(engine) as write_db:
        pb = write_db.get(RoomPlaybackState, room_id)
        if not pb or pb.current_queue_item_id != queue_item_id:
            return "changed"
        pb.is_playing = True
        pb.position_ms = 0
        pb.updated_at = datetime.utcnow()
        write_db.add(pb)
        write_db.commit()
        return "resumed"


async def _resume_loudness_wait_with_retry(
    room_id: int,
    queue_item_id: int,
    *,
    attempts: int = 4,
) -> bool:
    """Fail open after analysis, retrying transient SQLite contention."""
    for attempt in range(max(1, attempts)):
        async with _playback_lock(room_id):
            if not _is_loudness_waiting(room_id, queue_item_id):
                return True
            try:
                outcome = await asyncio.to_thread(
                    _resume_loudness_wait_transaction,
                    room_id,
                    queue_item_id,
                )
            except Exception:
                logger.exception(
                    "failed to resume room %s after loudness analysis (attempt %s/%s)",
                    room_id,
                    attempt + 1,
                    attempts,
                )
            else:
                _ROOM_LOUDNESS_WAITING.pop(room_id, None)
                if outcome == "resumed":
                    try:
                        with Session(engine) as db:
                            await _broadcast_playback_snapshot(db, room_id)
                    except Exception:
                        logger.exception("failed to broadcast resumed loudness state for room %s", room_id)
                return True
        if attempt + 1 < attempts:
            await asyncio.sleep(min(2 ** attempt, 4))
    # Keep the marker so a later room-state request or normalizer-off request can
    # make another recovery attempt instead of leaving an unobservable pause.
    return False


def _schedule_loudness_wait_recovery(room_id: int, queue_item_id: int) -> asyncio.Task:
    """Start one bounded, lock-owned fail-open recovery attempt for a room."""
    key = (room_id, queue_item_id)
    existing = _LOUDNESS_RECOVERY_TASKS.get(key)
    if existing and not existing.done():
        return existing

    async def _run() -> bool:
        try:
            return await _resume_loudness_wait_with_retry(room_id, queue_item_id)
        finally:
            _LOUDNESS_RECOVERY_TASKS.pop(key, None)

    task = asyncio.create_task(_run())
    _LOUDNESS_RECOVERY_TASKS[key] = task
    return task


async def _broadcast_playback_snapshot(db: Session, room_id: int) -> None:
    db.expire_all()
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        return

    if pb.current_queue_item_id:
        qi = db.get(RoomQueueItem, pb.current_queue_item_id)
        if qi:
            tr = db.get(Track, qi.track_id)
            await _prepare_current_track_for_playback(db, room_id, pb, qi, tr)

    db.rollback()
    db.expire_all()
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        return
    current_track = None
    ordered_by = None
    current_queue_item_id = pb.current_queue_item_id
    if current_queue_item_id:
        qi = db.get(RoomQueueItem, current_queue_item_id)
        if qi:
            user = db.get(User, qi.ordered_by_user_id)
            if user:
                ordered_by = {"id": user.id, "username": user.username}
            track = db.get(Track, qi.track_id)
            if track:
                current_track = _playback_track_payload(track)

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


def _set_playback_transaction(
    room_id: int,
    *,
    is_playing: Optional[bool],
    position_ms: Optional[int],
    volume: Optional[int],
    update_current_queue_item: bool,
    current_queue_item_id: int | None,
) -> None:
    with Session(engine) as write_db:
        try:
            pb = write_db.get(RoomPlaybackState, room_id)
            if not pb:
                pb = RoomPlaybackState(room_id=room_id, volume=settings.rooms.default_volume)
            if update_current_queue_item and current_queue_item_id is not None:
                next_item = write_db.get(RoomQueueItem, current_queue_item_id)
                if not next_item or next_item.room_id != room_id:
                    raise ValueError("current queue item does not belong to room")
            if is_playing is not None:
                pb.is_playing = is_playing
            if position_ms is not None:
                pb.position_ms = max(0, int(position_ms))
            if volume is not None:
                pb.volume = max(0, min(100, int(volume)))
            if update_current_queue_item:
                pb.current_queue_item_id = current_queue_item_id
            pb.updated_at = datetime.utcnow()
            write_db.add(pb)

            if pb.current_queue_item_id:
                playing_items = write_db.exec(
                    select(RoomQueueItem).where(
                        RoomQueueItem.room_id == room_id,
                        RoomQueueItem.status == QueueStatus.playing,
                        RoomQueueItem.id != pb.current_queue_item_id,
                    )
                ).all()
                for item in playing_items:
                    item.status = QueueStatus.played
                    write_db.add(item)
                current_item = write_db.get(RoomQueueItem, pb.current_queue_item_id)
                if current_item:
                    current_item.status = QueueStatus.playing
                    write_db.add(current_item)
            elif update_current_queue_item:
                playing_items = write_db.exec(
                    select(RoomQueueItem).where(
                        RoomQueueItem.room_id == room_id,
                        RoomQueueItem.status == QueueStatus.playing,
                    )
                ).all()
                for item in playing_items:
                    item.status = QueueStatus.played
                    write_db.add(item)
            write_db.commit()
        except BaseException:
            write_db.rollback()
            raise


async def _set_playback(
    db: Session,
    room_id: int,
    *,
    is_playing: Optional[bool] = None,
    position_ms: Optional[int] = None,
    volume: Optional[int] = None,
    current_queue_item_id: Any = _UNSET,
) -> RoomPlaybackState:
    update_current_queue_item = current_queue_item_id is not _UNSET
    next_queue_item_id = current_queue_item_id if update_current_queue_item else None
    db.rollback()
    await asyncio.to_thread(
        _set_playback_transaction,
        room_id,
        is_playing=is_playing,
        position_ms=position_ms,
        volume=volume,
        update_current_queue_item=update_current_queue_item,
        current_queue_item_id=next_queue_item_id,
    )
    if update_current_queue_item:
        _clear_loudness_wait_if_changed(room_id, next_queue_item_id)
    db.expire_all()
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        raise RuntimeError("playback transaction did not persist state")
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
            recovery_needed = False
            async with _playback_lock(room_id):
                pb = db.get(RoomPlaybackState, room_id)
                if pb and _is_loudness_waiting(room_id, pb.current_queue_item_id):
                    queue_item_id = pb.current_queue_item_id
                    db.rollback()
                    try:
                        outcome = await asyncio.to_thread(
                            _resume_loudness_wait_transaction,
                            room_id,
                            queue_item_id,
                        )
                    except Exception:
                        logger.exception(
                            "failed to release loudness wait after normalizer was disabled in room %s",
                            room_id,
                        )
                        recovery_needed = True
                    else:
                        _ROOM_LOUDNESS_WAITING.pop(room_id, None)
                        if outcome == "resumed":
                            db.expire_all()
                            await _broadcast_playback_snapshot(db, room_id)
            if recovery_needed:
                _schedule_loudness_wait_recovery(room_id, queue_item_id)
    return {
        "ok": True,
        "enabled": enabled,
        "backend_loudness_available": is_loudness_analysis_available(),
    }
