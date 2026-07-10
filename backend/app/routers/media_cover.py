import asyncio
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import httpx
from fastapi import HTTPException
from starlette.responses import Response

from app.core.config import settings


_COVER_PROXY_HOST_SUFFIXES = (
    "bilibili.com",
    "biliimg.com",
    "hdslb.com",
)
_MAX_COVER_BYTES = 8 * 1024 * 1024
_MAX_COVER_REDIRECTS = 3
_ALLOWED_COVER_CONTENT_TYPES = {
    "image/avif",
    "image/gif",
    "image/jpeg",
    "image/png",
    "image/webp",
}


def _validate_cover_url(url: str) -> str:
    try:
        parsed = urlparse(url)
        port = parsed.port
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="invalid cover url") from exc
    host = (parsed.hostname or "").rstrip(".").lower()
    if (
        parsed.scheme not in {"http", "https"}
        or parsed.username
        or parsed.password
        or (port is not None and port not in {80, 443})
        or not any(host == suffix or host.endswith(f".{suffix}") for suffix in _COVER_PROXY_HOST_SUFFIXES)
    ):
        raise HTTPException(status_code=400, detail="unsupported cover host")
    return host


def _cover_host_is_public(host: str) -> bool:
    try:
        addresses = {
            item[4][0]
            for item in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except OSError:
        return False
    return bool(addresses) and all(ipaddress.ip_address(address).is_global for address in addresses)


async def fetch_cover_response(url: str) -> Response:
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer": "https://www.bilibili.com/",
        "Accept": "image/avif,image/webp,image/png,image/jpeg,image/gif",
    }
    try:
        async with httpx.AsyncClient(
            timeout=settings.upstream.bilibili_audio_timeout_s,
            follow_redirects=False,
            headers=headers,
        ) as client:
            current_url = url
            for redirect_count in range(_MAX_COVER_REDIRECTS + 1):
                host = _validate_cover_url(current_url)
                if not await asyncio.to_thread(_cover_host_is_public, host):
                    raise HTTPException(status_code=400, detail="cover host is not public")
                async with client.stream("GET", current_url) as response:
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        if not location or redirect_count >= _MAX_COVER_REDIRECTS:
                            raise HTTPException(status_code=502, detail="invalid cover redirect")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    content_type = (response.headers.get("content-type") or "").split(";", 1)[0].lower()
                    if content_type not in _ALLOWED_COVER_CONTENT_TYPES:
                        raise HTTPException(status_code=502, detail="cover response is not an image")
                    declared_size = response.headers.get("content-length")
                    if declared_size and int(declared_size) > _MAX_COVER_BYTES:
                        raise HTTPException(status_code=413, detail="cover image too large")
                    chunks: list[bytes] = []
                    total = 0
                    async for chunk in response.aiter_bytes():
                        total += len(chunk)
                        if total > _MAX_COVER_BYTES:
                            raise HTTPException(status_code=413, detail="cover image too large")
                        chunks.append(chunk)
                    content = b"".join(chunks)
                    break
            else:
                raise HTTPException(status_code=502, detail="cover fetch failed")
    except HTTPException:
        raise
    except (httpx.HTTPError, OSError, ValueError):
        raise HTTPException(status_code=502, detail="cover fetch failed")

    return Response(
        content=content,
        media_type=content_type,
        headers={"Cache-Control": "public, max-age=86400"},
    )
