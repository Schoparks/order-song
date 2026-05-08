from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

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


async def analyze_remote_audio_loudness(
    audio_url: str,
    *,
    headers: Mapping[str, str] | None = None,
    source: str = "ffmpeg-ebur128",
) -> LoudnessAnalysisResult:
    if not settings.audio_loudness.enabled:
        return LoudnessAnalysisResult(error="audio loudness analysis disabled")
    ffmpeg = _resolve_ffmpeg()
    if not ffmpeg:
        return LoudnessAnalysisResult(error="ffmpeg not found")
    if not audio_url:
        return LoudnessAnalysisResult(error="audio analysis url missing")

    command = [
        ffmpeg,
        "-hide_banner",
        "-nostdin",
        "-v",
        "info",
    ]
    if headers:
        header_text = "".join(f"{key}: {value}\r\n" for key, value in headers.items())
        command.extend(["-headers", header_text])
    command.extend(["-i", audio_url, "-vn"])
    if settings.audio_loudness.max_duration_s > 0:
        command.extend(["-t", str(settings.audio_loudness.max_duration_s)])
    command.extend(["-af", "ebur128=peak=true", "-f", "null", "-"])

    def _run() -> LoudnessAnalysisResult:
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                text=True,
                timeout=settings.audio_loudness.timeout_s,
                check=False,
            )
        except FileNotFoundError:
            return LoudnessAnalysisResult(error="ffmpeg not found")
        except subprocess.TimeoutExpired:
            return LoudnessAnalysisResult(error="ffmpeg loudness analysis timeout")
        except Exception as exc:
            return LoudnessAnalysisResult(error=f"ffmpeg loudness analysis failed: {exc}")

        parsed = _parse_ebur128(completed.stderr or "", source=source)
        if parsed.analysis:
            return parsed
        if completed.returncode != 0:
            tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
            detail = tail[0][:160] if tail else f"ffmpeg exited {completed.returncode}"
            return LoudnessAnalysisResult(error=detail)
        return parsed

    return await asyncio.to_thread(_run)
