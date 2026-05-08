from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx
from sqlmodel import Session

from app.bilibili import find_bilibili_page, normalize_bilibili_pic, parse_bilibili_source_track_id
from app.core.config import settings
from app.models import Track, TrackSource


@dataclass
class AudioResolveResult:
    audio_url: str | None = None
    analysis_audio_url: str | None = None
    loudness_gain_db: float | None = None
    loudness_peak: float | None = None
    loudness_source: str | None = None


def _direct_playback_audio_url(track: Track) -> str | None:
    if track.audio_url:
        return track.audio_url
    if track.source == TrackSource.netease:
        return f"https://music.163.com/song/media/outer/url?id={track.source_track_id}.mp3"
    return None


def _is_bilibili_browser_direct_url(audio_url: str | None) -> bool:
    if not audio_url:
        return False
    path = urlparse(audio_url).path.lower()
    return path.endswith(".mp4")


def _finite_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not (number == number and number not in (float("inf"), float("-inf"))):
        return None
    return number


def _clamp_loudness_gain_db(value: float | None) -> float | None:
    if value is None:
        return None
    return max(-12.0, min(12.0, value))


def _bilibili_loudness_from_playurl(data: dict[str, Any] | None) -> AudioResolveResult:
    volume = (data or {}).get("volume")
    if not isinstance(volume, dict):
        return AudioResolveResult()
    measured_i = _finite_float(volume.get("measured_i"))
    target_i = _finite_float(volume.get("target_i"))
    target_offset = _finite_float(volume.get("target_offset"))
    measured_tp = _finite_float(volume.get("measured_tp"))
    gain_db = None
    if measured_i is not None and target_i is not None:
        gain_db = target_i - measured_i
    elif target_offset is not None:
        gain_db = target_offset
    peak = None
    if measured_tp is not None:
        # true peak is normally reported in dBTP.
        peak = 10 ** (measured_tp / 20)
    return AudioResolveResult(
        loudness_gain_db=_clamp_loudness_gain_db(gain_db),
        loudness_peak=peak,
        loudness_source="bilibili:playurl-volume",
    )


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
    video = await _fetch_bilibili_video_metadata(track.source_track_id)
    if not video:
        return None

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
        db.add(track)
        db.commit()
        db.refresh(track)
    return video


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
            return AudioResolveResult(
                audio_url=audio_url,
                analysis_audio_url=audio_url,
            )
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
                if data.get("code") != 0:
                    return AudioResolveResult()
                vid = data["data"]
            if not vid:
                return AudioResolveResult()
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
            loudness = _bilibili_loudness_from_playurl(html5)
            playback_url = None
            durl = (html5 or {}).get("durl") or []
            for item in durl:
                direct_url = item.get("url") if isinstance(item, dict) else None
                if direct_url:
                    playback_url = direct_url
                    break
            if playback_url and (loudness.loudness_gain_db is not None or loudness.loudness_peak is not None):
                loudness.audio_url = playback_url
                return loudness

            dash_data = await _playurl({"bvid": ref.bvid, "cid": cid, "fnval": 16, "fnver": 0, "fourk": 1})
            if loudness.loudness_gain_db is None and loudness.loudness_peak is None:
                loudness = _bilibili_loudness_from_playurl(dash_data)
            dash = (dash_data or {}).get("dash")
            if not dash:
                if playback_url:
                    loudness.audio_url = playback_url
                    return loudness
                return AudioResolveResult()
            audio_list = dash.get("audio") or []
            if not audio_list:
                if playback_url:
                    loudness.audio_url = playback_url
                    return loudness
                return AudioResolveResult()
            aac = [a for a in audio_list if "mp4a" in (a.get("codecs") or "")]
            candidates = aac if aac else audio_list
            candidates.sort(key=lambda x: x.get("bandwidth", 0), reverse=True)
            loudness.analysis_audio_url = candidates[0].get("baseUrl") or candidates[0].get("base_url")
            loudness.audio_url = playback_url or loudness.analysis_audio_url
            return loudness
    except Exception:
        return AudioResolveResult()
