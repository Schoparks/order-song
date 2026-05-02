from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, delete, func, select

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.models import Room, RoomMember, RoomPlaybackState, RoomMode, RoomQueueItem, Track, User
from app.schemas import CreateRoomIn, RoomOut
from app.routers.queue_playback import (
    _require_active_room_member,
    _pick_next_queue_item_id,
    _playback_track_payload,
    _playback_lock,
    _playback_state_payload,
    _require_room_member,
    _schedule_track_normalization,
    _set_playback,
    _track_needs_normalization,
)
from app.ws import hub


router = APIRouter(prefix="/api", tags=["rooms"])


def _default_room_name(username: str) -> str:
    return f"{username}的听歌房"


@router.get("/rooms")
def list_rooms(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    rooms = db.exec(select(Room).order_by(Room.created_at.desc()).limit(settings.rooms.list_limit)).all()
    out = []
    for r in rooms:
        members = db.exec(
            select(User.username)
            .join(RoomMember, RoomMember.user_id == User.id)
            .where(RoomMember.room_id == r.id)
            .order_by(RoomMember.joined_at.asc())
        ).all()
        out.append({
            **RoomOut.model_validate(r).model_dump(mode="json"),
            "member_count": len(members),
            "member_names": list(members[: settings.rooms.member_preview_count]),
        })
    return out


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
    room = Room(name=name, created_by_user_id=user.id)
    db.add(room)
    db.commit()
    db.refresh(room)

    # creator auto-joins
    member = RoomMember(room_id=room.id, user_id=user.id)
    db.add(member)
    # initialize playback row
    pb = RoomPlaybackState(room_id=room.id, mode=RoomMode.order_only, is_playing=False, position_ms=0, volume=settings.rooms.default_volume)
    db.add(pb)
    user.last_active_room_id = room.id
    db.add(user)
    db.commit()
    return RoomOut.model_validate(room)


@router.post("/rooms/{room_id}/join")
async def join_room(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    existing = db.exec(select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user.id)).first()
    if not existing:
        db.add(RoomMember(room_id=room_id, user_id=user.id))
    user.last_active_room_id = room_id
    db.add(user)
    db.commit()
    await hub.note_activity(room_id, user.id)
    await hub.broadcast(room_id, {"type": "room_member_joined", "room_id": room_id, "user_id": user.id})
    return {"ok": True}


async def remove_member_from_room(db: Session, room_id: int, user_id: int) -> bool:
    """Remove a member from a room. Destroys the room if empty. Returns True if room was destroyed."""
    room = db.get(Room, room_id)
    if not room:
        return False

    member = db.exec(select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)).first()
    was_member = member is not None
    if member:
        db.delete(member)
    user = db.get(User, user_id)
    if user and user.last_active_room_id == room_id:
        user.last_active_room_id = None
        db.add(user)
    db.commit()

    remaining = db.exec(select(func.count()).select_from(RoomMember).where(RoomMember.room_id == room_id)).one()
    remaining_count = int(remaining)
    if remaining_count <= 0:
        db.exec(delete(RoomPlaybackState).where(RoomPlaybackState.room_id == room_id))
        db.exec(delete(RoomQueueItem).where(RoomQueueItem.room_id == room_id))
        db.exec(delete(RoomMember).where(RoomMember.room_id == room_id))
        db.exec(delete(Room).where(Room.id == room_id))
        db.commit()
        await hub.broadcast(room_id, {"type": "room_destroyed", "room_id": room_id})
        return True
    if was_member:
        await hub.broadcast(room_id, {"type": "room_member_left", "room_id": room_id, "user_id": user_id})
    return False


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
    room = db.get(Room, room_id)
    if not room:
        return {"ok": True}
    destroyed = await remove_member_from_room(db, room_id, user.id)
    return {"ok": True, "destroyed": destroyed}


@router.patch("/rooms/{room_id}/mode")
async def set_room_mode(room_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    mode = payload.get("mode")
    if mode not in (RoomMode.order_only.value, RoomMode.play_enabled.value):
        raise HTTPException(status_code=400, detail="invalid mode")
    async with _playback_lock(room_id):
        pb = db.get(RoomPlaybackState, room_id)
        if not pb:
            raise HTTPException(status_code=404, detail="room not found")
        pb.mode = RoomMode(mode)
        pb.updated_at = datetime.utcnow()
        db.add(pb)
        db.commit()
        db.refresh(pb)

        if pb.mode == RoomMode.play_enabled:
            if pb.current_queue_item_id is None:
                next_id = _pick_next_queue_item_id(db, room_id)
                if next_id is not None:
                    await _set_playback(db, room_id, current_queue_item_id=next_id, is_playing=True, position_ms=0)
                    return {"ok": True}
            elif not pb.is_playing:
                await _set_playback(db, room_id, is_playing=True)
                return {"ok": True}

        await hub.broadcast(
            room_id,
            {
                "type": "playback_updated",
                "room_id": room_id,
                **_playback_state_payload(pb),
            },
        )
    return {"ok": True}


@router.get("/rooms/{room_id}/state")
async def room_state(room_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    await _require_active_room_member(db, room_id, user)
    pb = db.get(RoomPlaybackState, room_id)
    if not pb:
        raise HTTPException(status_code=404, detail="room not found")
    current_track = None
    ordered_by = None
    if pb.current_queue_item_id:
        qi = db.get(RoomQueueItem, pb.current_queue_item_id)
        if qi:
            u = db.get(User, qi.ordered_by_user_id)
            if u:
                ordered_by = {"id": u.id, "username": u.username}
            tr = db.get(Track, qi.track_id)
            if tr:
                current_track = _playback_track_payload(tr)
                if _track_needs_normalization(tr):
                    _schedule_track_normalization(tr.id, room_id)
    return {
        **_playback_state_payload(pb),
        "current_track": current_track,
        "ordered_by": ordered_by,
        "queue": [],
    }
