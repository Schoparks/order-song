import json
import re
from datetime import datetime

import httpx
from fastapi import APIRouter, Depends, HTTPException
from sqlmodel import Session, delete, select

from app.core.config import settings
from app.deps import get_current_user, get_db
from app.models import Track, TrackOrderStats, TrackSource, User, UserPlaylist, UserPlaylistItem
from app.routers.queue_playback import _ensure_bilibili_metadata, _get_or_create_track


router = APIRouter(prefix="/api", tags=["playlists", "trending"])


@router.get("/playlists")
def list_playlists(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pls = db.exec(select(UserPlaylist).where(UserPlaylist.user_id == user.id).order_by(UserPlaylist.created_at.asc())).all()
    out = []
    for p in pls:
        count = db.exec(
            select(UserPlaylistItem.id).where(UserPlaylistItem.playlist_id == p.id)
        ).all()
        out.append({"id": p.id, "name": p.name, "created_at": p.created_at, "item_count": len(count)})
    return out


@router.post("/playlists")
def create_playlist(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    pl = UserPlaylist(user_id=user.id, name=name)
    db.add(pl)
    db.commit()
    db.refresh(pl)
    return {"id": pl.id, "name": pl.name, "created_at": pl.created_at, "item_count": 0}


@router.patch("/playlists/{playlist_id}")
def rename_playlist(playlist_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    name = (payload.get("name") or "").strip()
    if not name:
        raise HTTPException(status_code=400, detail="name required")
    pl.name = name
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
async def add_playlist_item(playlist_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
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
    if tr.source == TrackSource.bilibili and not tr.cover_url:
        await _ensure_bilibili_metadata(db, tr)
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


@router.post("/playlists/{playlist_id}/items/{item_id}/move")
def move_playlist_item(playlist_id: int, item_id: int, payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Move a track from one playlist to another."""
    pl = db.get(UserPlaylist, playlist_id)
    if not pl or pl.user_id != user.id:
        raise HTTPException(status_code=404, detail="playlist not found")
    it = db.get(UserPlaylistItem, item_id)
    if not it or it.playlist_id != playlist_id:
        raise HTTPException(status_code=404, detail="item not found")
    target_id = payload.get("target_playlist_id")
    if not target_id:
        raise HTTPException(status_code=400, detail="target_playlist_id required")
    target = db.get(UserPlaylist, target_id)
    if not target or target.user_id != user.id:
        raise HTTPException(status_code=404, detail="target playlist not found")
    existing = db.exec(
        select(UserPlaylistItem).where(
            UserPlaylistItem.playlist_id == target_id,
            UserPlaylistItem.track_id == it.track_id,
        )
    ).first()
    if existing:
        db.delete(it)
        db.commit()
        return {"id": existing.id, "created_at": existing.created_at}
    it.playlist_id = target_id
    db.add(it)
    db.commit()
    db.refresh(it)
    return {"id": it.id, "created_at": it.created_at}


@router.get("/playlists/track-map")
def track_playlist_map(db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Returns a mapping of all tracks in user's playlists: {source:source_track_id -> {item_id, playlist_id}}"""
    pls = db.exec(select(UserPlaylist).where(UserPlaylist.user_id == user.id)).all()
    result = {}
    for pl in pls:
        items = db.exec(
            select(UserPlaylistItem, Track)
            .join(Track, Track.id == UserPlaylistItem.track_id)
            .where(UserPlaylistItem.playlist_id == pl.id)
        ).all()
        for it, tr in items:
            key = f"{tr.source.value}:{tr.source_track_id}"
            if key not in result:
                result[key] = []
            result[key].append({"item_id": it.id, "playlist_id": pl.id, "playlist_name": pl.name})
    return result


@router.post("/playlists/import-netease")
async def import_netease_playlist(payload: dict, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    """Import a NetEase Cloud Music playlist, excluding VIP-only songs."""
    url_or_id = (payload.get("url") or payload.get("id") or "").strip()
    if not url_or_id:
        raise HTTPException(status_code=400, detail="url or id required")

    playlist_id = None
    m = re.search(r"id=(\d+)", url_or_id)
    if m:
        playlist_id = m.group(1)
    if not playlist_id:
        m = re.search(r"/playlist[/](\d+)", url_or_id)
        if m:
            playlist_id = m.group(1)
    if not playlist_id and url_or_id.isdigit():
        playlist_id = url_or_id

    if not playlist_id:
        raise HTTPException(status_code=400, detail="无法解析歌单ID，请提供歌单链接或ID")

    try:
        async with httpx.AsyncClient(timeout=settings.upstream.netease_playlist_timeout_s) as client:
            r = await client.post(
                "https://music.163.com/api/v3/playlist/detail",
                data={"id": playlist_id, "n": "100000", "s": "8"},
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                    "Referer": "https://music.163.com/",
                    "Content-Type": "application/x-www-form-urlencoded",
                },
            )
            r.raise_for_status()
            data = r.json()
    except Exception:
        raise HTTPException(status_code=502, detail="请求网易云API失败")

    playlist_data = data.get("playlist") or data.get("result")
    if data.get("code") not in (200, None) or not playlist_data:
        raise HTTPException(status_code=400, detail="歌单不存在或无法访问")

    netease_name = playlist_data.get("name") or f"网易云歌单{playlist_id}"
    tracks_raw = playlist_data.get("tracks") or []
    track_ids_raw = playlist_data.get("trackIds") or []
    privileges = {p["id"]: p for p in (playlist_data.get("privileges") or data.get("privileges") or []) if "id" in p}

    if track_ids_raw and len(tracks_raw) < len(track_ids_raw):
        fetched_ids = {s.get("id") for s in tracks_raw if s.get("id") is not None}
        missing_ids = [t["id"] for t in track_ids_raw if isinstance(t, dict) and t.get("id") not in fetched_ids]
        if missing_ids:
            try:
                async with httpx.AsyncClient(timeout=settings.upstream.netease_playlist_detail_timeout_s) as detail_client:
                    batch_size = settings.upstream.netease_playlist_detail_batch_size
                    for i in range(0, len(missing_ids), batch_size):
                        batch = missing_ids[i:i + batch_size]
                        c_param = json.dumps([{"id": sid} for sid in batch])
                        dr = await detail_client.post(
                            "https://music.163.com/api/v3/song/detail",
                            data={"c": c_param},
                            headers={
                                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36",
                                "Referer": "https://music.163.com/",
                                "Content-Type": "application/x-www-form-urlencoded",
                            },
                        )
                        dr.raise_for_status()
                        detail_data = dr.json()
                        tracks_raw.extend(detail_data.get("songs") or [])
                        for p in (detail_data.get("privileges") or []):
                            if "id" in p:
                                privileges[p["id"]] = p
            except Exception:
                pass

    playlist_name = (payload.get("name") or "").strip() or netease_name
    pl = UserPlaylist(user_id=user.id, name=playlist_name)
    db.add(pl)
    db.commit()
    db.refresh(pl)

    added = 0
    skipped = 0
    for s in tracks_raw:
        sid = s.get("id")
        if sid is None:
            continue
        fee = s.get("fee", 0)
        priv = privileges.get(sid, {})
        if fee == 1 or s.get("noCopyrightRcmd") or priv.get("st", 0) < 0 or priv.get("pl", 1) == 0:
            skipped += 1
            continue

        artists = s.get("ar") or s.get("artists") or []
        artist = artists[0].get("name") if artists else None
        album = s.get("al") or s.get("album") or {}
        cover_url = album.get("picUrl")
        duration_ms = s.get("dt") or s.get("duration")

        tr = _get_or_create_track(
            db,
            source=TrackSource.netease,
            source_track_id=str(sid),
            title=s.get("name") or str(sid),
            artist=artist,
            duration_ms=duration_ms,
            cover_url=cover_url,
        )
        existing = db.exec(
            select(UserPlaylistItem).where(
                UserPlaylistItem.playlist_id == pl.id,
                UserPlaylistItem.track_id == tr.id,
            )
        ).first()
        if not existing:
            db.add(UserPlaylistItem(playlist_id=pl.id, track_id=tr.id))
            added += 1

    db.commit()
    return {
        "ok": True,
        "playlist_id": pl.id,
        "playlist_name": playlist_name,
        "added": added,
        "skipped": skipped,
        "total": len(tracks_raw),
    }


@router.get("/trending")
def trending(limit: int | None = None, db: Session = Depends(get_db), user: User = Depends(get_current_user)):
    limit = max(1, int(limit if limit is not None else settings.trending.limit))
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
