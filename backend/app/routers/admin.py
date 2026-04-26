from fastapi import APIRouter, Depends, HTTPException, status
from sqlmodel import Session, delete, select

from app.deps import get_current_user, get_db
from app.models import Room, RoomMember, RoomPlaybackState, RoomQueueItem, User, UserPlaylist, UserPlaylistItem
from app.schemas import UserPublic
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
def update_user(user_id: int, payload: dict, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if "is_admin" in payload:
        user.is_admin = bool(payload["is_admin"])
    db.add(user)
    db.commit()
    db.refresh(user)
    return {"id": user.id, "username": user.username, "is_admin": user.is_admin}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="user not found")
    if user.id == admin.id:
        raise HTTPException(status_code=400, detail="cannot delete yourself")
    db.exec(delete(RoomMember).where(RoomMember.user_id == user_id))
    playlists = db.exec(select(UserPlaylist).where(UserPlaylist.user_id == user_id)).all()
    for pl in playlists:
        db.exec(delete(UserPlaylistItem).where(UserPlaylistItem.playlist_id == pl.id))
    db.exec(delete(UserPlaylist).where(UserPlaylist.user_id == user_id))
    db.delete(user)
    db.commit()
    return {"ok": True}


@router.get("/rooms")
def list_all_rooms(db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    rooms = db.exec(select(Room).order_by(Room.created_at.desc())).all()
    out = []
    for r in rooms:
        members = db.exec(
            select(User.id, User.username)
            .join(RoomMember, RoomMember.user_id == User.id)
            .where(RoomMember.room_id == r.id)
            .order_by(RoomMember.joined_at.asc())
        ).all()
        creator = db.get(User, r.created_by_user_id)
        out.append({
            "id": r.id,
            "name": r.name,
            "created_by": creator.username if creator else str(r.created_by_user_id),
            "created_at": r.created_at,
            "members": [{"id": m[0], "username": m[1]} for m in members],
        })
    return out


@router.delete("/rooms/{room_id}")
async def delete_room(room_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    db.exec(delete(RoomPlaybackState).where(RoomPlaybackState.room_id == room_id))
    db.exec(delete(RoomQueueItem).where(RoomQueueItem.room_id == room_id))
    db.exec(delete(RoomMember).where(RoomMember.room_id == room_id))
    db.delete(room)
    db.commit()
    await hub.broadcast(room_id, {"type": "room_destroyed", "room_id": room_id})
    return {"ok": True}


@router.delete("/rooms/{room_id}/members/{user_id}")
async def remove_room_member(room_id: int, user_id: int, db: Session = Depends(get_db), admin: User = Depends(require_admin)):
    room = db.get(Room, room_id)
    if not room:
        raise HTTPException(status_code=404, detail="room not found")
    member = db.exec(select(RoomMember).where(RoomMember.room_id == room_id, RoomMember.user_id == user_id)).first()
    if not member:
        raise HTTPException(status_code=404, detail="member not found")
    db.delete(member)
    user = db.get(User, user_id)
    if user and user.last_active_room_id == room_id:
        user.last_active_room_id = None
        db.add(user)
    db.commit()
    await hub.broadcast(room_id, {"type": "room_member_left", "room_id": room_id, "user_id": user_id})
    return {"ok": True}
