from __future__ import annotations

import asyncio
import ipaddress
import re
import shutil
import socket
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping
from urllib.parse import urljoin, urlparse

import httpx

from app.core.config import settings


@dataclass(frozen=True)
class LoudnessAnalysis:
    gain_db: float
    peak: float | None
    source: str


@dataclass(frozen=True)
class LoudnessAnalysisResult:
    analysis: LoudnessAnalysis | None = None
    error: str | None = None


_INTEGRATED_RE = re.compile(r"\bI:\s*(-?\d+(?:\.\d+)?)\s+LUFS\b")
_TRUE_PEAK_RE = re.compile(r"\bPeak:\s*(-?\d+(?:\.\d+)?)\s+dBFS\b")
_MAX_REDIRECTS = 4
_MAX_ANALYSIS_DOWNLOAD_BYTES = 128 * 1024 * 1024
_TRUSTED_AUDIO_HOST_SUFFIXES = {
    "bilibili": (
        "bilivideo.com",
        "bilivideo.cn",
        "akamaized.net",
    ),
    "netease": (
        "music.126.net",
        "music.163.com",
    ),
}


def _configured_ffmpeg_path() -> str:
    raw = settings.audio_loudness.ffmpeg_path.strip().strip("\"'")
    if not raw:
        return "ffmpeg"
    path = Path(raw)
    if path.is_dir():
        candidate = path / ("ffmpeg.exe" if _is_windows_path(raw) else "ffmpeg")
        return candidate.as_posix()
    return raw


def _is_windows_path(value: str) -> bool:
    return "\\" in value or ":" in value


def _resolve_ffmpeg() -> str | None:
    configured = _configured_ffmpeg_path()
    if Path(configured).is_absolute() or "/" in configured or "\\" in configured:
        return configured if Path(configured).exists() else None
    return shutil.which(configured)


def is_loudness_analysis_available() -> bool:
    return settings.audio_loudness.enabled and _resolve_ffmpeg() is not None


def _clamp_gain_db(value: float) -> float:
    low = min(settings.audio_loudness.min_gain_db, settings.audio_loudness.max_gain_db)
    high = max(settings.audio_loudness.min_gain_db, settings.audio_loudness.max_gain_db)
    return max(low, min(high, value))


def _parse_ebur128(stderr: str, *, source: str) -> LoudnessAnalysisResult:
    summary = stderr.rsplit("Summary:", 1)[-1]
    integrated_matches = _INTEGRATED_RE.findall(summary)
    if not integrated_matches:
        integrated_matches = _INTEGRATED_RE.findall(stderr)
    if not integrated_matches:
        return LoudnessAnalysisResult(error="ffmpeg ebur128 loudness not found")

    integrated_lufs = float(integrated_matches[-1])
    peak_db: float | None = None
    peak_matches = _TRUE_PEAK_RE.findall(summary) or _TRUE_PEAK_RE.findall(stderr)
    if peak_matches:
        peak_db = float(peak_matches[-1])

    gain_db = settings.audio_loudness.target_lufs - integrated_lufs
    if peak_db is not None:
        gain_db = min(gain_db, settings.audio_loudness.true_peak_headroom_db - peak_db)
    gain_db = _clamp_gain_db(gain_db)
    peak = 10 ** (peak_db / 20) if peak_db is not None else None
    return LoudnessAnalysisResult(
        analysis=LoudnessAnalysis(
            gain_db=gain_db,
            peak=peak,
            source=source,
        )
    )


def _trusted_source_key(source: str) -> str:
    return source.strip().lower().split(":", 1)[0]


def _host_matches_suffix(host: str, suffix: str) -> bool:
    return host == suffix or host.endswith(f".{suffix}")


def validate_trusted_audio_url(audio_url: str, trusted_source: str) -> str | None:
    """Return a stable error string when a resolver URL is unsafe for backend fetches."""
    try:
        parsed = urlparse(audio_url)
        port = parsed.port
    except (TypeError, ValueError):
        return "invalid audio analysis url"
    if parsed.scheme not in {"http", "https"}:
        return "unsupported audio analysis url scheme"
    if not parsed.hostname or parsed.username or parsed.password:
        return "invalid audio analysis url host"
    if port is not None and port not in {80, 443}:
        return "unsupported audio analysis url port"

    host = parsed.hostname.rstrip(".").lower()
    suffixes = _TRUSTED_AUDIO_HOST_SUFFIXES.get(_trusted_source_key(trusted_source), ())
    if not suffixes or not any(_host_matches_suffix(host, suffix) for suffix in suffixes):
        return "untrusted audio analysis url host"
    return None


