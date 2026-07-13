import asyncio
import io
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app.core.config import settings  # noqa: E402
from app.models import TrackSource  # noqa: E402
from app.qq_music import QQMusicService, terminal_qr_from_png  # noqa: E402
from app.qq_music_qr import validate_qr_png  # noqa: E402
from app.main import _lifespan  # noqa: E402
from app.routers.playlists_trending import _parse_playlist_link, import_playlist  # noqa: E402
from app.routers.search import search  # noqa: E402


class PlaylistLinkTests(unittest.TestCase):
    def test_recognizes_netease_playlist_links(self) -> None:
        self.assertEqual(
            _parse_playlist_link("https://music.163.com/#/playlist?id=123456"),
            (TrackSource.netease, "123456"),
        )
        self.assertEqual(
            _parse_playlist_link("https://music.163.com/playlist/987654"),
            (TrackSource.netease, "987654"),
        )

    def test_recognizes_qq_playlist_links(self) -> None:
        self.assertEqual(
            _parse_playlist_link("https://y.qq.com/n/ryqq/playlist/123456"),
            (TrackSource.qq, "123456"),
        )
        self.assertEqual(
            _parse_playlist_link("https://i.y.qq.com/n2/m/share/details/taoge.html?id=987654"),
            (TrackSource.qq, "987654"),
        )

    def test_rejects_non_playlist_and_unknown_links(self) -> None:
        cases = (
            "https://music.163.com/song?id=123",
            "https://y.qq.com/n/ryqq/songDetail/003abc",
            "https://example.com/playlist/123",
            "123456",
        )
        for value in cases:
            with self.subTest(value=value), self.assertRaises(HTTPException) as raised:
                _parse_playlist_link(value)
            self.assertEqual(raised.exception.status_code, 400)

    def test_qq_link_reports_disabled_parser(self) -> None:
        async def run() -> None:
            with patch.object(settings.qq_music, "enabled", False):
                with self.assertRaises(HTTPException) as raised:
                    await import_playlist(
                        {"url": "https://y.qq.com/n/ryqq/playlist/123456"},
                        db=SimpleNamespace(),
                        user=SimpleNamespace(id=1),
                    )
            self.assertEqual(raised.exception.status_code, 400)
            self.assertIn("QQ 音乐解析未开启", raised.exception.detail)

        asyncio.run(run())

    def test_qq_search_reports_disabled_parser(self) -> None:
        async def run() -> None:
            with patch.object(settings.qq_music, "enabled", False):
                with self.assertRaises(HTTPException) as raised:
                    await search("test", source="qq", user=SimpleNamespace())
            self.assertEqual(raised.exception.status_code, 400)
            self.assertEqual(raised.exception.detail, "QQ 音乐解析未开启")

        asyncio.run(run())


class QQTrackEligibilityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = QQMusicService()
        self.service._song_file_type = SimpleNamespace(MP3_128="mp3", ACC_96="aac96", ACC_48="aac48")
        self.service._song_file_info_type = lambda *args: args

    @staticmethod
    def song(
        *,
        status: int = 0,
        pay_play: int = 0,
        pay_month: int = 0,
        time_free: int = 0,
        interval: int = 180,
    ):
        return SimpleNamespace(
            mid="003abc",
            type=1,
            status=status,
            interval=interval,
            pay=SimpleNamespace(pay_play=pay_play, pay_month=pay_month, time_free=time_free),
            file=SimpleNamespace(size_128mp3=1024, size_96aac=0, size_48aac=0, media_mid="003abc"),
        )

    def test_accepts_only_free_valid_tracks(self) -> None:
        self.assertTrue(self.service._is_free_candidate(self.song()))
        # pay_month describes subscription quality/features. Standard playback
        # can still be a complete free file when pay_play is zero.
        self.assertTrue(self.service._is_free_candidate(self.song(pay_month=1)))
        self.assertFalse(self.service._is_free_candidate(self.song(pay_play=1)))
        self.assertFalse(self.service._is_free_candidate(self.song(time_free=1)))
        self.assertFalse(self.service._is_free_candidate(self.song(status=1)))
        self.assertFalse(self.service._is_free_candidate(self.song(interval=0)))
        self.assertFalse(self.service._is_free_candidate(self.song(interval=settings.search.max_duration_s)))

    def test_accepts_only_trusted_full_standard_urls(self) -> None:
        item = SimpleNamespace(vkey="key", filename="M500003abc.mp3")
        self.assertTrue(
            self.service._is_full_standard_url(
                item,
                "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=key",
            )
        )
        self.assertFalse(
            self.service._is_full_standard_url(
                item,
                "https://isure.stream.qqmusic.qq.com/RS02003abc.mp3?vkey=key",
            )
        )
        self.assertFalse(
            self.service._is_full_standard_url(
                item,
                "https://example.com/M500003abc.mp3?vkey=key",
            )
        )
        self.assertFalse(
            self.service._is_full_standard_url(
                item,
                "https://user@isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=key",
            )
        )
        self.assertFalse(
            self.service._is_full_standard_url(
                item,
                "https://isure.stream.qqmusic.qq.com:444/M500003abc.mp3?vkey=key",
            )
        )


