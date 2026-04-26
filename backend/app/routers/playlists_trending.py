from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, delete, select

from app.deps import get_current_user, get_db
from app.models import Track, TrackOrderStats, TrackSource, User, UserPlaylist, UserPlaylistItem
from app.routers.queue_playback import _get_or_create_track


router = APIRouter(prefix="/api", tags=["playlists", "trending"])


@router.get("/playlists")
def list_playlists(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pls = db.exec(select(UserPlaylist).where(UserPlaylist.user_id == user.id).order_by(UserPlaylist.created_at.desc())).all()
    return [{"id": p.id, "name": p.name, "created_at": p.created_at} for p in pls]


@router.post("/playlists")
def create_playlist(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    pl = UserPlaylist(user_id=user.id, name=name)
    db.add(pl)
    db.commit()
    db.refresh(pl)
    return {"id": pl.id, "name": pl.name, "created_at": pl.created_at}


@router.delete("/playlists/{playlist_id}")
def delete_playlist(playlist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    db.exec(delete(UserPlaylistItem).where(UserPlaylistItem.playlist_id == playlist_id))
    db.delete(pl)
    db.commit()
    return {"ok": True}


@router.get("/playlists/{playlist_id}/items")
def playlist_items(playlist_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    rows = db.exec(
        select(UserPlaylistItem, Track)
        .join(Track, Track.id == UserPlaylistItem.track_id)
        .where(UserPlaylistItem.playlist_id == playlist_id)
        .order_by(UserPlaylistItem.created_at.desc())
    ).all()
    return [
        {
            "id": it.id,
            "created_at": it.created_at,
            "track": {
                "id": tr.id,
                "source": tr.source,
                "source_track_id": tr.source_track_id,
                "title": tr.title,
                "artist": tr.artist,
                "duration_ms": tr.duration_ms,
                "cover_url": tr.cover_url,
                "audio_url": tr.audio_url,
            },
        }
        for it, tr in rows
    ]


@router.post("/playlists/{playlist_id}/items")
def add_playlist_item(playlist_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    source = payload.get("source")
    source_track_id = payload.get("source_track_id")
    title = payload.get("title")
    if source not in {s.value for s in TrackSource}:
        raise HTTPException(status_code=400, detail="invalid source")
    if not source_track_id or not isinstance(source_track_id, str):
        raise HTTPException(status_code=400, detail="invalid source_track_id")
    if not title or not isinstance(title, str):
        raise HTTPException(status_code=400, detail="invalid title")
    tr = _get_or_create_track(
        db,
        source=TrackSource(source),
        source_track_id=source_track_id,
        title=title,
        artist=payload.get("artist"),
        duration_ms=payload.get("duration_ms"),
        cover_url=payload.get("cover_url"),
        audio_url=payload.get("audio_url"),
    )
    existing = db.exec(
        select(UserPlaylistItem).where(
            UserPlaylistItem.playlist_id == playlist_id,
            UserPlaylistItem.track_id == tr.id,
        )
    ).first()
    if existing:
        return {"id": existing.id, "created_at": existing.created_at}
    it = UserPlaylistItem(playlist_id=playlist_id, track_id=tr.id)
    db.add(it)
    db.commit()
    db.refresh(it)
    return {"id": it.id, "created_at": it.created_at}


@router.delete("/playlists/{playlist_id}/items/{item_id}")
def delete_playlist_item(playlist_id: int, item_id: int, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    it = db.get(UserPlaylistItem, item_id)
    if not it or it.playlist_id != playlist_id:
        raise HTTPException(status_code=404, detail="item not found")
    db.delete(it)
    db.commit()
    return {"ok": True}


@router.get("/trending")
def trending(limit: int = 50, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    limit = max(1, min(200, int(limit)))
    rows = db.exec(
        select(TrackOrderStats, Track)
        .join(Track, Track.id == TrackOrderStats.track_id)
        .order_by(TrackOrderStats.order_count.desc(), TrackOrderStats.last_ordered_at.desc())
        .limit(limit)
    ).all()
    return [
        {
            "track": {
                "id": tr.id,
                "source": tr.source,
                "source_track_id": tr.source_track_id,
                "title": tr.title,
                "artist": tr.artist,
                "duration_ms": tr.duration_ms,
                "cover_url": tr.cover_url,
            },
            "order_count": stats.order_count,
            "last_ordered_at": stats.last_ordered_at,
        }
        for stats, tr in rows
    ]