def _validate_public_dns(audio_url: str) -> str | None:
    host = urlparse(audio_url).hostname
    if not host:
        return "invalid audio analysis url host"
    try:
        addresses = {
            address[0]
            for _, _, _, _, address in socket.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        }
    except OSError:
        return "audio analysis host resolution failed"
    if not addresses:
        return "audio analysis host resolution failed"
    for address in addresses:
        try:
            if not ipaddress.ip_address(address).is_global:
                return "audio analysis host resolved to non-public address"
        except ValueError:
            return "audio analysis host resolution invalid"
    return None


async def analyze_remote_audio_loudness(
    audio_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    source: str = "ffmpeg-ebur128",
    trusted_source: str,
) -> LoudnessAnalysisResult:
    if not settings.audio_loudness.enabled:
        return LoudnessAnalysisResult(error="audio loudness analysis disabled")
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return LoudnessAnalysisResult(error="ffmpeg not found")
    if not audio_url:
        return LoudnessAnalysisResult(error="audio analysis url missing")

    unsafe_reason = validate_trusted_audio_url(audio_url, trusted_source)
    if unsafe_reason:
        return LoudnessAnalysisResult(error=unsafe_reason)

    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
        "-protocol_whitelist",
        "pipe",
    ]
    command.extend(["-i", "pipe:0", "-vn"])
    if settings.audio_loudness.max_duration_s > 0:
        command.extend(["-t", str(settings.audio_loudness.max_duration_s)])
    command.extend(["-af", "ebur128=peak=true", "-f", "null", "-"])

    def _run() -> LoudnessAnalysisResult:
        deadline = time.monotonic() + settings.audio_loudness.timeout_s
        request_headers = dict(headers or {})
        request_headers["Range"] = "bytes=0-"
        try:
            with httpx.Client(
                timeout=settings.audio_loudness.timeout_s,
                follow_redirects=False,
            ) as client:
                current_url = audio_url
                response_context = None
                for redirect_count in range(_MAX_REDIRECTS + 1):
                    unsafe = validate_trusted_audio_url(current_url, trusted_source) or _validate_public_dns(current_url)
                    if unsafe:
                        return LoudnessAnalysisResult(error=unsafe)
                    response_context = client.stream("GET", current_url, headers=request_headers)
                    response = response_context.__enter__()
                    if response.status_code in {301, 302, 303, 307, 308}:
                        location = response.headers.get("location")
                        response_context.__exit__(None, None, None)
                        response_context = None
                        if not location:
                            return LoudnessAnalysisResult(error="audio analysis redirect missing location")
                        if redirect_count >= _MAX_REDIRECTS:
                            return LoudnessAnalysisResult(error="too many audio analysis redirects")
                        current_url = urljoin(current_url, location)
                        continue
                    response.raise_for_status()
                    break
                if response_context is None:
                    return LoudnessAnalysisResult(error="audio analysis fetch failed")

                try:
                    with tempfile.TemporaryFile(mode="w+b") as stderr_file:
                        process = subprocess.Popen(
                            command,
                            stdin=subprocess.PIPE,
                            stdout=subprocess.DEVNULL,
                            stderr=stderr_file,
                        )
                        try:
                            assert process.stdin is not None
                            downloaded_bytes = 0
                            for chunk in response.iter_bytes():
                                if time.monotonic() >= deadline:
                                    raise subprocess.TimeoutExpired(command, settings.audio_loudness.timeout_s)
                                downloaded_bytes += len(chunk)
                                if downloaded_bytes > _MAX_ANALYSIS_DOWNLOAD_BYTES:
                                    raise ValueError("audio analysis download exceeded byte limit")
                                try:
                                    process.stdin.write(chunk)
                                except BrokenPipeError:
                                    break
                            try:
                                process.stdin.close()
                            except BrokenPipeError:
                                pass
                            remaining = max(0.1, deadline - time.monotonic())
                            returncode = process.wait(timeout=remaining)
                        except BaseException:
                            process.kill()
                            process.wait()
                            raise
                        finally:
                            response_context.__exit__(None, None, None)
                            response_context = None

                        stderr_file.seek(0)
                        stderr = stderr_file.read().decode("utf-8", errors="replace")
                except BaseException:
                    if response_context is not None:
                        response_context.__exit__(None, None, None)
                    raise
        except FileNotFoundError:
            return LoudnessAnalysisResult(error="ffmpeg not found")
        except subprocess.TimeoutExpired:
            return LoudnessAnalysisResult(error="ffmpeg loudness analysis timeout")
        except httpx.HTTPError as exc:
            return LoudnessAnalysisResult(error=f"audio analysis fetch failed: {exc}")
        except Exception as exc:
            return LoudnessAnalysisResult(error=f"ffmpeg loudness analysis failed: {exc}")

        parsed = _parse_ebur128(stderr, source=source)
        if parsed.analysis:
            return parsed
        if returncode != 0:
            tail = stderr.strip().splitlines()[-1:]
            detail = tail[0][:160] if tail else f"ffmpeg exited {returncode}"
            return LoudnessAnalysisResult(error=detail)
        return parsed

    return await asyncio.to_thread(_run)
