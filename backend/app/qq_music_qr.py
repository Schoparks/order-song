from __future__ import annotations

import math
import os
import sys
from io import BytesIO
from typing import Any


def validate_qr_png(data: bytes) -> tuple[int, int]:
    try:
        from PIL import Image
    except ImportError as exc:  # pragma: no cover - guarded by requirements/startup
        raise RuntimeError("Pillow 未安装，无法读取 QQ 登录二维码") from exc
    if not isinstance(data, bytes) or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        raise ValueError("QQ 登录二维码不是完整的 PNG 图片")
    with Image.open(BytesIO(data)) as source:
        if source.format != "PNG" or source.width < 21 or source.height < 21:
            raise ValueError("QQ 登录二维码尺寸或格式异常")
        size = source.size
        source.verify()
    return size


def _binary_run_gcd(image: Any) -> int:
    """Infer the integer pixel scale used by a black-and-white QR image."""
    width, height = image.size
    scale = 0
    for y in range(height):
        last = image.getpixel((0, y))
        run = 1
        for x in range(1, width):
            value = image.getpixel((x, y))
            if value == last:
                run += 1
                continue
            scale = math.gcd(scale, run)
            last = value
            run = 1
        scale = math.gcd(scale, run)
    return max(1, scale)


def _qr_modules_from_png(data: bytes) -> list[list[bool]]:
    try:
        from PIL import Image, ImageOps
    except ImportError as exc:  # pragma: no cover - guarded by requirements/startup
        raise RuntimeError("Pillow 未安装，无法读取 QQ 登录二维码") from exc

    validate_qr_png(data)
    with Image.open(BytesIO(data)) as source:
        source.load()
        binary = source.convert("L").point(lambda value: 255 if value > 127 else 0, mode="1")
        bbox = ImageOps.invert(binary.convert("L")).getbbox()
        if not bbox:
            raise ValueError("QQ 登录二维码图片为空")
        symbol = binary.crop(bbox)
        scale = _binary_run_gcd(symbol)
        columns = max(1, round(symbol.width / scale))
        rows = max(1, round(symbol.height / scale))
        modules = symbol.resize((columns, rows), resample=Image.Resampling.NEAREST)
        return [
            [modules.getpixel((x, y)) == 0 for x in range(columns)]
            for y in range(rows)
        ]


def _with_quiet_zone(modules: list[list[bool]], quiet: int = 4) -> list[list[bool]]:
    width = len(modules[0])
    white = [False] * (width + quiet * 2)
    return [white[:] for _ in range(quiet)] + [
        [False] * quiet + row + [False] * quiet for row in modules
    ] + [white[:] for _ in range(quiet)]


def _ansi_supported() -> bool:
    return bool(
        sys.stdout.isatty()
        and os.getenv("TERM", "").lower() != "dumb"
        and "NO_COLOR" not in os.environ
    )


def _render_ansi(modules: list[list[bool]]) -> str:
    lines: list[str] = []
    for row in modules:
        line: list[str] = []
        current: bool | None = None
        for black in row:
            if black != current:
                line.append("\x1b[40m" if black else "\x1b[47m")
                current = black
            line.append("  ")
        line.append("\x1b[0m")
        lines.append("".join(line))
    return "\n".join(lines)


def _render_half_blocks(modules: list[list[bool]]) -> str:
    if len(modules) % 2:
        modules.append([False] * len(modules[0]))
    glyphs = {
        (False, False): " ",
        (True, False): "▀",
        (False, True): "▄",
        (True, True): "█",
    }
    return "\n".join(
        "".join(glyphs[(top, bottom)] for top, bottom in zip(modules[y], modules[y + 1]))
        for y in range(0, len(modules), 2)
    )


def terminal_qr_from_png(data: bytes, *, ansi: bool | None = None) -> str:
    """Render a QR without spaces between modules; the original PNG remains the reliable fallback."""
    modules = _with_quiet_zone(_qr_modules_from_png(data))
    use_ansi = _ansi_supported() if ansi is None else ansi
    return _render_ansi(modules) if use_ansi else _render_half_blocks(modules)
