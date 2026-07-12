import asyncio
from dataclasses import dataclass
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, delete, func, select

from app.core.config import settings
from app.db import engine
from app.deps import get_current_user, get_db
from app.models import QueueStatus, Room, RoomMember, RoomPlaybackState, RoomMode, RoomQueueItem, Track, User
from app.schemas import CreateRoomIn, RoomOut, RoomSummaryOut, SetRoomModeIn
from app.routers.playback_core import (
    _broadcast_playback_snapshot,
    _clear_loudness_wait_if_changed,
    _is_loudness_waiting,
    _prepare_current_track_for_playback,
    _pick_next_queue_item_id,
    _playback_track_payload,
    _playback_lock,
    _playback_state_payload,
)
from app.routers.queue_playback import _require_active_room_member
from app.ws import hub


router = APIRouter(prefix="/api", tags=["rooms"])


def _default_room_name(username: str) -> str:
    return f"{username}的听歌房"


def _room_summary_payloads(db: Session, rooms: list[Room]) -> list[dict]:
    """Build room cards with a fixed number of queries for related data.

    The playback query deliberately selects only presentation metadata.  In
    particular, it must not load or resolve Track.audio_url while rendering a
    room list/card.
    """
    room_ids = [int(room.id) for room in rooms]
    member_names_by_room: dict[int, list[str]] = {room_id: [] for room_id in room_ids}
    now_playing_by_room: dict[int, dict] = {}

    if room_ids:
        member_rows = db.exec(
            select(RoomMember.room_id, User.username)
            .join(User, RoomMember.user_id == User.id)
            .where(RoomMember.room_id.in_(room_ids))
            .order_by(RoomMember.joined_at.asc())
        ).all()
        for room_id, username in member_rows:
            member_names_by_room[int(room_id)].append(username)

        playback_rows = db.exec(
            select(
                RoomPlaybackState.room_id,
                RoomPlaybackState.is_playing,
                Track.title,
                Track.artist,
                Track.source,
                Track.cover_url,
            )
            .join(
                RoomQueueItem,
                RoomPlaybackState.current_queue_item_id == RoomQueueItem.id,
            )
            .join(Track, RoomQueueItem.track_id == Track.id)
            .where(RoomPlaybackState.room_id.in_(room_ids))
        ).all()
        for room_id, is_playing, title, artist, source, cover_url in playback_rows:
            now_playing_by_room[int(room_id)] = {
                "title": title,
                "artist": artist,
                "source": source.value if hasattr(source, "value") else source,
                "cover_url": cover_url,
                "is_playing": bool(is_playing),
            }

    out = []
    for room in rooms:
        room_id = int(room.id)
        members = member_names_by_room[room_id]
        out.append({
            **RoomOut.model_validate(room).model_dump(mode="json"),
            "member_count": len(members),
            "member_names": list(members[: settings.rooms.member_preview_count]),
            "now_playing": now_playing_by_room.get(room_id),
        })
    return out


