from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine
from app.db import _set_sqlite_pragmas
from app.models import QueueStatus, Room, RoomMember, RoomMode, RoomPlaybackState, RoomQueueItem, Track, TrackSource, User
from app.routers.playback_core import _playback_lock
from app.routers.rooms import room_state, set_room_mode
from app.schemas import SetRoomModeIn


class RoomModeEnvelopeTests(unittest.IsolatedAsyncioTestCase):
    async def test_mode_change_delegates_to_complete_playback_snapshot(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'mode.sqlite3'}",
                connect_args={"check_same_thread": False},
                poolclass=NullPool,
            )
            event.listen(engine, "connect", _set_sqlite_pragmas)
            SQLModel.metadata.create_all(engine)
            try:
                with Session(engine) as db:
                    user = User(username="mode-user", password_hash="x")
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    room = Room(name="mode", created_by_user_id=user.id)
                    db.add(room)
                    db.commit()
                    db.refresh(room)
                    db.add(RoomMember(room_id=room.id, user_id=user.id))
                    db.add(RoomPlaybackState(room_id=room.id, mode=RoomMode.play_enabled))
                    db.commit()

                    with (
                        patch("app.routers.rooms._require_active_room_member", new=AsyncMock()),
                        patch("app.routers.rooms.engine", engine),
                        patch(
                            "app.routers.playback_core.hub.broadcast",
                            new=AsyncMock(),
                        ) as broadcast,
                    ):
                        result = await set_room_mode(
                            room.id,
                            SetRoomModeIn(mode=RoomMode.order_only),
                            db,
                            user,
                        )

                    self.assertEqual(result, {"ok": True})
                    broadcast.assert_awaited_once()
                    broadcast_room_id, playback_event = broadcast.await_args.args
                    self.assertEqual(broadcast_room_id, room.id)
                    self.assertEqual(playback_event["type"], "playback_updated")
                    self.assertEqual(playback_event["room_id"], room.id)
                    self.assertEqual(playback_event["playback_state"]["mode"], RoomMode.order_only.value)
                    self.assertIsNone(playback_event["current_track"])
                    self.assertIsNone(playback_event["ordered_by"])
                    self.assertFalse(playback_event["loudness_waiting"])
                    self.assertIn("server_time", playback_event)
                    self.assertIn("server_ts_ms", playback_event)
                    self.assertIn("effective_position_ms", playback_event)
            finally:
                engine.dispose()

    async def test_enabling_playback_commits_mode_pointer_and_queue_status_together(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'mode-start.sqlite3'}",
                connect_args={"check_same_thread": False},
                poolclass=NullPool,
            )
            event.listen(engine, "connect", _set_sqlite_pragmas)
            SQLModel.metadata.create_all(engine)
            try:
                with Session(engine) as db:
                    user = User(username="starter", password_hash="x")
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    room = Room(name="start", created_by_user_id=user.id)
                    track = Track(
                        source=TrackSource.netease,
                        source_track_id="start-track",
                        title="Start",
                    )
                    db.add(room)
                    db.add(track)
                    db.commit()
                    db.refresh(room)
                    db.refresh(track)
                    db.add(RoomMember(room_id=room.id, user_id=user.id))
                    db.add(RoomPlaybackState(room_id=room.id, mode=RoomMode.order_only))
                    queue_item = RoomQueueItem(
                        room_id=room.id,
                        track_id=track.id,
                        ordered_by_user_id=user.id,
                        status=QueueStatus.queued,
                    )
                    db.add(queue_item)
                    db.commit()
                    db.refresh(queue_item)

                    with (
                        patch("app.routers.rooms.engine", engine),
                        patch("app.routers.playback_core.engine", engine),
                        patch("app.routers.playback_core._ensure_playback_audio_url", new=AsyncMock()),
                        patch(
                            "app.routers.playback_core.hub.broadcast",
                            new=AsyncMock(),
                        ) as broadcast,
                    ):
                        result = await set_room_mode(
                            room.id,
                            SetRoomModeIn(mode=RoomMode.play_enabled),
                            db,
                            user,
                        )

                    self.assertEqual(result, {"ok": True})
                    db.rollback()
                    db.expire_all()
                    playback = db.get(RoomPlaybackState, room.id)
                    persisted_item = db.get(RoomQueueItem, queue_item.id)
                    self.assertEqual(playback.mode, RoomMode.play_enabled)
                    self.assertEqual(playback.current_queue_item_id, queue_item.id)
                    self.assertTrue(playback.is_playing)
                    self.assertEqual(playback.position_ms, 0)
                    self.assertEqual(persisted_item.status, QueueStatus.playing)
                    self.assertEqual(
                        [call.args[1]["type"] for call in broadcast.await_args_list],
                        ["playback_updated", "queue_updated"],
                    )
            finally:
                engine.dispose()

    async def test_room_state_resolves_outside_lock_and_rebuilds_after_track_change(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            engine = create_engine(
                f"sqlite:///{Path(temp_dir) / 'state.sqlite3'}",
                connect_args={"check_same_thread": False},
                poolclass=NullPool,
            )
            event.listen(engine, "connect", _set_sqlite_pragmas)
            SQLModel.metadata.create_all(engine)
            try:
                with Session(engine) as db:
                    user = User(username="state-user", password_hash="x")
                    db.add(user)
                    db.commit()
                    db.refresh(user)
                    room = Room(name="state", created_by_user_id=user.id)
                    db.add(room)
                    first_track = Track(
                        source=TrackSource.netease,
                        source_track_id="state-1",
                        title="First",
                    )
                    second_track = Track(
                        source=TrackSource.netease,
                        source_track_id="state-2",
                        title="Second",
                    )
                    db.add(first_track)
                    db.add(second_track)
                    db.commit()
                    db.refresh(room)
                    db.refresh(first_track)
                    db.refresh(second_track)
                    db.add(RoomMember(room_id=room.id, user_id=user.id))
                    first_item = RoomQueueItem(
                        room_id=room.id,
                        track_id=first_track.id,
                        ordered_by_user_id=user.id,
                        status=QueueStatus.playing,
                    )
                    second_item = RoomQueueItem(
                        room_id=room.id,
                        track_id=second_track.id,
                        ordered_by_user_id=user.id,
                        status=QueueStatus.queued,
                    )
                    db.add(first_item)
                    db.add(second_item)
                    db.commit()
                    db.refresh(first_item)
                    db.refresh(second_item)
                    db.add(
                        RoomPlaybackState(
                            room_id=room.id,
                            mode=RoomMode.play_enabled,
                            current_queue_item_id=first_item.id,
                            is_playing=True,
                        )
                    )
                    db.commit()

                    lock_states: list[bool] = []

                    async def change_track_during_prepare(
                        prepare_db: Session,
                        prepare_room_id: int,
                        playback: RoomPlaybackState,
                        _queue_item: RoomQueueItem,
                        _track: Track,
                    ) -> bool:
                        lock_states.append(_playback_lock(prepare_room_id).locked())
                        playback.current_queue_item_id = second_item.id
                        playback.is_playing = True
                        prepare_db.add(playback)
                        prepare_db.commit()
                        return False

                    with (
                        patch("app.routers.rooms._require_active_room_member", new=AsyncMock()),
                        patch(
                            "app.routers.rooms._prepare_current_track_for_playback",
                            new=change_track_during_prepare,
                        ),
                    ):
                        response = await room_state(room.id, db, user)

                    self.assertEqual(lock_states, [False])
                    self.assertEqual(response["playback_state"]["current_queue_item_id"], second_item.id)
                    self.assertEqual(response["current_track"]["id"], second_track.id)
                    self.assertEqual(response["current_track"]["title"], "Second")
            finally:
                engine.dispose()


if __name__ == "__main__":
    unittest.main()