class QQServiceLifecycleTests(unittest.TestCase):
    @staticmethod
    def qr_png() -> bytes:
        from PIL import Image, ImageDraw

        size = 21
        scale = 4
        modules = [[False] * size for _ in range(size)]

        def finder(left: int, top: int) -> None:
            for y in range(7):
                for x in range(7):
                    modules[top + y][left + x] = (
                        x in (0, 6) or y in (0, 6) or (2 <= x <= 4 and 2 <= y <= 4)
                    )

        finder(0, 0)
        finder(14, 0)
        finder(0, 14)
        for index in range(8, 21):
            modules[index][index] = index % 2 == 0
            modules[index][20 - index] = index % 3 == 0
        image = Image.new("L", (size * scale, size * scale), 255)
        draw = ImageDraw.Draw(image)
        for y, row in enumerate(modules):
            for x, black in enumerate(row):
                if black:
                    draw.rectangle(
                        (x * scale, y * scale, (x + 1) * scale - 1, (y + 1) * scale - 1),
                        fill=0,
                    )
        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()

    @staticmethod
    def ready_service() -> QQMusicService:
        service = QQMusicService()
        service._credential_type = lambda: SimpleNamespace(musicid=0, musickey="")
        service._client = SimpleNamespace(
            credential=SimpleNamespace(musicid=1, musickey="old-key"),
            _device_store=SimpleNamespace(device=None),
            close=AsyncMock(),
        )
        return service

    def test_start_prints_restored_login_status_to_stdout(self) -> None:
        async def run() -> None:
            service = self.ready_service()
            service._client.login = SimpleNamespace(check_expired=AsyncMock(return_value=False))
            output = io.StringIO()
            with (
                patch.object(settings.qq_music, "enabled", True),
                patch.object(service, "_load_dependency"),
                patch.object(service, "_restore_device", AsyncMock()),
                patch("sys.stdout", output),
            ):
                await service.start()
                await service.close()
            self.assertEqual(output.getvalue(), "QQ 音乐：登录态已恢复，可用\n")

        asyncio.run(run())

    def test_start_without_login_does_not_print_restored_status(self) -> None:
        async def run() -> None:
            service = self.ready_service()
            service._client.credential = SimpleNamespace(musicid=0, musickey="")
            output = io.StringIO()
            with (
                patch.object(settings.qq_music, "enabled", True),
                patch.object(service, "_load_dependency"),
                patch.object(service, "_restore_device", AsyncMock()),
                patch.object(service, "_create_qr_session", AsyncMock(return_value=object())),
                patch.object(service, "_ensure_login_task") as ensure_login,
                patch("sys.stdout", output),
            ):
                await service.start()
                await service.close()
            self.assertNotIn("登录态已恢复", output.getvalue())
            ensure_login.assert_called_once()

        asyncio.run(run())

    def test_start_when_disabled_has_no_qq_stdout(self) -> None:
        async def run() -> None:
            service = QQMusicService()
            output = io.StringIO()
            with (
                patch.object(settings.qq_music, "enabled", False),
                patch.object(service, "_load_dependency") as load_dependency,
                patch("sys.stdout", output),
            ):
                await service.start()
            self.assertEqual(output.getvalue(), "")
            load_dependency.assert_not_called()

        asyncio.run(run())

    def test_upstream_operation_has_hard_timeout(self) -> None:
        async def run() -> None:
            service = self.ready_service()

            async def hangs_forever():
                await asyncio.Event().wait()

            with (
                patch.object(settings.qq_music, "enabled", True),
                patch.object(settings.qq_music, "request_timeout_s", 0.01),
                patch.object(service, "_save_device", AsyncMock()),
            ):
                with self.assertRaises(TimeoutError):
                    await service._authenticated(hangs_forever)

        asyncio.run(run())

    def test_second_auth_failure_clears_session_and_starts_qr_login(self) -> None:
        class AuthFailure(RuntimeError):
            pass

        async def run() -> None:
            service = self.ready_service()
            service._credential_errors = (AuthFailure,)
            operation = AsyncMock(side_effect=[AuthFailure(), AuthFailure()])
            with tempfile.TemporaryDirectory() as directory:
                with (
                    patch.object(settings.qq_music, "enabled", True),
                    patch.object(settings.qq_music, "session_path", str(Path(directory) / "session.json")),
                    patch.object(service, "_refresh_credential", AsyncMock()),
                    patch.object(service, "_save_device", AsyncMock()),
                    patch.object(service, "_ensure_login_task") as ensure_login,
                ):
                    with self.assertRaisesRegex(RuntimeError, "重新扫码"):
                        await service._authenticated(operation)
            self.assertFalse(service.logged_in)
            ensure_login.assert_called_once_with()
            self.assertEqual(operation.await_count, 2)

        asyncio.run(run())

    def test_audio_url_cache_expires_and_is_disabled_with_feature(self) -> None:
        service = self.ready_service()
        url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=key"
        service._audio_urls["003abc"] = (url, time.monotonic() + 60)
        with patch.object(settings.qq_music, "enabled", True):
            self.assertEqual(service.current_audio_url("003abc", url), url)
            self.assertIsNone(service.current_audio_url("003abc", None))
            service._audio_urls["003abc"] = (url, time.monotonic() - 1)
            self.assertIsNone(service.current_audio_url("003abc", url))
        service._audio_urls["003abc"] = (url, time.monotonic() + 60)
        with patch.object(settings.qq_music, "enabled", False):
            self.assertIsNone(service.current_audio_url("003abc", url))

    def test_atomic_json_file_is_private_and_leaves_no_temp_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "session.json"
            QQMusicService._atomic_json_write(path, {"token": "secret"})
            if os.name != "nt":
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(list(Path(directory).glob("*.tmp")), [])

    def test_terminal_qr_uses_gap_free_half_blocks_or_ansi_cells(self) -> None:
        png = self.qr_png()
        self.assertEqual(validate_qr_png(png), (84, 84))

        fallback = terminal_qr_from_png(png, ansi=False)
        fallback_lines = fallback.splitlines()
        self.assertEqual(len(fallback_lines), 15)
        self.assertTrue(all(len(line) == 29 for line in fallback_lines))
        self.assertNotIn("\x1b[", fallback)
        self.assertTrue(any(glyph in fallback for glyph in ("▀", "▄", "█")))

        ansi = terminal_qr_from_png(png, ansi=True)
        self.assertEqual(len(ansi.splitlines()), 29)
        self.assertIn("\x1b[40m", ansi)
        self.assertIn("\x1b[47m", ansi)
        self.assertTrue(all(line.endswith("\x1b[0m") for line in ansi.splitlines()))

    def test_qr_png_is_exact_private_and_cleaned(self) -> None:
        service = QQMusicService()
        png = self.qr_png()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "login.png"
            with patch.object(settings.qq_music, "qr_path", str(path)):
                self.assertEqual(service._save_qr_png(png), path)
                self.assertEqual(path.read_bytes(), png)
                self.assertEqual(validate_qr_png(path.read_bytes()), (84, 84))
                if os.name != "nt":
                    self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(list(Path(directory).glob("*.tmp")), [])
                service._clear_qr_png()
                self.assertFalse(path.exists())

    def test_login_success_and_expiry_remove_qr_png(self) -> None:
        async def run() -> None:
            service = self.ready_service()
            service._client.credential = SimpleNamespace(musicid=0, musickey="")
            credential = SimpleNamespace(musicid=2, musickey="new-key")
            success_session = SimpleNamespace(wait_qrcode_login=AsyncMock(return_value=credential))
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "login.png"
                with (
                    patch.object(settings.qq_music, "enabled", True),
                    patch.object(settings.qq_music, "qr_path", str(path)),
                    patch.object(service, "_save_credential"),
                ):
                    service._save_qr_png(self.qr_png())
                    await service._login_loop(success_session)
                    self.assertFalse(path.exists())

                    service._client.credential = SimpleNamespace(musicid=0, musickey="")

                    async def expire():
                        settings.qq_music.enabled = False
                        raise RuntimeError("expired")

                    expired_session = SimpleNamespace(wait_qrcode_login=expire)
                    settings.qq_music.enabled = True
                    service._save_qr_png(self.qr_png())
                    with patch("app.qq_music.asyncio.sleep", AsyncMock()):
                        await service._login_loop(expired_session)
                    self.assertFalse(path.exists())

                    service._save_qr_png(self.qr_png())
                    await service.close()
                    self.assertFalse(path.exists())

        asyncio.run(run())

    def test_lifespan_closes_partially_started_qq_client(self) -> None:
        async def run() -> None:
            with (
                patch("app.main.validate_auth_configuration"),
                patch("app.main.init_db"),
                patch("app.main.qq_music.start", AsyncMock(side_effect=RuntimeError("start failed"))),
                patch("app.main.qq_music.close", AsyncMock()) as close,
            ):
                with self.assertRaisesRegex(RuntimeError, "start failed"):
                    async with _lifespan(SimpleNamespace()):
                        pass
            close.assert_awaited_once_with()

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