@router.get("/rooms", response_model=list[RoomSummaryOut])
def list_rooms(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rooms = db.exec(select(Room).order_by(Room.created_at.desc()).limit(settings.rooms.list_limit)).all()
    return _room_summary_payloads(db, list(rooms))


@router.get("/rooms/{room_id}", response_model=RoomSummaryOut)
async def get_room_summary(
    room_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    room = await _require_active_room_member(db, room_id, user)
    return _room_summary_payloads(db, [room])[0]


@router.get("/rooms/{room_id}/members")
async def get_room_members(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    members = db.exec(
        select(User.id, User.username)
        .join(RoomMember, RoomMember.user_id == User.id)
        .where(RoomMember.room_id == room_id)
        .order_by(RoomMember.joined_at.asc())
    ).all()
    return [{"id": m[0], "username": m[1]} for m in members]


@router.post("/rooms", response_model=RoomOut)
def create_room(payload: CreateRoomIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = (payload.name or "").strip() or _default_room_name(user.username)
    try:
        room = Room(name=name, created_by_user_id=user.id)
        db.add(room)
        db.flush()

        # The room, creator membership, playback row, and active-room pointer
        # are one invariant and must become visible atomically.
        db.add(RoomMember(room_id=room.id, user_id=user.id))
        db.add(
            RoomPlaybackState(
                room_id=room.id,
                mode=RoomMode.order_only,
                is_playing=False,
                position_ms=0,
                volume=settings.rooms.default_volume,
            )
        )
        user.last_active_room_id = room.id
        db.add(user)
        db.commit()
        db.refresh(room)
        return RoomOut.model_validate(room)
    except BaseException:
        db.rollback()
        raise


@router.post("/rooms/{room_id}/join")
async def join_room(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    user_id = int(user.id)
    await asyncio.to_thread(_join_room_transaction, room_id, user_id)
    await hub.note_activity(room_id, user.id)
    await hub.broadcast(room_id, {"type": "room_member_joined", "room_id": room_id, "user_id": user.id})
    return {"ok": True}


def _join_room_transaction(room_id: int, user_id: int) -> None:
    with Session(engine) as write_db:
        room = write_db.get(Room, room_id)
        if not room:
            raise HTTPException(status_code=404, detail="room not found")
        persisted_user = write_db.get(User, user_id)
        if not persisted_user:
            raise HTTPException(status_code=401, detail="user not found")
        existing = write_db.exec(
            select(RoomMember).where(
                RoomMember.room_id == room_id,
                RoomMember.user_id == user_id,
            )
        ).first()
        if not existing:
            write_db.add(RoomMember(room_id=room_id, user_id=user_id))
        persisted_user.last_active_room_id = room_id
        write_db.add(persisted_user)
        write_db.commit()


@dataclass(frozen=True)
class _MemberRemovalOutcome:
    destroyed: bool
    was_member: bool


def _remove_member_transaction(
    room_id: int,
    user_id: int,
    *,
    require_room: bool = False,
    require_member: bool = False,
) -> _MemberRemovalOutcome:
    """Perform the complete membership removal in a worker-thread session."""
    with Session(engine) as db:
        try:
            room = db.get(Room, room_id)
            if not room:
                if require_room:
                    raise HTTPException(status_code=404, detail="room not found")
                return _MemberRemovalOutcome(destroyed=False, was_member=False)

            member = db.exec(
                select(RoomMember).where(
                    RoomMember.room_id == room_id,
                    RoomMember.user_id == user_id,
                )
            ).first()
            if not member and require_member:
                raise HTTPException(status_code=404, detail="member not found")
            was_member = member is not None
            if member:
                db.delete(member)
            persisted_user = db.get(User, user_id)
            if persisted_user and persisted_user.last_active_room_id == room_id:
                persisted_user.last_active_room_id = None
                db.add(persisted_user)
            db.flush()

            remaining = db.exec(
                select(func.count())
                .select_from(RoomMember)
                .where(RoomMember.room_id == room_id)
            ).one()
            destroyed = int(remaining) <= 0
            if destroyed:
                delete_room_data(db, room_id)
            db.commit()
            return _MemberRemovalOutcome(destroyed=destroyed, was_member=was_member)
        except BaseException:
            db.rollback()
            raise


async def remove_member_from_room(
    room_id: int,
    user_id: int,
    *,
    require_room: bool = False,
    require_member: bool = False,
) -> bool:
    """Remove a member without blocking the event loop on SQLite writes."""
    outcome = await asyncio.to_thread(
        _remove_member_transaction,
        room_id,
        user_id,
        require_room=require_room,
        require_member=require_member,
    )
    if outcome.destroyed:
        _clear_loudness_wait_if_changed(room_id, None)
        await hub.broadcast(room_id, {"type": "room_destroyed", "room_id": room_id})
        await hub.disconnect_room(room_id)
        return True
    await hub.disconnect_user(room_id, user_id)
    if outcome.was_member:
        await hub.broadcast(room_id, {"type": "room_member_left", "room_id": room_id, "user_id": user_id})
    return False


def delete_room_data(db: Session, room_id: int) -> bool:
    """Stage FK-safe deletion of a room; the caller owns the transaction."""
    room = db.get(Room, room_id)
    if not room:
        return False

    active_users = db.exec(select(User).where(User.last_active_room_id == room_id)).all()
    for active_user in active_users:
        active_user.last_active_room_id = None
        db.add(active_user)

    # Playback references queue items, so remove it before the queue.
    db.exec(delete(RoomPlaybackState).where(RoomPlaybackState.room_id == room_id))
    db.exec(delete(RoomQueueItem).where(RoomQueueItem.room_id == room_id))
    db.exec(delete(RoomMember).where(RoomMember.room_id == room_id))
    db.delete(room)
    return True


@router.get("/rooms/{room_id}/check")
async def check_room(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    room = db.get(Room, room_id)
    if not room:
        return {"exists": False, "is_member": False}
    member = db.exec(select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user.id)).first()
    if member:
        await hub.note_activity(room_id, user.id)
    return {"exists": True, "is_member": bool(member)}


@router.post("/rooms/{room_id}/leave")
async def leave_room(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    destroyed = await remove_member_from_room(room_id, int(user.id))
    return {"ok": True, "destroyed": destroyed}


def _set_room_mode_transaction(
    room_id: int,
    user_id: int,
    mode: RoomMode,
) -> tuple[int | None, bool]:
    """Atomically change mode and establish any required playback state."""
    with Session(engine) as write_db:
        try:
            room = write_db.get(Room, room_id)
            if not room:
                raise HTTPException(status_code=404, detail="room not found")
            member = write_db.exec(
                select(RoomMember.id).where(
                    RoomMember.room_id == room_id,
                    RoomMember.user_id == user_id,
                )
            ).first()
            if member is None:
                raise HTTPException(status_code=403, detail="not a room member")
            pb = write_db.get(RoomPlaybackState, room_id)
            if not pb:
                raise HTTPException(status_code=404, detail="room not found")
            pb.mode = mode
            pb.updated_at = datetime.utcnow()
            write_db.add(pb)

            queue_changed = False
            if mode == RoomMode.play_enabled:
                if pb.current_queue_item_id is None:
                    next_id = _pick_next_queue_item_id(write_db, room_id)
                    if next_id is not None:
                        pb.current_queue_item_id = next_id
                        pb.is_playing = True
                        pb.position_ms = 0
                        queue_changed = True
                elif not pb.is_playing:
                    pb.is_playing = True

                # Match the playback transaction invariant in the same commit:
                # at most one queue item is playing, and it is the PB pointer.
                if pb.current_queue_item_id is not None:
                    playing_items = write_db.exec(
                        select(RoomQueueItem).where(
                            RoomQueueItem.room_id == room_id,
                            RoomQueueItem.status == QueueStatus.playing,
                            RoomQueueItem.id != pb.current_queue_item_id,
                        )
                    ).all()
                    for playing_item in playing_items:
                        playing_item.status = QueueStatus.played
                        write_db.add(playing_item)
                    current_item = write_db.get(
                        RoomQueueItem,
                        pb.current_queue_item_id,
                    )
                    if not current_item or current_item.room_id != room_id:
                        raise RuntimeError("playback queue item does not belong to room")
                    current_item.status = QueueStatus.playing
                    write_db.add(current_item)
            write_db.commit()
            return pb.current_queue_item_id, queue_changed
        except BaseException:
            write_db.rollback()
            raise


@router.patch("/rooms/{room_id}/mode")
async def set_room_mode(room_id: int, payload: SetRoomModeIn, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    async with _playback_lock(room_id):
        current_queue_item_id, queue_changed = await asyncio.to_thread(
            _set_room_mode_transaction,
            room_id,
            int(user.id),
            payload.mode,
        )
        await hub.note_activity(room_id, int(user.id))
        db.rollback()
        db.expire_all()
        if queue_changed:
            _clear_loudness_wait_if_changed(room_id, current_queue_item_id)
        await _broadcast_playback_snapshot(db, room_id)
        if queue_changed:
            await hub.broadcast(room_id, {"type": "queue_updated"})
    return {"ok": True}


@router.get("/rooms/{room_id}/state")
async def room_state(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        raise HTTPException(status_code=404, detail="room not found")
    if pb.current_queue_item_id:
        qi = db.get(RoomQueueItem, pb.current_queue_item_id)
        if qi:
            track = db.get(Track, qi.track_id)
            await _prepare_current_track_for_playback(db, room_id, pb, qi, track)

    # Never hold the playback lock across upstream URL resolution. Preparation
    # uses its own post-await identity check; the short lock below only makes the
    # final authoritative snapshot coherent with play/pause/next mutations.
    async with _playback_lock(room_id):
        db.rollback()
        db.expire_all()
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        current_track = None
        ordered_by = None
        if pb.current_queue_item_id:
            qi = db.get(RoomQueueItem, pb.current_queue_item_id)
            if qi:
                ordered_by_user = db.get(User, qi.ordered_by_user_id)
                if ordered_by_user:
                    ordered_by = {"id": ordered_by_user.id, "username": ordered_by_user.username}
                track = db.get(Track, qi.track_id)
                if track:
                    current_track = _playback_track_payload(track)
        return {
            **_playback_state_payload(pb),
            "current_track": current_track,
            "ordered_by": ordered_by,
            "queue": [],
            "loudness_waiting": _is_loudness_waiting(room_id, pb.current_queue_item_id),
        }
