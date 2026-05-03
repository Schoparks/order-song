from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional
from urllib.parse import parse_qsl


_BVID_RE = re.compile(r"^(BV[0-9A-Za-z]{10})", re.IGNORECASE)


@dataclass(frozen=True)
class BilibiliTrackRef:
    bvid: str
    page: Optional[int] = None
    cid: Optional[int] = None

    @property
    def is_part(self) -> bool:
        return self.page is not None or self.cid is not None


def _positive_int(value: Any) -> Optional[int]:
    try:
        current = int(value)
    except (TypeError, ValueError):
        return None
    return current if current > 0 else None


def make_bilibili_source_track_id(bvid: str, *, page: int | None = None, cid: int | None = None) -> str:
    page = _positive_int(page)
    cid = _positive_int(cid)
    if not page and not cid:
        return bvid
    parts = []
    if page:
        parts.append(f"p={page}")
    if cid:
        parts.append(f"cid={cid}")
    return f"{bvid}#{'&'.join(parts)}"


def parse_bilibili_source_track_id(source_track_id: str) -> BilibiliTrackRef:
    value = (source_track_id or "").strip()
    match = _BVID_RE.match(value)
    if not match:
        return BilibiliTrackRef(bvid=value)

    bvid = match.group(1)
    suffix = value[match.end():]
    page: Optional[int] = None
    cid: Optional[int] = None
    if suffix.startswith(("#", "?")):
        for key, raw_value in parse_qsl(suffix[1:], keep_blank_values=False):
            if key == "p":
                page = _positive_int(raw_value) or page
            elif key == "cid":
                cid = _positive_int(raw_value) or cid
    return BilibiliTrackRef(bvid=bvid, page=page, cid=cid)


def normalize_bilibili_pic(value: Any) -> Optional[str]:
    if not isinstance(value, str) or not value.strip():
        return None
    pic = value.strip()
    if pic.startswith("//"):
        return f"https:{pic}"
    return pic


def find_bilibili_page(video: dict[str, Any] | None, ref: BilibiliTrackRef) -> dict[str, Any] | None:
    if not video:
        return None
    pages = video.get("pages")
    if not isinstance(pages, list) or not pages:
        return None

    if ref.cid:
        for page in pages:
            if isinstance(page, dict) and _positive_int(page.get("cid")) == ref.cid:
                return page
    if ref.page:
        for page in pages:
            if isinstance(page, dict) and _positive_int(page.get("page")) == ref.page:
                return page
        index = ref.page - 1
        if 0 <= index < len(pages) and isinstance(pages[index], dict):
            return pages[index]

    first = pages[0]
    return first if isinstance(first, dict) else None


def bilibili_page_url(source_track_id: str) -> str:
    ref = parse_bilibili_source_track_id(source_track_id)
    url = f"https://www.bilibili.com/video/{ref.bvid}"
    if ref.page:
        url = f"{url}?p={ref.page}"
    return url

