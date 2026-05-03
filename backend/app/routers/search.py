import asyncio
import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.bilibili import make_bilibili_source_track_id, normalize_bilibili_pic
from app.core.config import settings
from app.deps import get_current_user
from app.models import TrackSource, User
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
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value * 1000)
    parts = str(value).strip().split(":")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    seconds = 0
    for part in parts:
        seconds = seconds * 60 + int(part)
    return seconds * 1000


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
                r = await client.get(url, params=params)
                r.raise_for_status()
                return r.json()
        except Exception as exc:
            last_exc = exc
            if attempt + 1 < max_attempts:
                await asyncio.sleep(0.25 * (attempt + 1))
    assert last_exc is not None
    raise last_exc


async def _bili_video_by_bv(bv: str) -> dict[str, Any]:
    for attempt in range(settings.search.attempts):
        data = await _get_json_with_retries(
            "https://api.bilibili.com/x/web-interface/view",
            params={"bvid": bv},
            headers={**_BILI_HEADERS, "Referer": "https://www.bilibili.com/"},
        )
        if data.get("code") == 0:
            return data["data"]
        if attempt + 1 < settings.search.attempts:
            await asyncio.sleep(0.25)
    raise HTTPException(status_code=400, detail="bilibili view api error")


async def _bili_search(keyword: str, page: int = 1) -> list[dict[str, Any]]:
    for attempt in range(settings.search.attempts):
        data = await _get_json_with_retries(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": keyword, "page": page, "page_size": settings.search.bilibili.page_size},
            headers=_BILI_HEADERS,
        )
        if data.get("code") == 0:
            return data.get("data", {}).get("result", []) or []
        if attempt + 1 < settings.search.attempts:
            await asyncio.sleep(0.25)
    raise HTTPException(status_code=400, detail="bilibili search api error")


def _clean_bili_title(value: Any) -> str:
    return str(value or "").replace("<em class=\"keyword\">", "").replace("</em>", "").strip()


def _bili_video_to_search_track(
    video: dict[str, Any],
    *,
    bvid: str,
    fallback_title: str | None = None,
    fallback_artist: str | None = None,
    fallback_duration_ms: int | None = None,
    fallback_cover_url: str | None = None,
) -> SearchTrackOut:
    owner = video.get("owner") or {}
    artist = owner.get("name") if isinstance(owner, dict) else None
    cover_url = normalize_bilibili_pic(video.get("pic")) or normalize_bilibili_pic(fallback_cover_url)
    title = video.get("title") or fallback_title or bvid
    duration = video.get("duration")
    duration_ms = int(duration * 1000) if isinstance(duration, (int, float)) else fallback_duration_ms

    parts: list[SearchTrackPartOut] = []
    pages = video.get("pages")
    if isinstance(pages, list) and len(pages) > 1:
        for index, page in enumerate(pages):
            if not isinstance(page, dict):
                continue
            page_no = page.get("page") if isinstance(page.get("page"), int) else index + 1
            cid = page.get("cid") if isinstance(page.get("cid"), int) else None
            part_title = str(page.get("part") or "").strip() or f"{title} P{page_no}"
            part_duration = page.get("duration")
            parts.append(
                SearchTrackPartOut(
                    source=TrackSource.bilibili,
                    source_track_id=make_bilibili_source_track_id(bvid, page=page_no, cid=cid),
                    title=part_title,
                    artist=artist or fallback_artist,
                    duration_ms=int(part_duration * 1000) if isinstance(part_duration, (int, float)) else None,
                    cover_url=cover_url,
                )
            )

    return SearchTrackOut(
        source=TrackSource.bilibili,
        source_track_id=bvid,
        title=title,
        artist=artist or fallback_artist,
        duration_ms=duration_ms,
        cover_url=cover_url,
        parts=parts,
    )


@router.get("/search", response_model=list[SearchTrackOut])
async def search(q: str, user: User = Depends(get_current_user)):
    q = q.strip()
    if not q:
        return []

    out: list[SearchTrackOut] = []

    def _dedupe(items: list[SearchTrackOut]) -> list[SearchTrackOut]:
        seen: set[tuple[str, str]] = set()
        res: list[SearchTrackOut] = []
        for it in items:
            key = (it.source.value, it.source_track_id)
            if key in seen:
                continue
            seen.add(key)
            res.append(it)
        return res

    # bilibili (BV direct or keyword)
    async def run_bilibili() -> list[SearchTrackOut]:
        if not settings.search.bilibili.enabled:
            return []
        m = _BV_IN_TEXT_RE.search(q)
        if m:
            bv = m.group(1)
            try:
                v = await _bili_video_by_bv(bv)
                return [_bili_video_to_search_track(v, bvid=bv)]
            except Exception:
                # BV 号检索不到对应视频：不返回结果
                return []

        try:
            results = await _bili_search(q, page=1)
            raw_items: list[SearchTrackOut] = []
            for item in results[: settings.search.bilibili.result_limit]:
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
            detail_tasks = [_bili_video_by_bv(item.source_track_id) for item in raw_items]
            details = await asyncio.gather(*detail_tasks, return_exceptions=True)
            out2: list[SearchTrackOut] = []
            for item, detail in zip(raw_items, details):
                if isinstance(detail, Exception):
                    out2.append(item)
                    continue
                out2.append(
                    _bili_video_to_search_track(
                        detail,
                        bvid=item.source_track_id,
                        fallback_title=item.title,
                        fallback_artist=item.artist,
                        fallback_duration_ms=item.duration_ms,
                        fallback_cover_url=item.cover_url,
                    )
                )
            return out2
        except Exception:
            return []

    async def run_netease() -> list[SearchTrackOut]:
        if not settings.search.netease.enabled:
            return []
        async with httpx.AsyncClient(timeout=settings.search.timeout_s, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"}) as client:
            r = await client.get("https://music.163.com/api/search/get", params={"s": q, "type": 1, "limit": settings.search.netease.api_limit})
            r.raise_for_status()
            data = r.json()
        songs = (((data or {}).get("result") or {}).get("songs")) or []
        out2: list[SearchTrackOut] = []
        for s in songs:
            sid = s.get("id")
            if sid is None:
                continue
            fee = s.get("fee", 0)
            if fee == 1 or s.get("noCopyrightRcmd"):
                continue
            status = s.get("status", 0)
            privilege = s.get("privilege") or {}
            if status < 0 or privilege.get("st", 0) < 0 or privilege.get("pl", 1) == 0:
                continue
            artists = s.get("artists") or []
            artist = artists[0].get("name") if artists else None
            out2.append(
                SearchTrackOut(
                    source=TrackSource.netease,
                    source_track_id=str(sid),
                    title=s.get("name") or str(sid),
                    artist=artist,
                    duration_ms=s.get("duration"),
                    cover_url=((s.get("album") or {}).get("picUrl")),
                )
            )
            if len(out2) >= settings.search.netease.result_limit:
                break
        return out2

    # Aggregate concurrently (best-effort)
    tasks = [run_bilibili(), run_netease()]

    # qqmusic / kugou:
    # In 2026 these are often served via third-party proxies that may be unstable.
    # We'll add them later behind configurable proxy endpoints.

    # Use asyncio.gather without importing globally to keep deps minimal.
    gather = __import__("asyncio").gather
    gathered = await gather(*tasks, return_exceptions=True)
    for r in gathered:
        if isinstance(r, Exception):
            continue
        out.extend(r)

    return _dedupe(out)[: settings.search.aggregate_limit]
