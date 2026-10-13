from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from fastapi import HTTPException
from sqlmodel import SQLModel, Session, create_engine, select
from app import db as database, track_availability as availability
from app.models import Track, TrackSource, User, UserPlaylist, UserPlaylistItem, Room, RoomMember, RoomPlaybackState, RoomQueueItem, RoomMode
from app.routers import playback_audio, playback_core, queue_playback, track_maintenance, playlists_trending
from app.schemas import SearchTrackOut, SearchTrackPartOut


class AvailabilityTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.engine = create_engine(f"sqlite:///{self.temp.name}/test.sqlite3", connect_args={"check_same_thread": False})
        SQLModel.metadata.create_all(self.engine)
        self.patches = [patch.object(module, "engine", self.engine) for module in (availability, playback_core, queue_playback, track_maintenance, playlists_trending)]
        for item in self.patches:
            item.start()
        with Session(self.engine) as db:
            user = User(username="owner", password_hash="unused")
            other = User(username="other", password_hash="unused")
            db.add(user); db.add(other); db.flush()
            old = Track(source=TrackSource.netease, source_track_id="1", title="removed", availability="unavailable", unavailable_reason="下架")
            new = Track(source=TrackSource.netease, source_track_id="2", title="replacement", availability="available", availability_checked_at=datetime.utcnow(), audio_url="https://m801.music.126.net/new.mp3")
            db.add(old); db.add(new); db.flush()
            playlists = [UserPlaylist(user_id=user.id, name="one"), UserPlaylist(user_id=user.id, name="two"), UserPlaylist(user_id=other.id, name="other")]
            db.add_all(playlists); db.flush()
            when = datetime(2020, 1, 1)
            memberships = [UserPlaylistItem(playlist_id=p.id, track_id=old.id, created_at=when) for p in playlists]
            db.add_all(memberships)
            db.add(UserPlaylistItem(playlist_id=playlists[0].id, track_id=new.id, created_at=when + timedelta(days=1)))
            room = Room(name="test", created_by_user_id=user.id)
            db.add(room); db.flush()
            db.add(RoomMember(room_id=room.id, user_id=user.id))
            db.add(RoomPlaybackState(room_id=room.id, mode=RoomMode.order_only))
            db.commit()
            self.user_id, self.other_id = user.id, other.id
            self.old_id, self.new_id = old.id, new.id
            self.playlist_ids = [p.id for p in playlists]
            self.membership_ids = [item.id for item in memberships]
            self.room_id = room.id

    async def asyncTearDown(self):
        tasks = list(availability._PAUSES.values()) + list(availability._CHECKS.values())
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for item in self.patches:
            item.stop()
        self.engine.dispose()
        self.temp.cleanup()

    def test_resolution_clears_failed_url_and_never_guesses_from_network_error(self):
        track = Track(source=TrackSource.netease, source_track_id="9", title="test", audio_url="https://m801.music.126.net/a.mp3")
        availability.apply_resolution(track, playback_audio.AudioResolveResult(), force=True)
        self.assertEqual(track.availability, "unknown")
        availability.apply_resolution(track, playback_audio.AudioResolveResult(unavailable_reason="下架"))
        self.assertEqual(track.availability, "unavailable")
        self.assertIsNone(track.audio_url)
        self.assertIsNone(playback_audio._direct_playback_audio_url(track))
        availability.apply_resolution(track, playback_audio.AudioResolveResult(), force=True)
        self.assertEqual(track.availability, "unavailable")
        availability.apply_resolution(track, playback_audio.AudioResolveResult(audio_url="https://m801.music.126.net/b.mp3"))
        self.assertEqual(track.availability, "available")
        self.assertIsNone(track.unavailable_reason)

    def test_replacement_is_atomic_owner_scoped_and_retains_original_slots(self):
        self.assertEqual(track_maintenance._replace_all(self.user_id, self.old_id, self.new_id), 2)
        with Session(self.engine) as db:
            for item_id in self.membership_ids[:2]:
                item = db.get(UserPlaylistItem, item_id)
                self.assertEqual(item.track_id, self.new_id)
                self.assertEqual(item.created_at, datetime(2020, 1, 1))
            self.assertEqual(db.get(UserPlaylistItem, self.membership_ids[2]).track_id, self.old_id)
            self.assertEqual(len(db.exec(select(UserPlaylistItem)).all()), 3)
            self.assertEqual(db.get(Track, self.old_id).title, "removed")

    def test_remove_all_is_owner_scoped_and_does_not_delete_global_track(self):
        self.assertEqual(track_maintenance._remove_all(self.user_id, self.old_id), 2)
        with Session(self.engine) as db:
            self.assertIsNotNone(db.get(Track, self.old_id))
            self.assertEqual(db.get(UserPlaylistItem, self.membership_ids[2]).track_id, self.old_id)
        with self.assertRaises(HTTPException) as error:
            track_maintenance._remove_all(9999, self.old_id)
        self.assertEqual(error.exception.status_code, 404)

    def test_unconfirmed_replacement_rolls_back_and_regular_membership_edits_are_blocked(self):
        with Session(self.engine) as db:
            replacement = db.get(Track, self.new_id)
            replacement.availability = "unknown"
            db.add(replacement); db.commit()
        with self.assertRaises(HTTPException) as error:
            track_maintenance._replace_all(self.user_id, self.old_id, self.new_id)
        self.assertEqual(error.exception.status_code, 503)
        with Session(self.engine) as db:
            self.assertEqual(db.get(UserPlaylistItem, self.membership_ids[0]).track_id, self.old_id)
            owner = db.get(User, self.user_id)
            with self.assertRaises(HTTPException):
                playlists_trending.delete_playlist_item(self.playlist_ids[0], self.membership_ids[0], db, owner)
            with self.assertRaises(HTTPException):
                playlists_trending._add_track_payload_to_playlist(db, self.playlist_ids[1], {"source": "netease", "source_track_id": "1", "title": "forged"})

    async def test_batch_selected_invalid_track_is_reported_without_backfill(self):
        with Session(self.engine) as db:
            payload = {"items": [{"source": "netease", "source_track_id": "1", "title": "removed"}, {"source": "netease", "source_track_id": "2", "title": "valid"}], "count": 1, "random": True}
            with patch.object(queue_playback.random, "shuffle", lambda rows: None):
                result = await queue_playback.add_to_queue_batch(self.room_id, payload, db, db.get(User, self.user_id))
            self.assertEqual(result["added"], 0)
            self.assertEqual(result["skipped_unavailable"][0]["id"], self.old_id)
            self.assertEqual(db.exec(select(RoomQueueItem)).all(), [])

    async def test_single_invalid_queue_is_rejected(self):
        with Session(self.engine) as db:
            with self.assertRaises(HTTPException) as error:
                await queue_playback.add_to_queue(self.room_id, {"source": "netease", "source_track_id": "1", "title": "forged"}, db, db.get(User, self.user_id))
            self.assertEqual(error.exception.status_code, 409)
            self.assertEqual(error.exception.detail["track"]["title"], "removed")

    async def test_checks_share_one_upstream_request_and_temporary_failure_remains_unknown(self):
        with Session(self.engine) as db:
            track = db.get(Track, self.new_id)
            db.expunge(track)
        async def slow(*args):
            await asyncio.sleep(0.01)
            return playback_audio.AudioResolveResult()
        with patch.object(availability, "_resolve_netease_audio", AsyncMock(side_effect=slow)) as resolver:
            results = await asyncio.gather(availability.check_track(track, force=True), availability.check_track(track, force=True))
        self.assertEqual(resolver.await_count, 1)
        self.assertTrue(all(item.availability == "unknown" for item in results))

    async def test_unavailable_current_song_is_paused_at_zero(self):
        with Session(self.engine) as db:
            item = RoomQueueItem(room_id=self.room_id, track_id=self.old_id, ordered_by_user_id=self.user_id)
            db.add(item); db.flush()
            pb = db.get(RoomPlaybackState, self.room_id)
            pb.current_queue_item_id = item.id; pb.is_playing = True
            db.add(pb); db.commit()
            old = db.get(Track, self.old_id); db.expunge(old)
        availability.schedule_unavailable_pause(old)
        await asyncio.gather(*availability._PAUSES.values())
        with Session(self.engine) as db:
            pb = db.get(RoomPlaybackState, self.room_id)
            self.assertFalse(pb.is_playing)
            self.assertEqual(pb.position_ms, 0)

    async def test_real_routes_replace_then_remove_only_owned_memberships(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        from app.deps import get_current_user, get_db
        app = FastAPI()
        # Keep production router order so dynamic playlist routes cannot shadow.
        app.include_router(playlists_trending.router)
        app.include_router(track_maintenance.router)
        def session_dependency():
            with Session(self.engine) as db:
                yield db
        def user_dependency():
            with Session(self.engine) as db:
                return db.get(User, self.user_id)
        app.dependency_overrides[get_db] = session_dependency
        app.dependency_overrides[get_current_user] = user_dependency
        with patch.object(availability, "_resolve_netease_audio", AsyncMock(return_value=playback_audio.AudioResolveResult(audio_url="https://m801.music.126.net/new.mp3"))):
            with TestClient(app) as client:
                result = client.post(f"/api/playlists/tracks/{self.old_id}/replace", json={"source": "netease", "source_track_id": "2", "title": "forged title"})
                self.assertEqual(result.status_code, 200, result.text)
                self.assertEqual(result.json()["replaced"], 2)
                self.assertEqual(result.json()["track"]["title"], "replacement")
                self.assertEqual(client.delete(f"/api/playlists/tracks/{self.old_id}").status_code, 404)
                app.dependency_overrides[get_current_user] = lambda: User(id=self.other_id, username="other", password_hash="unused")
                removed = client.delete(f"/api/playlists/tracks/{self.old_id}")
                self.assertEqual(removed.status_code, 200, removed.text)
                self.assertEqual(removed.json()["removed"], 1)

    async def test_late_playback_url_does_not_overwrite_newer_unavailable_check(self):
        started = asyncio.Event()
        release = asyncio.Event()
        async def old_resolver(*args):
            started.set()
            await release.wait()
            return playback_audio.AudioResolveResult(audio_url="https://m801.music.126.net/old.mp3")
        with patch.object(playback_core, "_resolve_netease_audio", side_effect=old_resolver), patch.object(availability, "_resolve_netease_audio", AsyncMock(return_value=playback_audio.AudioResolveResult(unavailable_reason="下架"))):
            with Session(self.engine) as db:
                track = db.get(Track, self.new_id)
                old_request = asyncio.create_task(playback_core._resolve_audio_url(db, track, force=True))
                await started.wait()
                current = await asyncio.to_thread(availability._read_track, self.new_id)
                newer = await availability.check_track(current, force=True)
                self.assertEqual(newer.availability, "unavailable")
                release.set()
                await old_request
        with Session(self.engine) as db:
            track = db.get(Track, self.new_id)
            self.assertEqual(track.availability, "unavailable")
            self.assertIsNone(track.audio_url)

    def test_search_annotation_includes_collection_parts(self):
        part = SearchTrackPartOut(source=TrackSource.netease, source_track_id="1", title="removed")
        result = SearchTrackOut(source=TrackSource.bilibili, source_track_id="collection", title="collection", parts=[part])
        availability.annotate_search_results([result])
        self.assertEqual(result.parts[0].id, self.old_id)
        self.assertEqual(result.parts[0].availability, "unavailable")

    def test_lightweight_migration_is_idempotent_and_defaults_to_unknown(self):
        filename = f"{self.temp.name}/legacy.sqlite3"
        with sqlite3.connect(filename) as conn:
            conn.executescript("CREATE TABLE users (id INTEGER PRIMARY KEY); CREATE TABLE tracks (id INTEGER PRIMARY KEY); INSERT INTO tracks VALUES (1);")
        from types import SimpleNamespace
        with patch.object(database, "settings", SimpleNamespace(sqlite_path=filename)):
            database._migrate_columns(); database._migrate_columns()
        with sqlite3.connect(filename) as conn:
            self.assertEqual(conn.execute("SELECT availability, unavailable_reason, availability_checked_at FROM tracks").fetchone(), ("unknown", None, None))


class ResolverClassificationTests(unittest.IsolatedAsyncioTestCase):
    async def resolve(self, resolver, payload):
        import httpx
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
        original = httpx.AsyncClient
        with patch.object(playback_audio.httpx, "AsyncClient", side_effect=lambda **kwargs: original(transport=transport, **kwargs)):
            return await resolver("123")

    async def test_explicit_bilibili_deletion_vs_rate_limit(self):
        gone = await self.resolve(playback_audio._resolve_bilibili_audio, {"code": -404})
        limited = await self.resolve(playback_audio._resolve_bilibili_audio, {"code": -412})
        self.assertIsNotNone(gone.unavailable_reason)
        self.assertIsNone(limited.unavailable_reason)

    async def test_netease_trial_and_missing_url_vs_service_failure(self):
        for item in ({"code": 404, "url": None}, {"url": "https://m801.music.126.net/trial.mp3", "freeTrialInfo": {"end": 30}}):
            result = await self.resolve(playback_audio._resolve_netease_audio, {"code": 200, "data": [item]})
            self.assertIsNotNone(result.unavailable_reason)
            self.assertIsNone(result.audio_url)
        failed = await self.resolve(playback_audio._resolve_netease_audio, {"code": 503})
        self.assertIsNone(failed.unavailable_reason)

    async def test_qq_login_failure_is_not_a_removed_song(self):
        from app.qq_music import QQMusicUnavailable, QQTrackUnavailable
        with patch.object(playback_audio.qq_music, "resolve_audio_url", AsyncMock(side_effect=QQMusicUnavailable("not logged in"))):
            self.assertIsNone((await playback_audio._resolve_qq_audio("mid")).unavailable_reason)
        with patch.object(playback_audio.qq_music, "resolve_audio_url", AsyncMock(side_effect=QQTrackUnavailable("removed"))):
            self.assertIsNotNone((await playback_audio._resolve_qq_audio("mid")).unavailable_reason)


if __name__ == "__main__":
    unittest.main()
