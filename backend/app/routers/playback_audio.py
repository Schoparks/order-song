import asyncio
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlmodel import Session

from app.audio_loudness import validate_trusted_audio_url
from app.bilibili import find_bilibili_page, normalize_bilibili_pic, parse_bilibili_source_track_id
from app.core.config import settings
from app.db import engine
from app.models import Track, TrackSource
from app.qq_music import QQMusicUnavailable, QQTrackUnavailable, qq_music


@dataclass
class AudioResolveResult:
    audio_url: str | None = None
    analysis_audio_url: str | None = None
    loudness_gain_db: float | None = None
    loudness_peak: float | None = None
    loudness_source: str | None = None
    unavailable_reason: str | None = None


def _direct_playback_audio_url(track: Track) -> str | None:
    if track.availability == "unavailable":
        return None
    if track.source == TrackSource.qq:
        return qq_music.current_audio_url(track.source_track_id, track.audio_url)
    if track.audio_url and validate_trusted_audio_url(track.audio_url, track.source.value) is None:
        if track.source != TrackSource.bilibili or _is_bilibili_browser_direct_url(track.audio_url):
            return track.audio_url
    if track.source == TrackSource.netease:
        return f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
    return None


def _is_bilibili_browser_direct_url(audio_url: str | None) -> bool:
    if not audio_url:
        return False
    path = urlparse(audio_url).path.lower()
    return path.endswith(".mp4")


def _clamp_loudness_gain_db(value: float | None) -> float | None:
    if value is None:
        return None
    return max(-12.0, min(12.0, value))

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
    source_track_id = track.source_track_id
    track_id = track.id
    db.rollback()
    video = await _fetch_bilibili_video_metadata(source_track_id)
    if not video:
        return None

    if not track_id:
        return video

    # The upstream request above runs on the event loop. Persist its result in a
    # fresh worker-owned Session so a slow SQLite commit cannot stall playback
    # heartbeats, and never move the request Session across threads.
    await asyncio.to_thread(_apply_bilibili_metadata_transaction, track_id, video)
    db.expire_all()
    db.refresh(track)
    return video


def _apply_bilibili_metadata_transaction(track_id: int, video: dict[str, Any]) -> None:
    with Session(engine) as write_db:
        track = write_db.get(Track, track_id)
        if not track or track.source != TrackSource.bilibili:
            return

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
            write_db.add(track)
            write_db.commit()


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
            audio_url = item.get("url") if isinstance(item.get("url"), str) else None
            if item.get("freeTrialInfo"):
                return AudioResolveResult(unavailable_reason="该歌曲仅提供试听，无法完整播放")
            if not audio_url and item and data.get("code") == 200 and item.get("code") in (200, 404, -110):
                return AudioResolveResult(unavailable_reason="该歌曲已下架或当前无完整播放权限")
            return AudioResolveResult(
                audio_url=audio_url,
                analysis_audio_url=audio_url,
            )
    except Exception:
        return AudioResolveResult()


async def _resolve_qq_audio(source_track_id: str, *, force: bool = False) -> AudioResolveResult:
    try:
        audio_url = await qq_music.resolve_audio_url(source_track_id, force=force)
        return AudioResolveResult(audio_url=audio_url, analysis_audio_url=audio_url)
    except QQTrackUnavailable:
        return AudioResolveResult(unavailable_reason="该歌曲已下架或不再提供免费完整播放")
    except QQMusicUnavailable:
        return AudioResolveResult()
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
                if data.get("code") in (-404, 62002, 62004):
                    return AudioResolveResult(unavailable_reason="该视频已删除、下架或不可见")
                if data.get("code") != 0:
                    return AudioResolveResult()
                vid = data["data"]
            if not vid:
                return AudioResolveResult()
            pages = vid.get("pages")
            if ref.is_part and isinstance(pages, list) and pages:
                exists = any(isinstance(p, dict) and (p.get("cid") == ref.cid if ref.cid else p.get("page") == ref.page) for p in pages)
                if not exists:
                    return AudioResolveResult(unavailable_reason="该视频分集已删除或替换")
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
            result = AudioResolveResult()
            playback_url = None
            durl = (html5 or {}).get("durl") or []
            for item in durl:
                direct_url = item.get("url") if isinstance(item, dict) else None
                if direct_url:
                    playback_url = direct_url
                    break
            dash_data = await _playurl({"bvid": ref.bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1})
            dash = (dash_data or {}).get("dash")
            if not dash:
                if playback_url:
                    result.audio_url = playback_url
                    result.analysis_audio_url = playback_url
                    return result
                return AudioResolveResult()
            audio_list = dash.get("audio") or []
            if not audio_list:
                if playback_url:
                    result.audio_url = playback_url
                    result.analysis_audio_url = playback_url
                    return result
                return AudioResolveResult()
            aac = [a for a in audio_list if "mp4a" in (a.get("codecs") or "")]
            candidates = aac if aac else audio_list
            candidates.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            result.analysis_audio_url = candidates[0].get("baseUrl") or candidates[0].get("base_url")
            # DASH audio-only URLs are for backend analysis. They commonly return 403
            # to browsers, so never promote them to the browser playback field.
            result.audio_url = playback_url
            return result
    except Exception:
        return AudioResolveResult()
