from __future__ import annotations

import asyncio
import io
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlmodel import Session, SQLModel, create_engine, select


BACKEND_ROOT = Path(__file__).resolve().parents[1]
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from app import audio_loudness  # noqa: E402
from app.models import (  # noqa: E402
    QueueStatus,
    Room,
    RoomMode,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackOrderStats,
    TrackSource,
    User,
    UserPlaylist,
)
from app.routers import media_cover, playback_core, playlists_trending, queue_playback  # noqa: E402
from app.routers.playback_audio import _direct_playback_audio_url  # noqa: E402


class _Response:
    def __init__(self, *, status_code: int = 200, headers: dict[str, str] | None = None, chunks=None):
        self.status_code = status_code
        self.headers = headers or {}
        self._chunks = chunks or []

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def iter_bytes(self):
        yield from self._chunks


class _ResponseContext:
    def __init__(self, response: _Response):
        self.response = response

    def __enter__(self):
        return self.response

    def __exit__(self, *_args):
        return False


class _Client:
    def __init__(self, response: _Response, **_kwargs):
        self.response = response

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return _ResponseContext(self.response)


class _Process:
    instances: list["_Process"] = []

    def __init__(self, *_args, **_kwargs):
        self.stdin = io.BytesIO()
        self.killed = False
        self.__class__.instances.append(self)

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        return -9 if self.killed else 0


class _AsyncResponse(_Response):
    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _AsyncResponseContext:
    def __init__(self, response: _AsyncResponse):
        self.response = response

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, *_args):
        return False


class _AsyncClient:
    def __init__(self, responses: list[_AsyncResponse], **_kwargs):
        self.responses = iter(responses)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    def stream(self, *_args, **_kwargs):
        return _AsyncResponseContext(next(self.responses))


class PlaybackSafetyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.TemporaryDirectory()
        self.engine = create_engine(
            f"sqlite:///{Path(self.tempdir.name) / 'test.sqlite3'}",
            connect_args={"check_same_thread": False},
        )
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.tempdir.cleanup()

    def _seed_room(self, session: Session, name: str = "room") -> tuple[User, Room]:
        user = User(username=f"{name}-user", password_hash="unused")
        session.add(user)
        session.flush()
        room = Room(name=name, created_by_user_id=user.id)
        session.add(room)
        session.commit()
        session.refresh(user)
        session.refresh(room)
        return user, room

    def test_client_audio_url_is_not_part_of_validated_queue_input(self) -> None:
        parsed = queue_playback._validate_queue_track_payload(
            {
                "source": "netease",
                "source_track_id": "123",
                "title": "safe title",
                "audio_url": "http://127.0.0.1/private",
            }
        )
        self.assertNotIn("audio_url", parsed.model_dump())

    def test_existing_global_track_metadata_and_trusted_audio_are_not_overwritten(self) -> None:
        with Session(self.engine) as session:
            original = Track(
                source=TrackSource.netease,
                source_track_id="123",
                title="trusted title",
                artist="trusted artist",
                audio_url="https://m801.music.126.net/song.mp3",
            )
            session.add(original)
            session.commit()
            track = queue_playback._get_or_create_track(
                session,
                source=TrackSource.netease,
                source_track_id="123",
                title="attacker title",
                artist="attacker artist",
                cover_url="https://attacker.invalid/cover.jpg",
            )
            session.commit()
            session.refresh(track)
            self.assertEqual(track.title, "trusted title")
            self.assertEqual(track.artist, "trusted artist")
            self.assertEqual(track.audio_url, "https://m801.music.126.net/song.mp3")

    def test_cross_room_track_and_stats_upserts_are_consistent(self) -> None:
        with Session(self.engine) as session:
            user1, room1 = self._seed_room(session, "one")
            user2, room2 = self._seed_room(session, "two")
            user1_id, room1_id = user1.id, room1.id
            user2_id, room2_id = user2.id, room2.id
        track_input = queue_playback._validate_queue_track_payload(
            {"source": "netease", "source_track_id": "456", "title": "song"}
        )
        with patch.object(queue_playback, "engine", self.engine):
            queue_playback._enqueue_track_inputs_transaction(room1_id, [track_input], user1_id)
            queue_playback._enqueue_track_inputs_transaction(room2_id, [track_input], user2_id)
        with Session(self.engine) as session:
            tracks = session.exec(select(Track).where(Track.source_track_id == "456")).all()
            self.assertEqual(len(tracks), 1)
            stats = session.get(TrackOrderStats, tracks[0].id)
            self.assertIsNotNone(stats)
            self.assertEqual(stats.order_count, 2)

    def test_bilibili_playlist_import_rolls_back_as_one_worker_transaction(self) -> None:
        with Session(self.engine) as session:
            user, _ = self._seed_room(session, "playlist-import")
            user_id = user.id
        tracks = [
            {
                "source": "bilibili",
                "source_track_id": "BV1test#p=1&cid=1",
                "title": "part one",
            },
            {
                "source": "bilibili",
                "source_track_id": "BV1test#p=2&cid=2",
                "title": "part two",
            },
        ]
        original = playlists_trending._add_track_payload_to_playlist
        calls = 0

        def fail_second(write_db, playlist_id, payload):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError("injected playlist failure")
            return original(write_db, playlist_id, payload)

        with (
            patch.object(playlists_trending, "engine", self.engine),
            patch.object(playlists_trending, "_add_track_payload_to_playlist", side_effect=fail_second),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected playlist failure"):
                playlists_trending._import_bilibili_collection_transaction(
                    mode="create",
                    name="collection",
                    playlist_ids=[],
                    tracks=tracks,
                    user_id=user_id,
                )

        with Session(self.engine) as session:
            self.assertEqual(session.exec(select(UserPlaylist)).all(), [])
            self.assertEqual(session.exec(select(Track)).all(), [])

    def test_playback_pointer_and_queue_status_change_together(self) -> None:
        with Session(self.engine) as session:
            user, room = self._seed_room(session)
            track1 = Track(source=TrackSource.netease, source_track_id="1", title="one")
            track2 = Track(source=TrackSource.netease, source_track_id="2", title="two")
            session.add(track1)
            session.add(track2)
            session.flush()
            item1 = RoomQueueItem(
                room_id=room.id,
                track_id=track1.id,
                ordered_by_user_id=user.id,
                status=QueueStatus.playing,
            )
            item2 = RoomQueueItem(
                room_id=room.id,
                track_id=track2.id,
                ordered_by_user_id=user.id,
                status=QueueStatus.queued,
            )
            session.add(item1)
            session.add(item2)
            session.flush()
            session.add(
                RoomPlaybackState(
                    room_id=room.id,
                    current_queue_item_id=item1.id,
                    is_playing=True,
                )
            )
            session.commit()
            room_id, item1_id, item2_id = room.id, item1.id, item2.id
        with patch.object(playback_core, "engine", self.engine):
            playback_core._set_playback_transaction(
                room_id,
                is_playing=True,
                position_ms=0,
                volume=None,
                update_current_queue_item=True,
                current_queue_item_id=item2_id,
            )
        with Session(self.engine) as session:
            state = session.get(RoomPlaybackState, room_id)
            self.assertEqual(state.current_queue_item_id, item2_id)
            self.assertEqual(session.get(RoomQueueItem, item1_id).status, QueueStatus.played)
            self.assertEqual(session.get(RoomQueueItem, item2_id).status, QueueStatus.playing)

    def test_removing_current_item_updates_pointer_and_statuses_atomically(self) -> None:
        with Session(self.engine) as session:
            user, room = self._seed_room(session, "remove-current")
            track1 = Track(source=TrackSource.netease, source_track_id="31", title="one")
            track2 = Track(source=TrackSource.netease, source_track_id="32", title="two")
            session.add(track1)
            session.add(track2)
            session.flush()
            item1 = RoomQueueItem(
                room_id=room.id,
                track_id=track1.id,
                ordered_by_user_id=user.id,
                status=QueueStatus.playing,
            )
            item2 = RoomQueueItem(
                room_id=room.id,
                track_id=track2.id,
                ordered_by_user_id=user.id,
                status=QueueStatus.queued,
            )
            session.add(item1)
            session.add(item2)
            session.flush()
            session.add(RoomPlaybackState(
                room_id=room.id,
                current_queue_item_id=item1.id,
                is_playing=True,
                position_ms=1234,
            ))
            session.commit()
            room_id, item1_id, item2_id = room.id, item1.id, item2.id

        with patch.object(queue_playback, "engine", self.engine):
            outcome, next_id = queue_playback._remove_queue_item_transaction(room_id, item1_id)

        self.assertEqual((outcome, next_id), ("current", item2_id))
        with Session(self.engine) as session:
            state = session.get(RoomPlaybackState, room_id)
            self.assertEqual(state.current_queue_item_id, item2_id)
            self.assertTrue(state.is_playing)
            self.assertEqual(state.position_ms, 0)
            self.assertEqual(session.get(RoomQueueItem, item1_id).status, QueueStatus.removed)
            self.assertEqual(session.get(RoomQueueItem, item2_id).status, QueueStatus.playing)

    def test_removing_current_rolls_back_if_next_selection_fails(self) -> None:
        with Session(self.engine) as session:
            user, room = self._seed_room(session, "remove-failure")
            track = Track(source=TrackSource.netease, source_track_id="41", title="one")
            session.add(track)
            session.flush()
            item = RoomQueueItem(
                room_id=room.id,
                track_id=track.id,
                ordered_by_user_id=user.id,
                status=QueueStatus.playing,
            )
            session.add(item)
            session.flush()
            session.add(RoomPlaybackState(
                room_id=room.id,
                current_queue_item_id=item.id,
                is_playing=True,
            ))
            session.commit()
            room_id, item_id = room.id, item.id

        with (
            patch.object(queue_playback, "engine", self.engine),
            patch.object(queue_playback, "_pick_next_queue_item_id", side_effect=RuntimeError("fault")),
        ):
            with self.assertRaisesRegex(RuntimeError, "fault"):
                queue_playback._remove_queue_item_transaction(room_id, item_id)

        with Session(self.engine) as session:
            state = session.get(RoomPlaybackState, room_id)
            self.assertEqual(state.current_queue_item_id, item_id)
            self.assertTrue(state.is_playing)
            self.assertEqual(session.get(RoomQueueItem, item_id).status, QueueStatus.playing)

    def test_prepare_does_not_pause_track_that_changed_during_resolver_await(self) -> None:
        with Session(self.engine) as session:
            user, room = self._seed_room(session, "race")
            track1 = Track(source=TrackSource.netease, source_track_id="11", title="one")
            track2 = Track(source=TrackSource.netease, source_track_id="22", title="two")
            session.add(track1)
            session.add(track2)
            session.flush()
            item1 = RoomQueueItem(room_id=room.id, track_id=track1.id, ordered_by_user_id=user.id)
            item2 = RoomQueueItem(room_id=room.id, track_id=track2.id, ordered_by_user_id=user.id)
            session.add(item1)
            session.add(item2)
            session.flush()
            state = RoomPlaybackState(
                room_id=room.id,
                mode=RoomMode.play_enabled,
                current_queue_item_id=item1.id,
                is_playing=True,
            )
            session.add(state)
            session.commit()
            room_id, item2_id = room.id, item2.id
            session.refresh(state)
            session.refresh(item1)
            session.refresh(track1)

            async def switch_current_track(*_args, **_kwargs):
                with Session(self.engine) as other_session:
                    fresh = other_session.get(RoomPlaybackState, room_id)
                    fresh.current_queue_item_id = item2_id
                    other_session.add(fresh)
                    other_session.commit()

            with patch.object(playback_core, "_ensure_playback_audio_url", side_effect=switch_current_track):
                prepared = asyncio.run(
                    playback_core._prepare_current_track_for_playback(
                        session,
                        room_id,
                        state,
                        item1,
                        track1,
                    )
                )
            self.assertFalse(prepared)
        with Session(self.engine) as session:
            fresh = session.get(RoomPlaybackState, room_id)
            self.assertEqual(fresh.current_queue_item_id, item2_id)
            self.assertTrue(fresh.is_playing)

    def test_loudness_resume_retries_transient_failures_and_clears_wait(self) -> None:
        room_id = 901
        queue_item_id = 902
        playback_core._ROOM_LOUDNESS_WAITING[room_id] = queue_item_id
        transaction = unittest.mock.Mock(
            side_effect=[RuntimeError("busy"), RuntimeError("busy"), "resumed"]
        )
        try:
            with (
                patch.object(playback_core, "_resume_loudness_wait_transaction", transaction),
                patch.object(playback_core, "_broadcast_playback_snapshot", new=AsyncMock()),
                patch.object(playback_core.asyncio, "sleep", new=AsyncMock()),
                patch.object(playback_core.logger, "exception"),
            ):
                resumed = asyncio.run(
                    playback_core._resume_loudness_wait_with_retry(
                        room_id,
                        queue_item_id,
                        attempts=4,
                    )
                )
            self.assertTrue(resumed)
            self.assertEqual(transaction.call_count, 3)
            self.assertNotIn(room_id, playback_core._ROOM_LOUDNESS_WAITING)
        finally:
            playback_core._ROOM_LOUDNESS_WAITING.pop(room_id, None)

    def test_loudness_recovery_failure_remains_explicitly_retryable(self) -> None:
        room_id = 903
        queue_item_id = 904
        playback_core._ROOM_LOUDNESS_WAITING[room_id] = queue_item_id
        calls = 0

        def fail_then_resume(*_args):
            nonlocal calls
            calls += 1
            self.assertTrue(playback_core._playback_lock(room_id).locked())
            if calls <= 2:
                raise RuntimeError("database is busy")
            return "resumed"

        async def scenario() -> None:
            first = playback_core._schedule_loudness_wait_recovery(room_id, queue_item_id)
            self.assertFalse(await first)
            self.assertEqual(
                playback_core._ROOM_LOUDNESS_WAITING.get(room_id),
                queue_item_id,
            )
            self.assertNotIn((room_id, queue_item_id), playback_core._LOUDNESS_RECOVERY_TASKS)

            second = playback_core._schedule_loudness_wait_recovery(room_id, queue_item_id)
            self.assertTrue(await second)

        try:
            with (
                patch.object(playback_core, "_resume_loudness_wait_transaction", side_effect=fail_then_resume),
                patch.object(playback_core, "_broadcast_playback_snapshot", new=AsyncMock()),
                patch.object(playback_core.asyncio, "sleep", new=AsyncMock()),
                patch.object(playback_core.logger, "exception"),
            ):
                original = playback_core._resume_loudness_wait_with_retry

                async def two_attempts(*args, **kwargs):
                    return await original(*args, **kwargs, attempts=2)

                with patch.object(playback_core, "_resume_loudness_wait_with_retry", side_effect=two_attempts):
                    asyncio.run(scenario())
            self.assertEqual(calls, 3)
            self.assertNotIn(room_id, playback_core._ROOM_LOUDNESS_WAITING)
        finally:
            playback_core._ROOM_LOUDNESS_WAITING.pop(room_id, None)
            playback_core._LOUDNESS_RECOVERY_TASKS.pop((room_id, queue_item_id), None)

    def test_browser_playback_never_uses_bilibili_dash_audio(self) -> None:
        dash = Track(
            source=TrackSource.bilibili,
            source_track_id="BV1test",
            title="dash",
            audio_url="https://upos-sz-mirrorcos.bilivideo.com/audio.m4s",
        )
        mp4 = Track(
            source=TrackSource.bilibili,
            source_track_id="BV1test2",
            title="mp4",
            audio_url="https://upos-sz-mirrorcos.bilivideo.com/audio.mp4",
        )
        self.assertIsNone(_direct_playback_audio_url(dash))
        self.assertEqual(_direct_playback_audio_url(mp4), mp4.audio_url)

    def test_qq_cached_url_is_only_exposed_while_service_marks_it_current(self) -> None:
        qq = Track(
            source=TrackSource.qq,
            source_track_id="003abc",
            title="qq",
            audio_url="https://isure.stream.qqmusic.qq.com/M500003abc.mp3?vkey=key",
        )
        with patch("app.routers.playback_audio.qq_music.current_audio_url", return_value=None):
            self.assertIsNone(_direct_playback_audio_url(qq))
        with patch(
            "app.routers.playback_audio.qq_music.current_audio_url",
            return_value=qq.audio_url,
        ):
            self.assertEqual(_direct_playback_audio_url(qq), qq.audio_url)

    def test_analysis_url_allowlist_rejects_credentials_and_broad_suffixes(self) -> None:
        self.assertIsNone(
            audio_loudness.validate_trusted_audio_url(
                "https://m801.music.126.net/song.mp3", "netease"
            )
        )
        self.assertIsNotNone(
            audio_loudness.validate_trusted_audio_url(
                "https://example.163.com/song.mp3", "netease"
            )
        )
        self.assertIsNotNone(
            audio_loudness.validate_trusted_audio_url(
                "https://user:pass@m801.music.126.net/song.mp3", "netease"
            )
        )

    def test_analysis_rejects_private_dns_resolution(self) -> None:
        resolved = [(None, None, None, None, ("127.0.0.1", 0))]
        with patch.object(audio_loudness.socket, "getaddrinfo", return_value=resolved):
            self.assertEqual(
                audio_loudness._validate_public_dns("https://m801.music.126.net/song.mp3"),
                "audio analysis host resolved to non-public address",
            )

    def test_analysis_download_limit_kills_ffmpeg(self) -> None:
        response = _Response(chunks=[b"x" * 5])
        _Process.instances.clear()
        with (
            patch.object(audio_loudness, "_resolve_ffmpeg", return_value="ffmpeg"),
            patch.object(audio_loudness, "_validate_public_dns", return_value=None),
            patch.object(audio_loudness, "_MAX_ANALYSIS_DOWNLOAD_BYTES", 4),
            patch.object(audio_loudness.httpx, "Client", side_effect=lambda **kwargs: _Client(response, **kwargs)),
            patch.object(audio_loudness.subprocess, "Popen", _Process),
        ):
            result = asyncio.run(
                audio_loudness.analyze_remote_audio_loudness(
                    "https://m801.music.126.net/song.mp3",
                    trusted_source="netease",
                )
            )
        self.assertIn("exceeded byte limit", result.error or "")
        self.assertTrue(_Process.instances[0].killed)

    def test_cover_proxy_revalidates_dns_after_each_redirect(self) -> None:
        responses = [
            _AsyncResponse(
                status_code=302,
                headers={"location": "https://i1.hdslb.com/redirected.jpg"},
            )
        ]
        with (
            patch.object(
                media_cover.httpx,
                "AsyncClient",
                side_effect=lambda **kwargs: _AsyncClient(responses, **kwargs),
            ),
            patch.object(media_cover, "_cover_host_is_public", side_effect=[True, False]) as dns_check,
        ):
            with self.assertRaises(HTTPException) as raised:
                asyncio.run(media_cover.fetch_cover_response("https://i0.hdslb.com/cover.jpg"))
        self.assertEqual(raised.exception.status_code, 400)
        self.assertEqual(dns_check.call_count, 2)

    def test_cover_proxy_rejects_non_images_and_streams_over_limit(self) -> None:
        async def fetch(response: _AsyncResponse):
            with (
                patch.object(
                    media_cover.httpx,
                    "AsyncClient",
                    side_effect=lambda **kwargs: _AsyncClient([response], **kwargs),
                ),
                patch.object(media_cover, "_cover_host_is_public", return_value=True),
            ):
                return await media_cover.fetch_cover_response("https://i0.hdslb.com/cover.jpg")

        with self.assertRaises(HTTPException) as wrong_type:
            asyncio.run(fetch(_AsyncResponse(headers={"content-type": "text/html"})))
        self.assertEqual(wrong_type.exception.status_code, 502)

        with patch.object(media_cover, "_MAX_COVER_BYTES", 4):
            with self.assertRaises(HTTPException) as too_large:
                asyncio.run(
                    fetch(
                        _AsyncResponse(
                            headers={"content-type": "image/jpeg"},
                            chunks=[b"123", b"45"],
                        )
                    )
                )
        self.assertEqual(too_large.exception.status_code, 413)


if __name__ == "__main__":
    unittest.main()
