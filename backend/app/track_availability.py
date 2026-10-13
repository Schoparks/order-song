"""Resolver-backed track availability; transient failures never prove removal."""
import asyncio
import math
from datetime import datetime

from fastapi import HTTPException
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlmodel import Session, select
from sqlalchemy import or_, update

from app.db import engine
from app.models import RoomPlaybackState, RoomQueueItem, Track, TrackSource
from app.routers.playback_audio import AudioResolveResult, _resolve_bilibili_audio, _resolve_netease_audio, _resolve_qq_audio
from app.schemas import TrackOut

_CHECKS: dict[tuple[str, str], asyncio.Task] = {}
_PAUSES: dict[int, asyncio.Task] = {}
_CHECK_LIMIT = asyncio.Semaphore(4)
def begin_resolution(track_id: int | None) -> datetime:
    # Persist request start time, so an older response cannot overwrite a newer
    # decision even when two worker transactions race or another process resolves.
    return datetime.utcnow()


def clear_invalid_loudness_metadata(track: Track) -> bool:
    try:
        valid = track.loudness_source == f"{track.source.value}:ffmpeg-ebur128" and math.isfinite(float(track.loudness_gain_db))
    except (ValueError, TypeError):
        valid = False
    if not track.loudness_source or valid:
        return False
    track.loudness_gain_db = track.loudness_peak = track.loudness_source = track.loudness_fetched_at = None
    return True


def availability_fields(track: Track) -> dict:
    return {
        "availability": track.availability,
        "unavailable_reason": track.unavailable_reason,
        "availability_checked_at": track.availability_checked_at,
    }


def track_summary(track: Track) -> dict:
    return TrackOut.model_validate(track).model_dump(mode="json", exclude={"audio_url"})


def require_available(track: Track) -> None:
    if track.availability == "unavailable":
        raise HTTPException(status_code=409, detail={
            "code": "track_unavailable",
            "message": "该歌曲已失效，请重新搜索替换或移出所有歌单",
            "track": track_summary(track),
        })


def apply_resolution(track: Track, result: AudioResolveResult, *, force: bool = False) -> None:
    track.availability_checked_at = datetime.utcnow()
    if result.unavailable_reason:
        track.availability = "unavailable"
        track.unavailable_reason = result.unavailable_reason
        track.audio_url = None
    elif result.audio_url:
        track.availability = "available"
        track.unavailable_reason = None
        track.audio_url = result.audio_url
    elif track.availability != "unavailable":
        track.availability = "unknown"
        track.unavailable_reason = None
        if force:
            track.audio_url = None


def persist_resolution(track_id: int | None, result: AudioResolveResult, force: bool, generation: datetime | None = None, *, db_engine=None) -> None:
    if track_id is None:
        return
    checked_at = generation or datetime.utcnow()
    with Session(db_engine or engine) as db:
        track = db.get(Track, track_id)
        if not track:
            return
        db.expunge(track)
        apply_resolution(track, result, force=force)
        cleared = clear_invalid_loudness_metadata(track)
        values = {key: getattr(track, key) for key in ("availability", "unavailable_reason", "audio_url")}
        if cleared:
            values.update(loudness_gain_db=None, loudness_peak=None, loudness_source=None, loudness_fetched_at=None)
        values["availability_checked_at"] = checked_at
        db.execute(update(Track).where(Track.id == track_id, or_(Track.availability_checked_at.is_(None), Track.availability_checked_at <= checked_at)).values(**values))
        db.commit()


def _read_or_create(payload: dict) -> Track:
    with Session(engine) as db:
        statement = sqlite_insert(Track).values(**payload).on_conflict_do_nothing(index_elements=["source", "source_track_id"])
        db.execute(statement)
        db.commit()
        track = db.exec(select(Track).where(Track.source == payload["source"], Track.source_track_id == payload["source_track_id"])).one()
        db.expunge(track)
        return track


def _read_track(track_id: int) -> Track:
    with Session(engine) as db:
        track = db.get(Track, track_id)
        if not track:
            raise HTTPException(status_code=404, detail="track not found")
        db.expunge(track)
        return track


