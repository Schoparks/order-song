import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, delete, select

from app.deps import get_current_user, get_db
from app.db import engine
from app.models import QueueStatus, Room, RoomMember, RoomPlaybackState, RoomQueueItem, User, UserPlaylist, UserPlaylistItem
from app.routers.playback_core import _broadcast_playback_snapshot, _clear_loudness_wait_if_changed
from app.routers.rooms import delete_room_data, remove_member_from_room
from app.schemas import AdminUserUpdateIn
from app.ws import hub


router = APIRouter(prefix="/api/admin", tags=["admin"])


def require_admin(user: User = Depends(get_current_user)) -> User:
    if not user.is_admin:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="admin required")
    return user


@router.get("/users")
def list_users(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    users = db.exec(select(User).order_by(User.created_at.asc())).all()
    return [
        {
            "id": u.id,
            "username": u.username,
            "is_admin": u.is_admin,
            "created_at": u.created_at,
            "last_active_room_id": u.last_active_room_id,
        }
        for u in users
    ]


@router.patch("/users/{user_id}")
def update_user(user_id: int, payload: AdminUserUpdateIn, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    user.is_admin = payload.is_admin
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin}


def _stage_user_deletion(db: Session, user: User) -> tuple[set[int], set[int], set[int], set[int]]:
    """Stage an FK-safe user deletion in the caller's transaction.

    Returns destroyed rooms, membership rooms, queue-changed rooms, and
    playback-changed rooms for post-commit WebSocket notifications.
    """
    user_id = int(user.id)
    created_room_ids = set(
        db.exec(select(Room.id).where(Room.created_by_user_id == user_id)).all()
    )
    membership_room_ids = set(
        db.exec(select(RoomMember.room_id).where(RoomMember.user_id == user_id)).all()
    )
    queue_items = db.exec(
        select(RoomQueueItem).where(RoomQueueItem.ordered_by_user_id == user_id)
    ).all()
    queue_room_ids = {item.room_id for item in queue_items}
    playback_changed_room_ids: set[int] = set()

    for item in queue_items:
        pb = db.get(RoomPlaybackState, item.room_id)
        if pb and pb.current_queue_item_id == item.id:
            pb.current_queue_item_id = None
            pb.is_playing = False
            pb.position_ms = 0
            pb.updated_at = datetime.utcnow()
            db.add(pb)
            playing_items = db.exec(
                select(RoomQueueItem).where(
                    RoomQueueItem.room_id == item.room_id,
                    RoomQueueItem.status == QueueStatus.playing,
                    RoomQueueItem.id != item.id,
                )
            ).all()
            for playing_item in playing_items:
                playing_item.status = QueueStatus.played
                db.add(playing_item)
            playback_changed_room_ids.add(item.room_id)
    db.flush()

    destroyed_room_ids = set(created_room_ids)
    for room_id in created_room_ids:
        delete_room_data(db, room_id)
    db.flush()

    db.exec(delete(RoomQueueItem).where(RoomQueueItem.ordered_by_user_id == user_id))
    db.exec(delete(RoomMember).where(RoomMember.user_id == user_id))
    db.flush()

    for room_id in membership_room_ids - created_room_ids:
        remaining = db.exec(
            select(RoomMember.id).where(RoomMember.room_id == room_id).limit(1)
        ).first()
        if remaining is None and delete_room_data(db, room_id):
            destroyed_room_ids.add(room_id)
    db.flush()

    playlists = db.exec(select(UserPlaylist).where(UserPlaylist.user_id == user_id)).all()
    for playlist in playlists:
        db.exec(delete(UserPlaylistItem).where(UserPlaylistItem.playlist_id == playlist.id))
    db.exec(delete(UserPlaylist).where(UserPlaylist.user_id == user_id))
    db.flush()
    db.delete(user)
    return destroyed_room_ids, membership_room_ids, queue_room_ids, playback_changed_room_ids


def _delete_user_transaction(
    user_id: int,
    admin_id: int,
) -> tuple[set[int], set[int], set[int], set[int]]:
    with Session(engine) as write_db:
        try:
            user = write_db.get(User, user_id)
            if not user:
                raise HTTPException(status_code=404, detail="user not found")
            if user_id == admin_id:
                raise HTTPException(status_code=400, detail="cannot delete yourself")
            result = _stage_user_deletion(write_db, user)
            write_db.commit()
            return result
        except BaseException:
            write_db.rollback()
            raise


@router.delete("/users/{user_id}")
async def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    destroyed_rooms, membership_rooms, queue_rooms, playback_rooms = await asyncio.to_thread(
        _delete_user_transaction,
        user_id,
        int(admin.id),
    )
    db.rollback()
    db.expire_all()

    for room_id in destroyed_rooms:
        _clear_loudness_wait_if_changed(room_id, None)
        await hub.broadcast(room_id, {"type": "room_destroyed", "room_id": room_id})
        await hub.disconnect_room(room_id)
    for room_id in membership_rooms - destroyed_rooms:
        await hub.disconnect_user(room_id, user_id, reason="user account deleted")
        await hub.broadcast(room_id, {"type": "room_member_left", "room_id": room_id, "user_id": user_id})
    for room_id in queue_rooms - destroyed_rooms:
        await hub.broadcast(room_id, {"type": "queue_updated"})
    for room_id in playback_rooms - destroyed_rooms:
        _clear_loudness_wait_if_changed(room_id, None)
        await _broadcast_playback_snapshot(db, room_id)
    return {"ok": True}


def _delete_room_transaction(room_id: int) -> None:
    with Session(engine) as write_db:
        try:
            if not delete_room_data(write_db, room_id):
                raise HTTPException(status_code=404, detail="room not found")
            write_db.commit()
        except BaseException:
            write_db.rollback()
            raise


@router.get("/rooms")
def list_all_rooms(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rooms = db.exec(select(Room).order_by(Room.created_at.desc())).all()
    room_ids = {int(room.id) for room in rooms}
    members_by_room: dict[int, list[dict[str, object]]] = {
        room_id: [] for room_id in room_ids
    }
    if room_ids:
        member_rows = db.exec(
            select(RoomMember.room_id, User.id, User.username)
            .join(User, RoomMember.user_id == User.id)
            .where(RoomMember.room_id.in_(room_ids))
            .order_by(RoomMember.joined_at.asc())
        ).all()
        for room_id, member_id, username in member_rows:
            members_by_room[int(room_id)].append(
                {"id": member_id, "username": username}
            )

    creator_ids = {int(room.created_by_user_id) for room in rooms}
    creator_names = {}
    if creator_ids:
        creator_names = dict(
            db.exec(
                select(User.id, User.username).where(User.id.in_(creator_ids))
            ).all()
        )
    out = []
    for r in rooms:
        out.append({
            "id": r.id,
            "name": r.name,
            "created_by": creator_names.get(
                r.created_by_user_id,
                str(r.created_by_user_id),
            ),
            "created_at": r.created_at,
            "members": members_by_room[int(r.id)],
        })
    return out


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    await asyncio.to_thread(_delete_room_transaction, room_id)
    _clear_loudness_wait_if_changed(room_id, None)
    await hub.broadcast(room_id, {"type": "room_destroyed", "room_id": room_id})
    await hub.disconnect_room(room_id)
    return {"ok": True}


@router.delete("/rooms/{room_id}/members/{user_id}")
async def remove_room_member(room_id: int, user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    destroyed = await remove_member_from_room(
        room_id,
        user_id,
        require_room=True,
        require_member=True,
    )
    return {"ok": True, "destroyed": destroyed}
