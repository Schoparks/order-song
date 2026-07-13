import asyncio
import hashlib
import re
from dataclasses import dataclass
from typing import Any, Literal

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query

from app.bilibili import make_bilibili_source_track_id, normalize_bilibili_pic
from app.core.config import settings
from app.deps import get_current_user
from app.models import TrackSource, User
from app.qq_music import QQMusicUnavailable, qq_music
from app.schemas import SearchTrackOut, SearchTrackPartOut


router = APIRouter(prefix="/api", tags=["search"])

_BV_IN_TEXT_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)
_BILI_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Referer": "https://search.bilibili.com/",
    "Origin": "https://search.bilibili.com",
}


def _parse_bili_duration_ms(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return max(0, round(float(value) * 1000))
    parts = str(value).strip().split(":")
    if not parts or len(parts) > 3:
        return None
    try:
        values = [float(part) if index == len(parts) - 1 else int(part) for index, part in enumerate(parts)]
    except (TypeError, ValueError):
        return None
    if any(value < 0 for value in values) or any(value >= 60 for value in values[1:]):
        return None
    seconds = 0.0
    for part in values:
        seconds = seconds * 60 + part
    return round(seconds * 1000)


def _duration_allowed(duration_ms: Any) -> bool:
    if isinstance(duration_ms, bool) or not isinstance(duration_ms, (int, float)):
        return False
    return 0 < duration_ms < settings.search.max_duration_s * 1000


async def _get_json_with_retries(
    url: str,
    *,
    params: dict[str, Any],
    headers: dict[str, str],
    attempts: int | None = None,
    timeout: float | None = None,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    max_attempts = attempts if attempts is not None else settings.search.attempts
    request_timeout = timeout if timeout is not None else settings.search.timeout_s
    for attempt in range(max_attempts):
        try:
            async with httpx.AsyncClient(timeout=request_timeout, headers=headers, follow_redirects=True) as client:
                response = await client.get(url, params=params)
                response.raise_for_status()
                return response.json()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.25 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def _bili_video_by_bv(bv: str) -> dict[str, Any]:
    data = await _get_json_with_retries(
        "https://api.bilibili.com/x/web-interface/view",
        params={"bvid": bv},
        headers={**_BILI_HEADERS, "Referer": "https://www.bilibili.com/"},
    )
    if data.get("code") != 0 or not isinstance(data.get("data"), dict):
        raise RuntimeError("bilibili view api error")
    return data["data"]


async def _bili_search(keyword: str, page: int = 1) -> list[dict[str, Any]]:
    data = await _get_json_with_retries(
        "https://api.bilibili.com/x/web-interface/search/type",
        params={"search_type": "video", "keyword": keyword, "page": page, "page_size": settings.search.bilibili.page_size},
        headers=_BILI_HEADERS,
    )
    if data.get("code") != 0:
        raise RuntimeError("bilibili search api error")
    return data.get("data", {}).get("result", []) or []


def _clean_bili_title(value: Any) -> str:
    return str(value or "").replace('<em class="keyword">', "").replace("</em>", "").strip()


def _bili_video_to_search_track(
    video: dict[str, Any],
    *,
    bvid: str,
    fallback_title: str | None = None,
    fallback_artist: str | None = None,
    fallback_duration_ms: int | None = None,
    fallback_cover_url: str | None = None,
) -> SearchTrackOut | None:
    owner = video.get("owner") or {}
    artist = owner.get("name") if isinstance(owner, dict) else None
    cover_url = normalize_bilibili_pic(video.get("pic")) or normalize_bilibili_pic(fallback_cover_url)
    title = str(video.get("title") or fallback_title or bvid)
    duration_ms = _parse_bili_duration_ms(video.get("duration")) or fallback_duration_ms
    pages = video.get("pages")
    is_collection = isinstance(pages, list) and len(pages) > 1
    parts: list[SearchTrackPartOut] = []
    if is_collection:
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            page_no = page.get("page") if isinstance(page.get("page"), int) else index + 1
            cid = page.get("cid") if isinstance(page.get("cid"), int) else None
            part_duration_ms = _parse_bili_duration_ms(page.get("duration"))
            if not _duration_allowed(part_duration_ms):
                continue
            parts.append(
                SearchTrackPartOut(
                    source=TrackSource.bilibili,
                    source_track_id=make_bilibili_source_track_id(bvid, page=page_no, cid=cid),
                    title=str(page.get("part") or "").strip() or f"{title} P{page_no}",
                    artist=artist or fallback_artist,
                    duration_ms=part_duration_ms,
                    cover_url=cover_url,
                    part_number=page_no,
                )
            )
        if not parts:
            return None
    elif not _duration_allowed(duration_ms):
        return None

    return SearchTrackOut(
        source=TrackSource.bilibili,
        source_track_id=bvid,
        title=title,
        artist=artist or fallback_artist,
        duration_ms=duration_ms,
        cover_url=cover_url,
        parts=parts,
    )


async def _run_bilibili(q: str) -> list[SearchTrackOut]:
    if not settings.search.bilibili.enabled:
        raise RuntimeError("B站搜索未启用")
    match = _BV_IN_TEXT_RE.search(q)
    if match:
        track = _bili_video_to_search_track(await _bili_video_by_bv(match.group(1)), bvid=match.group(1))
        return [track] if track else []

    results = await _bili_search(q, page=1)
    raw_items: list[SearchTrackOut] = []
    for item in results:
        bvid = item.get("bvid")
        if not bvid:
            continue
        raw_items.append(
            SearchTrackOut(
                source=TrackSource.bilibili,
                source_track_id=bvid,
                title=_clean_bili_title(item.get("title")),
                artist=item.get("author"),
                duration_ms=_parse_bili_duration_ms(item.get("duration")),
                cover_url=normalize_bilibili_pic(item.get("pic")),
            )
        )
    details = await asyncio.gather(
        *[_bili_video_by_bv(item.source_track_id) for item in raw_items], return_exceptions=True
    )
    filtered: list[SearchTrackOut] = []
    for item, detail in zip(raw_items, details):
        if isinstance(detail, Exception):
            if _duration_allowed(item.duration_ms):
                filtered.append(item)
            continue
        track = _bili_video_to_search_track(
            detail,
            bvid=item.source_track_id,
            fallback_title=item.title,
            fallback_artist=item.artist,
            fallback_duration_ms=item.duration_ms,
            fallback_cover_url=item.cover_url,
        )
        if track:
            filtered.append(track)
    return filtered[: settings.search.bilibili.result_limit]


async def _run_netease(q: str) -> list[SearchTrackOut]:
    if not settings.search.netease.enabled:
        raise RuntimeError("网易云搜索未启用")
    async with httpx.AsyncClient(
        timeout=settings.search.timeout_s,
        headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"},
    ) as client:
        response = await client.get(
            "https://music.163.com/api/search/get",
            params={"s": q, "type": 1, "limit": settings.search.netease.api_limit},
        )
        response.raise_for_status()
        data = response.json()
    songs = (((data or {}).get("result") or {}).get("songs")) or []
    filtered: list[SearchTrackOut] = []
    for song in songs:
        sid = song.get("id")
        duration_ms = song.get("duration")
        if sid is None or not _duration_allowed(duration_ms):
            continue
        if song.get("fee", 0) == 1 or song.get("noCopyrightRcmd"):
            continue
        privilege = song.get("privilege") or {}
        if song.get("status", 0) < 0 or privilege.get("st", 0) < 0 or privilege.get("pl", 1) == 0:
            continue
        artists = song.get("artists") or []
        filtered.append(
            SearchTrackOut(
                source=TrackSource.netease,
                source_track_id=str(sid),
                title=song.get("name") or str(sid),
                artist=artists[0].get("name") if artists else None,
                duration_ms=duration_ms,
                cover_url=((song.get("album") or {}).get("picUrl")),
            )
        )
        if len(filtered) >= settings.search.netease.result_limit:
            break
    return filtered


async def _run_qq(q: str) -> list[SearchTrackOut]:
    if not settings.qq_music.enabled:
        raise QQMusicUnavailable("QQ 音乐解析未开启")
    return [
        SearchTrackOut(source=TrackSource.qq, **track)
        for track in await qq_music.search_tracks(q)
    ]


def _normalize_search_text(value: str | None) -> str:
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (value or "").casefold()).strip()


def _text_relevance(query: str, text: str | None) -> tuple[float, bool]:
    normalized = _normalize_search_text(text)
    if not normalized:
        return 0.0, False
    if normalized == query:
        return 100.0, True
    if normalized.startswith(query):
        return 82.0, True
    if query in normalized:
        return 68.0, True
    tokens = [token for token in query.split() if token]
    coverage = sum(token in normalized for token in tokens) / max(1, len(tokens))
    return 40.0 * coverage, coverage >= 1.0 and bool(tokens)


@dataclass(frozen=True)
class _RankedTrack:
    track: SearchTrackOut
    score: float
    upstream_rank: int
    weak_collection: bool


def _rank_results(q: str, items: list[SearchTrackOut]) -> list[SearchTrackOut]:
    query = _normalize_search_text(q)
    ranked: list[_RankedTrack] = []
    seen: set[tuple[str, str]] = set()
    source_ranks: dict[str, int] = {}
    for track in items:
        key = (track.source.value, track.source_track_id)
        if key in seen:
            continue
        seen.add(key)
        upstream_rank = source_ranks.get(track.source.value, 0)
        source_ranks[track.source.value] = upstream_rank + 1
        title_score, title_strong = _text_relevance(query, track.title)
        artist_score, _ = _text_relevance(query, track.artist)
        best_part_score = 0.0
        part_strong = False
        for part in track.parts:
            part_score, strong = _text_relevance(query, part.title)
            best_part_score = max(best_part_score, part_score)
            part_strong = part_strong or strong
        is_collection = bool(track.parts)
        score = max(title_score, best_part_score) + artist_score * 0.35 + max(0.0, 3.0 - upstream_rank * 0.15)
        if not is_collection:
            score += 5.0
        elif len(_normalize_search_text(track.title)) > max(45, len(query) * 5):
            score -= 8.0
        ranked.append(
            _RankedTrack(
                track=track,
                score=score,
                upstream_rank=upstream_rank,
                weak_collection=is_collection and not (title_strong or part_strong),
            )
        )
    ranked.sort(
        key=lambda item: (
            -item.score,
            item.upstream_rank,
            hashlib.sha256(
                f"{query}\0{item.track.source.value}\0{item.track.source_track_id}".encode("utf-8")
            ).digest(),
            item.track.source.value,
            item.track.source_track_id,
        )
    )

    first_page: list[_RankedTrack] = []
    deferred: list[_RankedTrack] = []
    remainder: list[_RankedTrack] = []
    weak_in_first_page = 0
    for item in ranked:
        if len(first_page) >= 8:
            remainder.append(item)
        elif item.weak_collection and weak_in_first_page >= 2:
            deferred.append(item)
        else:
            first_page.append(item)
            if item.weak_collection:
                weak_in_first_page += 1
    ordered = first_page + deferred + remainder
    return [item.track for item in ordered[: settings.search.aggregate_limit]]


@router.get("/search", response_model=list[SearchTrackOut])
async def search(
    q: str,
    source: Literal["all", "bilibili", "netease", "qq"] = Query("all"),
    user: User = Depends(get_current_user),
):
    del user
    q = q.strip()
    if not q:
        return []
    runners = {"bilibili": _run_bilibili, "netease": _run_netease}
    if settings.qq_music.enabled:
        runners["qq"] = _run_qq
    if source != "all":
        if source == "qq" and not settings.qq_music.enabled:
            raise HTTPException(status_code=400, detail="QQ 音乐解析未开启")
        try:
            items = await runners[source](q)
        except QQMusicUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"{source} 搜索暂时不可用") from exc
        return _rank_results(q, items)

    gathered = await asyncio.gather(*(runner(q) for runner in runners.values()), return_exceptions=True)
    items: list[SearchTrackOut] = []
    for result in gathered:
        if not isinstance(result, Exception):
            items.extend(result)
    return _rank_results(q, items)
