from __future__ import annotations

import asyncio
import sqlite3
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
from sqlmodel import Session, SQLModel, create_engine, select

from app import main as main_module
from app.core import config as config_module
from app.core.config import AuthSettings, DEFAULT_JWT_SECRET, Settings, validate_auth_configuration
from app.core.security import hash_password, verify_password
from app.db import _repair_legacy_foreign_key_data, _set_sqlite_pragmas
from app.models import (
    QueueStatus,
    Room,
    RoomMember,
    RoomMode,
    RoomPlaybackState,
    RoomQueueItem,
    Track,
    TrackSource,
    User,
    UserPlaylist,
    UserPlaylistItem,
)
from app.routers.admin import _stage_user_deletion, list_all_rooms, reset_user_password
from app.routers.playback_core import _playback_lock
from app.routers.rooms import create_room, get_room_summary, list_rooms, room_state, set_room_mode
from app.schemas import CreateRoomIn, SetRoomModeIn
from app.ws import RoomHub


class AuthConfigurationTests(unittest.TestCase):
    def test_default_and_short_jwt_secrets_are_rejected(self) -> None:
        for secret in (DEFAULT_JWT_SECRET, "abc123"):
            with self.subTest(secret=secret):
                config = Settings(auth=AuthSettings(jwt_secret=secret))
                with self.assertRaisesRegex(RuntimeError, "at least 32 UTF-8 bytes"):
                    validate_auth_configuration(config)

    def test_only_hs256_is_accepted(self) -> None:
        config = Settings(auth=AuthSettings(jwt_secret="x" * 48, jwt_algorithm="HS512"))
        with self.assertRaisesRegex(RuntimeError, "must be HS256"):
            validate_auth_configuration(config)

        validate_auth_configuration(Settings(auth=AuthSettings(jwt_secret="x" * 48)))

    def test_template_secret_is_replaced_with_persistent_random_value(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.toml"
            config_path.write_text(
                f'[auth]\njwt_secret = "{DEFAULT_JWT_SECRET}"\n',
                encoding="utf-8",
            )
            with (
                patch.object(config_module, "CONFIG_PATH", config_path),
                patch.dict(
                    config_module.os.environ,
                    {"ORDER_SONG_JWT_SECRET": "", "ORDER_SONG_AUTH__JWT_SECRET": ""},
                    clear=False,
                ),
            ):
                config_module.ensure_persistent_jwt_secret()
                first = config_path.read_text(encoding="utf-8")
                config_module.ensure_persistent_jwt_secret()
                second = config_path.read_text(encoding="utf-8")

            self.assertNotIn(DEFAULT_JWT_SECRET, first)
            self.assertEqual(first, second)


class DatabasePragmaTests(unittest.TestCase):
    def test_connection_hook_enables_foreign_keys(self) -> None:
        connection = sqlite3.connect(":memory:")
        try:
            _set_sqlite_pragmas(connection, None)
            enabled = connection.execute("PRAGMA foreign_keys").fetchone()[0]
        finally:
            connection.close()
        self.assertEqual(enabled, 1)

    def test_legacy_missing_last_active_room_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.execute("CREATE TABLE rooms (id INTEGER PRIMARY KEY)")
                connection.execute(
                    "CREATE TABLE users ("
                    "id INTEGER PRIMARY KEY, "
                    "last_active_room_id INTEGER REFERENCES rooms(id))"
                )
                connection.execute("INSERT INTO users VALUES (1, 999)")
                connection.commit()
            finally:
                connection.close()

            _repair_legacy_foreign_key_data(str(db_path))

            connection = sqlite3.connect(db_path)
            try:
                last_room_id = connection.execute(
                    "SELECT last_active_room_id FROM users WHERE id = 1"
                ).fetchone()[0]
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
            finally:
                connection.close()
            self.assertIsNone(last_room_id)
            self.assertEqual(violations, [])

    def test_all_known_legacy_orphans_are_repaired(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy-all.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys=OFF;
                    CREATE TABLE users (
                        id INTEGER PRIMARY KEY,
                        last_active_room_id INTEGER REFERENCES rooms(id)
                    );
                    CREATE TABLE rooms (
                        id INTEGER PRIMARY KEY,
                        created_by_user_id INTEGER NOT NULL REFERENCES users(id)
                    );
                    CREATE TABLE tracks (id INTEGER PRIMARY KEY);
                    CREATE TABLE room_queue_items (
                        id INTEGER PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES rooms(id),
                        track_id INTEGER NOT NULL REFERENCES tracks(id),
                        ordered_by_user_id INTEGER NOT NULL REFERENCES users(id)
                    );
                    CREATE TABLE room_playback_state (
                        room_id INTEGER PRIMARY KEY REFERENCES rooms(id),
                        current_queue_item_id INTEGER REFERENCES room_queue_items(id),
                        is_playing BOOLEAN NOT NULL,
                        position_ms INTEGER NOT NULL
                    );
                    CREATE TABLE room_members (
                        id INTEGER PRIMARY KEY,
                        room_id INTEGER NOT NULL REFERENCES rooms(id),
                        user_id INTEGER NOT NULL REFERENCES users(id)
                    );
                    CREATE TABLE user_playlists (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER NOT NULL REFERENCES users(id)
                    );
                    CREATE TABLE user_playlist_items (
                        id INTEGER PRIMARY KEY,
                        playlist_id INTEGER NOT NULL REFERENCES user_playlists(id),
                        track_id INTEGER NOT NULL REFERENCES tracks(id)
                    );
                    CREATE TABLE track_order_stats (
                        track_id INTEGER PRIMARY KEY REFERENCES tracks(id)
                    );

                    INSERT INTO users VALUES (1, 999);
                    INSERT INTO rooms VALUES (10, 1);
                    INSERT INTO rooms VALUES (11, 999);
                    INSERT INTO rooms VALUES (12, 1);
                    INSERT INTO tracks VALUES (20);

                    INSERT INTO room_queue_items VALUES (30, 10, 20, 1);
                    INSERT INTO room_queue_items VALUES (31, 999, 20, 1);
                    INSERT INTO room_queue_items VALUES (32, 10, 999, 1);
                    INSERT INTO room_queue_items VALUES (33, 10, 20, 999);
                    INSERT INTO room_queue_items VALUES (34, 11, 20, 1);
                    INSERT INTO room_queue_items VALUES (35, 12, 20, 1);
                    INSERT INTO room_playback_state VALUES (10, 33, 1, 1234);
                    INSERT INTO room_playback_state VALUES (11, 34, 1, 100);
                    INSERT INTO room_playback_state VALUES (12, 30, 1, 200);
                    INSERT INTO room_playback_state VALUES (999, NULL, 0, 0);

                    INSERT INTO room_members VALUES (50, 10, 1);
                    INSERT INTO room_members VALUES (51, 999, 1);
                    INSERT INTO room_members VALUES (52, 10, 999);
                    INSERT INTO room_members VALUES (53, 11, 1);

                    INSERT INTO user_playlists VALUES (40, 1);
                    INSERT INTO user_playlists VALUES (41, 999);
                    INSERT INTO user_playlist_items VALUES (60, 40, 20);
                    INSERT INTO user_playlist_items VALUES (61, 999, 20);
                    INSERT INTO user_playlist_items VALUES (62, 40, 999);
                    INSERT INTO user_playlist_items VALUES (63, 41, 20);
                    INSERT INTO track_order_stats VALUES (20);
                    INSERT INTO track_order_stats VALUES (999);
                    """
                )
                connection.commit()
            finally:
                connection.close()

            _repair_legacy_foreign_key_data(str(db_path))

            connection = sqlite3.connect(db_path)
            try:
                self.assertEqual(connection.execute("PRAGMA foreign_key_check").fetchall(), [])
                self.assertEqual(connection.execute("SELECT id FROM rooms ORDER BY id").fetchall(), [(10,), (12,)])
                self.assertEqual(
                    connection.execute("SELECT id FROM room_queue_items ORDER BY id").fetchall(),
                    [(30,), (35,)],
                )
                self.assertEqual(
                    connection.execute("SELECT id FROM room_members ORDER BY id").fetchall(),
                    [(50,)],
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT current_queue_item_id, is_playing, position_ms "
                        "FROM room_playback_state WHERE room_id = 10"
                    ).fetchone(),
                    (None, 0, 0),
                )
                self.assertEqual(
                    connection.execute(
                        "SELECT current_queue_item_id, is_playing, position_ms "
                        "FROM room_playback_state WHERE room_id = 12"
                    ).fetchone(),
                    (None, 0, 0),
                )
                self.assertEqual(
                    connection.execute("SELECT id FROM user_playlists ORDER BY id").fetchall(),
                    [(40,)],
                )
                self.assertEqual(
                    connection.execute("SELECT id FROM user_playlist_items ORDER BY id").fetchall(),
                    [(60,)],
                )
                self.assertEqual(
                    connection.execute("SELECT track_id FROM track_order_stats").fetchall(),
                    [(20,)],
                )
                self.assertIsNone(
                    connection.execute("SELECT last_active_room_id FROM users WHERE id = 1").fetchone()[0]
                )
            finally:
                connection.close()

    def test_unrecognized_foreign_key_violation_fails_repair(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            db_path = Path(temp_dir) / "legacy-unknown.sqlite3"
            connection = sqlite3.connect(db_path)
            try:
                connection.executescript(
                    """
                    PRAGMA foreign_keys=OFF;
                    CREATE TABLE users (id INTEGER PRIMARY KEY);
                    CREATE TABLE unknown_children (
                        id INTEGER PRIMARY KEY,
                        user_id INTEGER REFERENCES users(id)
                    );
                    INSERT INTO unknown_children VALUES (1, 999);
                    """
                )
                connection.commit()
            finally:
                connection.close()

            with self.assertRaisesRegex(RuntimeError, "left violations"):
                _repair_legacy_foreign_key_data(str(db_path))


class _FakeWebSocket:
    def __init__(self) -> None:
        self.close_calls: list[tuple[int, str]] = []

    async def close(self, *, code: int = 1000, reason: str = "") -> None:
        self.close_calls.append((code, reason))


class RoomHubEvictionTests(unittest.IsolatedAsyncioTestCase):
    async def test_disconnect_user_only_evicts_matching_user(self) -> None:
        hub = RoomHub()
        removed = _FakeWebSocket()
        retained = _FakeWebSocket()
        await hub.join(7, removed, user_id=11)
        await hub.join(7, retained, user_id=12)

        count = await hub.disconnect_user(7, 11)

        self.assertEqual(count, 1)
        self.assertEqual(removed.close_calls, [(1008, "room membership ended")])
        self.assertEqual(retained.close_calls, [])
        self.assertNotIn(removed, hub._room_sockets[7])
        self.assertIn(retained, hub._room_sockets[7])

    async def test_disconnect_room_evicts_every_socket_and_clears_presence(self) -> None:
        hub = RoomHub()
        sockets = [_FakeWebSocket(), _FakeWebSocket()]
        await hub.join(9, sockets[0], user_id=21)
        await hub.join(9, sockets[1], user_id=22)

        count = await hub.disconnect_room(9)

        self.assertEqual(count, 2)
        self.assertNotIn(9, hub._room_sockets)
        self.assertNotIn(9, hub._last_activity)
        self.assertTrue(all(ws.close_calls == [(1001, "room ended")] for ws in sockets))


class LifespanTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifespan_owns_and_cancels_stale_cleanup_task(self) -> None:
        with patch.object(main_module, "init_db") as init_db:
            async with main_module._lifespan(main_module.app):
                cleanup_tasks = [
                    task
                    for task in asyncio.all_tasks()
                    if task.get_name() == "stale-room-member-cleanup"
                ]
                self.assertEqual(len(cleanup_tasks), 1)
                self.assertFalse(cleanup_tasks[0].done())
            self.assertTrue(cleanup_tasks[0].cancelled())
        init_db.assert_called_once_with()


class AdminUserManagementTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "admin.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_admin_can_reset_a_user_password_to_default(self) -> None:
        with Session(self.engine) as db:
            admin = User(username="admin", password_hash=hash_password("admin-pass"), is_admin=True)
            user = User(username="listener", password_hash=hash_password("forgotten"))
            db.add(admin)
            db.add(user)
            db.commit()
            db.refresh(admin)
            db.refresh(user)

            result = reset_user_password(int(user.id), db, admin)

            db.refresh(user)
            self.assertEqual(result, {"ok": True})
            self.assertTrue(verify_password("123456", user.password_hash))
            self.assertFalse(verify_password("forgotten", user.password_hash))


class UserDeletionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "lifecycle.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_user_deletion_removes_owned_and_empty_rooms_without_orphans(self) -> None:
        with Session(self.engine) as db:
            victim = User(username="victim", password_hash="x")
            survivor = User(username="survivor", password_hash="x", is_admin=True)
            db.add(victim)
            db.add(survivor)
            db.commit()
            db.refresh(victim)
            db.refresh(survivor)

            owned_room = Room(name="owned", created_by_user_id=victim.id)
            shared_room = Room(name="shared", created_by_user_id=survivor.id)
            emptied_room = Room(name="emptied", created_by_user_id=survivor.id)
            db.add(owned_room)
            db.add(shared_room)
            db.add(emptied_room)
            db.commit()
            for room in (owned_room, shared_room, emptied_room):
                db.refresh(room)

            db.add(RoomMember(room_id=owned_room.id, user_id=victim.id))
            db.add(RoomMember(room_id=owned_room.id, user_id=survivor.id))
            db.add(RoomMember(room_id=shared_room.id, user_id=victim.id))
            db.add(RoomMember(room_id=shared_room.id, user_id=survivor.id))
            db.add(RoomMember(room_id=emptied_room.id, user_id=victim.id))
            track = Track(source=TrackSource.netease, source_track_id="track-1", title="Track")
            db.add(track)
            db.commit()
            db.refresh(track)

            current = RoomQueueItem(
                room_id=shared_room.id,
                track_id=track.id,
                ordered_by_user_id=victim.id,
                status=QueueStatus.playing,
            )
            extra_playing = RoomQueueItem(
                room_id=shared_room.id,
                track_id=track.id,
                ordered_by_user_id=survivor.id,
                status=QueueStatus.playing,
            )
            owned_item = RoomQueueItem(
                room_id=owned_room.id,
                track_id=track.id,
                ordered_by_user_id=victim.id,
            )
            db.add(current)
            db.add(extra_playing)
            db.add(owned_item)
            db.commit()
            db.refresh(current)
            db.add(
                RoomPlaybackState(
                    room_id=shared_room.id,
                    mode=RoomMode.play_enabled,
                    current_queue_item_id=current.id,
                    is_playing=True,
                )
            )
            db.add(RoomPlaybackState(room_id=owned_room.id))
            playlist = UserPlaylist(user_id=victim.id, name="victim playlist")
            db.add(playlist)
            survivor.last_active_room_id = owned_room.id
            db.add(survivor)
            db.commit()
            db.refresh(playlist)
            db.add(UserPlaylistItem(playlist_id=playlist.id, track_id=track.id))
            db.commit()

            destroyed, member_rooms, queue_rooms, playback_rooms = _stage_user_deletion(db, victim)
            db.commit()

            self.assertEqual(destroyed, {owned_room.id, emptied_room.id})
            self.assertEqual(member_rooms, {owned_room.id, shared_room.id, emptied_room.id})
            self.assertEqual(queue_rooms, {owned_room.id, shared_room.id})
            self.assertEqual(playback_rooms, {shared_room.id})
            self.assertIsNone(db.get(User, victim.id))
            self.assertIsNone(db.get(Room, owned_room.id))
            self.assertIsNone(db.get(Room, emptied_room.id))
            self.assertIsNotNone(db.get(Room, shared_room.id))

            playback = db.get(RoomPlaybackState, shared_room.id)
            self.assertIsNone(playback.current_queue_item_id)
            self.assertFalse(playback.is_playing)
            self.assertEqual(playback.position_ms, 0)
            remaining_items = db.exec(
                select(RoomQueueItem).where(RoomQueueItem.room_id == shared_room.id)
            ).all()
            self.assertEqual(len(remaining_items), 1)
            self.assertEqual(remaining_items[0].status, QueueStatus.played)
            self.assertEqual(
                db.exec(select(RoomMember).where(RoomMember.room_id == shared_room.id)).all(),
                [db.exec(select(RoomMember).where(RoomMember.user_id == survivor.id)).one()],
            )
            db.refresh(survivor)
            self.assertIsNone(survivor.last_active_room_id)
            self.assertEqual(db.connection().exec_driver_sql("PRAGMA foreign_key_check").fetchall(), [])


class RoomTransactionAndQueryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp_dir = tempfile.TemporaryDirectory()
        db_path = Path(self.temp_dir.name) / "rooms.sqlite3"
        self.engine = create_engine(
            f"sqlite:///{db_path}",
            connect_args={"check_same_thread": False},
            poolclass=NullPool,
        )
        event.listen(self.engine, "connect", _set_sqlite_pragmas)
        SQLModel.metadata.create_all(self.engine)

    def tearDown(self) -> None:
        self.engine.dispose()
        self.temp_dir.cleanup()

    def test_create_room_rolls_back_every_invariant_on_commit_failure(self) -> None:
        with Session(self.engine) as db:
            creator = User(username="creator", password_hash="x")
            db.add(creator)
            db.commit()
            db.refresh(creator)

            with patch.object(db, "commit", side_effect=RuntimeError("write failed")):
                with self.assertRaisesRegex(RuntimeError, "write failed"):
                    create_room(CreateRoomIn(name="atomic"), db, creator)

        with Session(self.engine) as verify_db:
            self.assertEqual(verify_db.exec(select(Room)).all(), [])
            self.assertEqual(verify_db.exec(select(RoomMember)).all(), [])
            self.assertEqual(verify_db.exec(select(RoomPlaybackState)).all(), [])
            persisted_creator = verify_db.exec(select(User)).one()
            self.assertIsNone(persisted_creator.last_active_room_id)

    def test_room_lists_batch_members_and_creators(self) -> None:
        with Session(self.engine) as db:
            admin = User(username="admin", password_hash="x", is_admin=True)
            listener = User(username="listener", password_hash="x")
            db.add(admin)
            db.add(listener)
            db.commit()
            db.refresh(admin)
            db.refresh(listener)
            rooms = [
                Room(name="one", created_by_user_id=admin.id),
                Room(name="two", created_by_user_id=listener.id),
            ]
            db.add(rooms[0])
            db.add(rooms[1])
            db.commit()
            for room in rooms:
                db.refresh(room)
            db.add(RoomMember(room_id=rooms[0].id, user_id=admin.id))
            db.add(RoomMember(room_id=rooms[0].id, user_id=listener.id))
            db.add(RoomMember(room_id=rooms[1].id, user_id=listener.id))
            track = Track(
                source=TrackSource.bilibili,
                source_track_id="BV-summary",
                title="Current song",
                artist="Artist",
                cover_url="https://example.test/cover.jpg",
                audio_url="https://secret-upstream.test/audio.mp4",
            )
            db.add(track)
            db.commit()
            db.refresh(track)
            current_item = RoomQueueItem(
                room_id=rooms[0].id,
                track_id=track.id,
                ordered_by_user_id=admin.id,
                status=QueueStatus.playing,
            )
            db.add(current_item)
            db.commit()
            db.refresh(current_item)
            db.add(
                RoomPlaybackState(
                    room_id=rooms[0].id,
                    mode=RoomMode.play_enabled,
                    current_queue_item_id=current_item.id,
                    is_playing=True,
                )
            )
            db.commit()

            selects: list[str] = []

            def count_selects(_conn, _cursor, statement, _parameters, _context, _many):
                if statement.lstrip().upper().startswith("SELECT"):
                    selects.append(statement)

            event.listen(self.engine, "before_cursor_execute", count_selects)
            try:
                public_result = list_rooms(db, admin)
                public_select_count = len(selects)
                selects.clear()
                admin_result = list_all_rooms(db, admin)
                admin_select_count = len(selects)
            finally:
                event.remove(self.engine, "before_cursor_execute", count_selects)

            self.assertEqual(public_select_count, 3)
            self.assertEqual(admin_select_count, 3)
            self.assertEqual(
                {room["name"]: room["member_count"] for room in public_result},
                {"one": 2, "two": 1},
            )
            one_summary = next(room for room in public_result if room["name"] == "one")
            self.assertEqual(
                one_summary["now_playing"],
                {
                    "title": "Current song",
                    "artist": "Artist",
                    "source": "bilibili",
                    "cover_url": "https://example.test/cover.jpg",
                    "is_playing": True,
                },
            )
            self.assertNotIn("audio_url", one_summary["now_playing"])
            self.assertIsNone(next(room for room in public_result if room["name"] == "two")["now_playing"])
            self.assertEqual(
                {room["name"]: room["created_by"] for room in admin_result},
                {"one": "admin", "two": "listener"},
            )
            self.assertEqual(
                {
                    room["name"]: {member["username"] for member in room["members"]}
                    for room in admin_result
                },
                {"one": {"admin", "listener"}, "two": {"listener"}},
            )

    def test_room_summary_requires_membership_and_keeps_member_preview(self) -> None:
        with Session(self.engine) as db:
            member = User(username="member", password_hash="x")
            guest = User(username="guest", password_hash="x")
            db.add(member)
            db.add(guest)
            db.commit()
            db.refresh(member)
            db.refresh(guest)
            room = Room(name="members only", created_by_user_id=member.id)
            db.add(room)
            db.commit()
            db.refresh(room)
            db.add(RoomMember(room_id=room.id, user_id=member.id))
            db.commit()

            with patch("app.routers.rooms.hub.note_activity", new=AsyncMock()) as note_activity:
                result = asyncio.run(get_room_summary(room.id, db, member))

            self.assertEqual(result["id"], room.id)
            self.assertEqual(result["member_count"], 1)
            self.assertEqual(result["member_names"], ["member"])
            self.assertIsNone(result["now_playing"])
            note_activity.assert_awaited_once_with(room.id, member.id)

            with self.assertRaisesRegex(Exception, "403"):
                asyncio.run(get_room_summary(room.id, db, guest))


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
