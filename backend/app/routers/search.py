import re
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.deps import get_current_user
from app.models import TrackSource, User
from app.schemas import SearchTrackOut


router = APIRouter(prefix="/api", tags=["search"])


_BV_IN_TEXT_RE = re.compile(r"(BV[0-9A-Za-z]{10})", re.IGNORECASE)


async def _bili_video_by_bv(bv: str) -> dict[str, Any]:
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}) as client:
        r = await client.get("https://api.bilibili.com/x/web-interface/view", params={"bvid": bv})
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail="bilibili view api error")
        return data["data"]


async def _bili_search(keyword: str, page: int = 1) -> list[dict[str, Any]]:
    async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"}) as client:
        r = await client.get(
            "https://api.bilibili.com/x/web-interface/search/type",
            params={"search_type": "video", "keyword": keyword, "page": page},
        )
        r.raise_for_status()
        data = r.json()
        if data.get("code") != 0:
            raise HTTPException(status_code=400, detail="bilibili search api error")
        return data.get("data", {}).get("result", []) or []


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
        m = _BV_IN_TEXT_RE.search(q)
        if m:
            bv = m.group(1)
            try:
                v = await _bili_video_by_bv(bv)
                return [
                    SearchTrackOut(
                        source=TrackSource.bilibili,
                        source_track_id=bv,
                        title=v.get("title") or bv,
                        artist=(v.get("owner") or {}).get("name"),
                        duration_ms=int((v.get("duration") or 0) * 1000) if v.get("duration") is not None else None,
                        cover_url=v.get("pic"),
                    )
                ]
            except Exception:
                # BV 号检索不到对应视频：不返回结果
                return []

        try:
            results = await _bili_search(q, page=1)
            out2: list[SearchTrackOut] = []
            for item in results[:10]:
                bvid = item.get("bvid")
                if not bvid:
                    continue
                out2.append(
                    SearchTrackOut(
                        source=TrackSource.bilibili,
                        source_track_id=bvid,
                        title=(item.get("title") or "").replace("<em class=\"keyword\">", "").replace("</em>", ""),
                        artist=item.get("author"),
                        duration_ms=None,
                        cover_url=("https:" + item["pic"]) if isinstance(item.get("pic"), str) and item["pic"].startswith("//") else item.get("pic"),
                    )
                )
            return out2
        except Exception:
            return []

    async def run_netease() -> list[SearchTrackOut]:
        async with httpx.AsyncClient(timeout=10.0, headers={"User-Agent": "Mozilla/5.0", "Referer": "https://music.163.com/"}) as client:
            r = await client.get("https://music.163.com/api/search/get", params={"s": q, "type": 1, "limit": 20})
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
            if len(out2) >= 10:
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

    return _dedupe(out)[:40]