async def check_track(track: Track, *, force: bool = False) -> Track:
    age = (datetime.utcnow() - track.availability_checked_at).total_seconds() if track.availability_checked_at else None
    if not force and (track.availability == "unavailable" or (age is not None and age < (600 if track.availability == "available" else 30))):
        return track
    key = (track.source.value, track.source_track_id)
    existing = _CHECKS.get(key)
    if existing and not existing.done():
        return await asyncio.shield(existing)

    async def run() -> Track:
        async with _CHECK_LIMIT:
            generation = begin_resolution(track.id)
            if track.source == TrackSource.bilibili:
                result = await _resolve_bilibili_audio(track.source_track_id)
            elif track.source == TrackSource.netease:
                result = await _resolve_netease_audio(track.source_track_id)
            elif track.source == TrackSource.qq:
                result = await _resolve_qq_audio(track.source_track_id, force=force)
            else:
                result = AudioResolveResult()
            await asyncio.to_thread(persist_resolution, track.id, result, True, generation)
            checked = await asyncio.to_thread(_read_track, track.id)
            schedule_unavailable_pause(checked)
            return checked

    task = asyncio.create_task(run())
    _CHECKS[key] = task
    task.add_done_callback(lambda done: _CHECKS.pop(key, None) if _CHECKS.get(key) is done else None)
    return await asyncio.shield(task)


async def check_inputs(inputs: list) -> tuple[list, list[dict]]:
    async def check(item):
        track = await asyncio.to_thread(_read_or_create, item.model_dump())
        return await check_track(track)
    tracks = await asyncio.gather(*(check(item) for item in inputs))
    valid, unavailable = [], []
    seen = set()
    for item, track in zip(inputs, tracks):
        if track.availability != "unavailable":
            valid.append(item)
        elif track.id not in seen:
            seen.add(track.id)
            unavailable.append(track_summary(track))
    return valid, unavailable


def schedule_unavailable_pause(track: Track) -> None:
    if track.availability != "unavailable" or track.id in _PAUSES:
        return
    track_id = track.id

    async def pause() -> None:
        # Lazy import avoids coupling the shared resolver to room orchestration.
        from app.routers import playback_core as core
        with Session(engine) as db:
            room_ids = db.exec(select(RoomPlaybackState.room_id).join(RoomQueueItem, RoomQueueItem.id == RoomPlaybackState.current_queue_item_id).where(RoomQueueItem.track_id == track_id)).all()
        for room_id in room_ids:
            async with core._playback_lock(room_id):
                with Session(engine) as db:
                    pb = db.get(RoomPlaybackState, room_id)
                    qi = db.get(RoomQueueItem, pb.current_queue_item_id) if pb and pb.current_queue_item_id else None
                    current = db.get(Track, track_id)
                    if qi and qi.track_id == track_id and current and current.availability == "unavailable":
                        core._ROOM_LOUDNESS_WAITING.pop(room_id, None)
                        if pb.is_playing:
                            await core._set_playback(db, room_id, is_playing=False, position_ms=0)

    task = asyncio.create_task(pause())
    _PAUSES[track_id] = task
    def done(completed):
        _PAUSES.pop(track_id, None)
        if not completed.cancelled():
            completed.exception()  # A later snapshot retries if the room write failed.
    task.add_done_callback(done)


def annotate_search_results(items: list) -> list:
    """One batched lookup also annotates collection parts without upstream work."""
    from sqlalchemy import tuple_
    candidates = [candidate for item in items for candidate in [item, *item.parts]]
    keys = {(item.source, item.source_track_id) for item in candidates}
    if not keys:
        return items
    with Session(engine) as db:
        tracks = db.exec(select(Track).where(tuple_(Track.source, Track.source_track_id).in_(keys))).all()
        saved = {(track.source, track.source_track_id): track for track in tracks}
        for item in candidates:
            track = saved.get((item.source, item.source_track_id))
            if track:
                item.id = track.id
                for key, value in availability_fields(track).items():
                    setattr(item, key, value)
    return items
