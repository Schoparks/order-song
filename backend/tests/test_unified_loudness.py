from __future__ import annotations

import asyncio
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from sqlmodel import Session, SQLModel, create_engine

BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import audio_loudness  # noqa: E402
from app.audio_loudness import LoudnessAnalysisResult  # noqa: E402
from app.models import (  # noqa: E402
    QueueStatus,
    Room,
    RoomMode,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackSource,
    User,
)
from app.qq_music import QQMusicService  # noqa: E402
from app.routers import playback_core  # noqa: E402
from app.routers.playback_audio import (  # noqa: E402
    AudioResolveResult,
    _resolve_bilibili_audio,
    _resolve_netease_audio,
    _resolve_qq_audio,
)


class UnifiedLoudnessContractTests(unittest.TestCase):
    def _track(
        self,
        source: TrackSource,
        *,
        loudness_source: str | None = None,
        gain: float | None = None,
    ) -> Track:
        return Track(
            source=source,
            source_track_id=f"{source.value}-id",
            title=source.value,
            loudness_source=loudness_source,
            loudness_gain_db=gain,
        )

    def test_only_matching_ffmpeg_marker_is_accepted_for_each_source(self) -> None:
        for source in (TrackSource.bilibili, TrackSource.netease, TrackSource.qq):
            with self.subTest(source=source):
                expected = f"{source.value}:ffmpeg-ebur128"
                self.assertTrue(
                    playback_core._track_has_loudness(
                        self._track(source, loudness_source=expected, gain=-2.0)
                    )
                )
                self.assertFalse(
                    playback_core._track_has_loudness(
                        self._track(
                            source,
                            loudness_source="bilibili:playurl-volume",
                            gain=-2.0,
                        )
                    )
                )
                other = "qq:ffmpeg-ebur128" if source != TrackSource.qq else "netease:ffmpeg-ebur128"
                self.assertFalse(
                    playback_core._track_has_loudness(
                        self._track(source, loudness_source=other, gain=-2.0)
                    )
                )
                peak_only = self._track(source, loudness_source=expected)
                peak_only.loudness_peak = 0.8
                self.assertFalse(playback_core._track_has_loudness(peak_only))

    def test_all_supported_sources_need_backend_loudness_without_cache(self) -> None:
        for source in (TrackSource.bilibili, TrackSource.netease, TrackSource.qq):
            with self.subTest(source=source):
                self.assertTrue(playback_core._track_needs_backend_loudness(self._track(source)))

    def test_qq_resolver_returns_separate_controlled_analysis_input(self) -> None:
        url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret"
        with patch(
            "app.routers.playback_audio.qq_music.resolve_audio_url",
            new=AsyncMock(return_value=url),
        ):
            result = asyncio.run(_resolve_qq_audio("003abc"))
        self.assertEqual(result.audio_url, url)
        self.assertEqual(result.analysis_audio_url, url)

    def test_upstream_bilibili_and_netease_loudness_is_ignored(self) -> None:
        class Response:
            def __init__(self, payload):
                self.payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self.payload

        class Client:
            def __init__(self, payloads, **_kwargs):
                self.payloads = iter(payloads)

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args):
                return False

            async def get(self, *_args, **_kwargs):
                return Response(next(self.payloads))

        bilibili_payloads = [
            {
                "code": 0,
                "data": {
                    "volume": {"measured_i": -20, "target_i": -14},
                    "durl": [{"url": "https://cdn.bilivideo.com/audio.mp4"}],
                },
            },
            {
                "code": 0,
                "data": {
                    "volume": {"measured_i": -20, "target_i": -14},
                    "dash": {
                        "audio": [
                            {
                                "baseUrl": "https://cdn.bilivideo.com/audio.m4s",
                                "codecs": "mp4a.40.2",
                                "bandwidth": 1,
                            }
                        ]
                    },
                },
            },
        ]
        with patch(
            "app.routers.playback_audio.httpx.AsyncClient",
            side_effect=lambda **kwargs: Client(bilibili_payloads, **kwargs),
        ):
            bilibili = asyncio.run(
                _resolve_bilibili_audio("BV1test", video_data={"cid": 123})
            )
        self.assertEqual(bilibili.audio_url, "https://cdn.bilivideo.com/audio.mp4")
        self.assertEqual(bilibili.analysis_audio_url, "https://cdn.bilivideo.com/audio.m4s")
        self.assertIsNone(bilibili.loudness_gain_db)
        self.assertIsNone(bilibili.loudness_source)

        netease_payloads = [
            {
                "data": [
                    {
                        "url": "https://m801.music.126.net/song.mp3",
                        "gain": 0.75,
                        "peak": 0.9,
                    }
                ]
            }
        ]
        with patch(
            "app.routers.playback_audio.httpx.AsyncClient",
            side_effect=lambda **kwargs: Client(netease_payloads, **kwargs),
        ):
            netease = asyncio.run(_resolve_netease_audio("123"))
        self.assertEqual(netease.audio_url, "https://m801.music.126.net/song.mp3")
        self.assertEqual(netease.analysis_audio_url, netease.audio_url)
        self.assertIsNone(netease.loudness_gain_db)
        self.assertIsNone(netease.loudness_source)

    def test_qq_analysis_allowlist_is_exact(self) -> None:
        accepted = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret"
        self.assertIsNone(audio_loudness.validate_trusted_audio_url(accepted, "qq"))
        for rejected in (
            "http://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret",
            "https://isure.stream.qqmusic.qq.com:80/M500003abc.mp3?vkey=secret",
            "https://evil.isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret",
            "https://isure.stream.qqmusic.qq.com.evil.example/M500003abc.mp3?vkey=secret",
            "https://user@isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret",
            "https://isure.stream.qqmusic.qq.com:444/M500003abc.mp3?vkey=secret",
        ):
            with self.subTest(url=rejected):
                self.assertIsNotNone(audio_loudness.validate_trusted_audio_url(rejected, "qq"))

    def test_qq_expired_purl_is_refreshed_before_prefetch_analysis(self) -> None:
        old_url = "https://isure.stream.qqmusic.qq.com/old.mp3?vkey=old"
        new_url = "https://isure.stream.qqmusic.qq.com/new.mp3?vkey=new"
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite:///{Path(directory) / 'expired.sqlite3'}",
                connect_args={"check_same_thread": False},
            )
            SQLModel.metadata.create_all(engine)
            with Session(engine) as db:
                track = self._track(TrackSource.qq)
                track.audio_url = old_url
                db.add(track)
                db.commit()
                db.refresh(track)
                track_id = track.id

                with (
                    patch.object(playback_core, "engine", engine),
                    patch(
                        "app.routers.playback_audio.qq_music.current_audio_url",
                        return_value=None,
                    ),
                    patch.object(
                        playback_core,
                        "_resolve_qq_audio",
                        new=AsyncMock(
                            return_value=AudioResolveResult(
                                audio_url=new_url,
                                analysis_audio_url=new_url,
                            )
                        ),
                    ) as resolver,
                    patch.object(playback_core, "_schedule_loudness_analysis") as schedule,
                ):
                    asyncio.run(playback_core._resolve_audio_url(db, track))

                resolver.assert_awaited_once_with(track.source_track_id, force=False)
                self.assertEqual(schedule.call_count, 1)
                self.assertEqual(schedule.call_args.args[2], new_url)
                db.expire_all()
                refreshed = db.get(Track, track_id)
                self.assertEqual(refreshed.audio_url, new_url)
                self.assertIsNone(refreshed.loudness_error)
            engine.dispose()

    def test_qq_failed_expired_purl_refresh_does_not_analyze_old_url(self) -> None:
        old_url = "https://isure.stream.qqmusic.qq.com/old.mp3?vkey=old"
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite:///{Path(directory) / 'expired-failure.sqlite3'}",
                connect_args={"check_same_thread": False},
            )
            SQLModel.metadata.create_all(engine)
            with Session(engine) as db:
                track = self._track(TrackSource.qq)
                track.audio_url = old_url
                db.add(track)
                db.commit()
                db.refresh(track)
                track_id = track.id

                with (
                    patch.object(playback_core, "engine", engine),
                    patch(
                        "app.routers.playback_audio.qq_music.current_audio_url",
                        return_value=None,
                    ),
                    patch.object(
                        playback_core,
                        "_resolve_qq_audio",
                        new=AsyncMock(return_value=AudioResolveResult()),
                    ),
                    patch.object(playback_core, "_schedule_loudness_analysis") as schedule,
                ):
                    asyncio.run(playback_core._resolve_audio_url(db, track))

                schedule.assert_not_called()
                db.expire_all()
                refreshed = db.get(Track, track_id)
                self.assertEqual(refreshed.audio_url, old_url)
                self.assertIsNone(refreshed.loudness_error)
            engine.dispose()

    def test_qq_analysis_dns_is_bounded_by_overall_timeout(self) -> None:
        secret_url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret"

        def slow_dns(_url: str) -> str | None:
            time.sleep(0.2)
            return None

        started = time.monotonic()
        with (
            patch.object(audio_loudness, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(audio_loudness, "_validate_public_dns", side_effect=slow_dns),
            patch.object(audio_loudness.settings.audio_loudness, "timeout_s", 0.03),
        ):
            result = asyncio.run(
                audio_loudness.analyze_remote_audio_loudness(
                    secret_url,
                    trusted_source="qq",
                    source="qq:ffmpeg-ebur128",
                )
            )
        self.assertLess(time.monotonic() - started, 0.15)
        self.assertIn("timeout", result.error or "")

    def test_analysis_fetch_error_does_not_expose_vkey_url(self) -> None:
        secret_url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=super-secret"

        class ExplodingClient:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                raise audio_loudness.httpx.RequestError("failed", request=None)

        with (
            patch.object(audio_loudness, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(audio_loudness, "_validate_public_dns", return_value=None),
            patch.object(audio_loudness.httpx, "Client", ExplodingClient),
        ):
            result = asyncio.run(
                audio_loudness.analyze_remote_audio_loudness(
                    secret_url,
                    trusted_source="qq",
                    source="qq:ffmpeg-ebur128",
                )
            )
        self.assertIsNotNone(result.error)
        self.assertNotIn("super-secret", result.error or "")
        self.assertNotIn("vkey", result.error or "")

    def test_qq_analysis_revalidates_redirect_target_without_leaking_vkey(self) -> None:
        secret_url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=super-secret"

        class Response:
            status_code = 302
            headers = {"location": "https://evil.example/audio.mp3?vkey=redirect-secret"}

            def raise_for_status(self):
                return None

        class Context:
            def __enter__(self):
                return Response()

            def __exit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return Context()

        with (
            patch.object(audio_loudness, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(audio_loudness, "_validate_public_dns", return_value=None),
            patch.object(audio_loudness.httpx, "Client", Client),
        ):
            result = asyncio.run(
                audio_loudness.analyze_remote_audio_loudness(
                    secret_url,
                    trusted_source="qq",
                    source="qq:ffmpeg-ebur128",
                )
            )
        self.assertEqual(result.error, "untrusted audio analysis url host")
        self.assertNotIn("secret", result.error or "")

    def test_qq_analysis_rejects_cross_port_redirect(self) -> None:
        secret_url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret"

        class Response:
            status_code = 302
            headers = {
                "location": "https://isure.stream.qqmusic.qq.com:80/next.mp3?vkey=redirect-secret"
            }

        class Context:
            def __enter__(self):
                return Response()

            def __exit__(self, *_args):
                return False

        class Client:
            def __init__(self, **_kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def stream(self, *_args, **_kwargs):
                return Context()

        with (
            patch.object(audio_loudness, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(audio_loudness, "_validate_public_dns", return_value=None),
            patch.object(audio_loudness.httpx, "Client", Client),
        ):
            result = asyncio.run(
                audio_loudness.analyze_remote_audio_loudness(
                    secret_url,
                    trusted_source="qq",
                    source="qq:ffmpeg-ebur128",
                )
            )
        self.assertEqual(result.error, "unsupported audio analysis url port")
        self.assertNotIn("secret", result.error or "")

    def test_qq_playback_url_refresh_preserves_matching_loudness_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(f"sqlite:///{Path(directory) / 'cache.sqlite3'}")
            SQLModel.metadata.create_all(engine)
            with Session(engine) as db:
                track = self._track(
                    TrackSource.qq,
                    loudness_source="qq:ffmpeg-ebur128",
                    gain=-3.0,
                )
                track.audio_url = "https://isure.stream.qqmusic.qq.com/old.mp3?vkey=old"
                db.add(track)
                db.commit()
                db.refresh(track)
                track_id = track.id

            with patch.object(playback_core, "engine", engine):
                playback_core._persist_audio_resolution_transaction(
                    track_id,
                    AudioResolveResult(
                        audio_url="https://isure.stream.qqmusic.qq.com/new.mp3?vkey=new"
                    ),
                    True,
                )

            with Session(engine) as db:
                refreshed = db.get(Track, track_id)
                self.assertIn("new.mp3", refreshed.audio_url or "")
                self.assertEqual(refreshed.loudness_source, "qq:ffmpeg-ebur128")
                self.assertEqual(refreshed.loudness_gain_db, -3.0)
                self.assertFalse(playback_core._track_needs_backend_loudness(refreshed))
            engine.dispose()

    def test_qq_audio_resolution_is_singleflight(self) -> None:
        async def scenario() -> None:
            service = QQMusicService()
            started = asyncio.Event()
            release = asyncio.Event()
            calls = 0
            url = "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret"

            async def authenticated(_operation):
                nonlocal calls
                calls += 1
                started.set()
                await release.wait()
                return url

            with patch.object(service, "_authenticated", side_effect=authenticated):
                first = asyncio.create_task(service.resolve_audio_url("003abc", force=True))
                await started.wait()
                second = asyncio.create_task(service.resolve_audio_url("003abc", force=True))
                await asyncio.sleep(0)
                self.assertEqual(calls, 1)
                release.set()
                self.assertEqual(await first, url)
                self.assertEqual(await second, url)
                await asyncio.sleep(0)
                self.assertNotIn("003abc", service._audio_url_tasks)

        asyncio.run(scenario())

    def test_loudness_analysis_scheduling_is_singleflight_and_skips_without_ffmpeg(self) -> None:
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()

            async def analyze(*_args, **_kwargs):
                started.set()
                await release.wait()
                return LoudnessAnalysisResult(error="mock analysis failed")

            with (
                patch.object(playback_core, "is_loudness_analysis_available", return_value=True),
                patch.object(playback_core, "analyze_remote_audio_loudness", side_effect=analyze) as mocked,
                patch.object(playback_core, "_persist_loudness_result_transaction"),
                patch.object(playback_core, "_handle_loudness_analysis_finished", new=AsyncMock()),
            ):
                playback_core._schedule_loudness_analysis(
                    1001,
                    TrackSource.qq,
                    "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret",
                )
                playback_core._schedule_loudness_analysis(
                    1001,
                    TrackSource.qq,
                    "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=other",
                )
                await started.wait()
                self.assertEqual(mocked.await_count, 1)
                release.set()
                await playback_core._LOUDNESS_ANALYSIS_TASKS[1001]
                await asyncio.sleep(0)
                self.assertNotIn(1001, playback_core._LOUDNESS_ANALYSIS_TASKS)

            with patch.object(playback_core, "is_loudness_analysis_available", return_value=False):
                playback_core._schedule_loudness_analysis(
                    1002,
                    TrackSource.qq,
                    "https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=secret",
                )
            self.assertNotIn(1002, playback_core._LOUDNESS_ANALYSIS_TASKS)

        asyncio.run(scenario())

    def test_all_sources_wait_only_when_ffmpeg_is_available(self) -> None:
        for source in (TrackSource.bilibili, TrackSource.netease, TrackSource.qq):
            for available in (True, False):
                with self.subTest(source=source, available=available):
                    self._assert_wait_contract(source, available)

    def _assert_wait_contract(self, source: TrackSource, available: bool) -> None:
        with tempfile.TemporaryDirectory() as directory:
            engine = create_engine(
                f"sqlite:///{Path(directory) / 'wait.sqlite3'}",
                connect_args={"check_same_thread": False},
            )
            SQLModel.metadata.create_all(engine)
            with Session(engine) as db:
                user = User(username=f"{source.value}-{available}", password_hash="unused")
                db.add(user)
                db.flush()
                room = Room(name=f"room-{source.value}-{available}", created_by_user_id=user.id)
                db.add(room)
                db.flush()
                track = self._track(source)
                db.add(track)
                db.flush()
                item = RoomQueueItem(
                    room_id=room.id,
                    track_id=track.id,
                    ordered_by_user_id=user.id,
                    status=QueueStatus.playing,
                )
                db.add(item)
                db.flush()
                state = RoomPlaybackState(
                    room_id=room.id,
                    mode=RoomMode.play_enabled,
                    current_queue_item_id=item.id,
                    is_playing=True,
                    position_ms=0,
                )
                db.add(state)
                db.commit()
                db.refresh(state)
                db.refresh(item)
                db.refresh(track)

                async def scenario() -> bool:
                    blocker = asyncio.create_task(asyncio.Event().wait())
                    if available:
                        playback_core._LOUDNESS_ANALYSIS_TASKS[track.id] = blocker
                    playback_core._ROOM_NORMALIZER_PREFS[(room.id, user.id)] = 1.0
                    try:
                        with (
                            patch.object(playback_core, "engine", engine),
                            patch.object(playback_core, "is_loudness_analysis_available", return_value=available),
                            patch.object(playback_core, "_ensure_playback_audio_url", new=AsyncMock()),
                            patch.object(playback_core.time, "monotonic", return_value=1.0),
                        ):
                            return await playback_core._prepare_current_track_for_playback(
                                db,
                                room.id,
                                state,
                                item,
                                track,
                            )
                    finally:
                        blocker.cancel()
                        playback_core._LOUDNESS_ANALYSIS_TASKS.pop(track.id, None)
                        playback_core._ROOM_NORMALIZER_PREFS.pop((room.id, user.id), None)
                        playback_core._ROOM_LOUDNESS_WAITING.pop(room.id, None)

                prepared = asyncio.run(scenario())
                db.expire_all()
                persisted = db.get(RoomPlaybackState, room.id)
                self.assertTrue(prepared)
                self.assertEqual(persisted.is_playing, not available)
                self.assertEqual(persisted.position_ms, 0)
            engine.dispose()


if __name__ == "__main__":
    unittest.main()
