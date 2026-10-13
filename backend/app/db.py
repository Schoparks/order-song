import sqlite3
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.pool import NullPool
from sqlmodel import Session, SQLModel, create_engine, func, select

from app.core.config import settings


_SQLITE_BUSY_TIMEOUT_MS = 30_000


def _db_url() -> str:
    return f"sqlite:///{settings.sqlite_path}"


Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(
    _db_url(),
    connect_args={
        "check_same_thread": False,
        "timeout": _SQLITE_BUSY_TIMEOUT_MS / 1000,
    },
    poolclass=NullPool,
)


@event.listens_for(engine, "connect")
def _set_sqlite_pragmas(dbapi_connection, _connection_record):
    cursor = dbapi_connection.cursor()
    try:
        cursor.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        cursor.execute("PRAGMA foreign_keys=ON")
    finally:
        cursor.close()


def _configure_sqlite_database() -> None:
    conn = sqlite3.connect(settings.sqlite_path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            conn.execute("PRAGMA journal_mode=WAL")
        except sqlite3.DatabaseError:
            pass
        conn.commit()
    finally:
        conn.close()


def _migrate_columns():
    """Add any missing columns to existing tables (lightweight migration)."""
    migrations = [
        ("users", "is_admin", "BOOLEAN DEFAULT 0"),
        ("tracks", "availability", "VARCHAR(16) NOT NULL DEFAULT 'unknown'"),
        ("tracks", "unavailable_reason", "VARCHAR(240)"),
        ("tracks", "availability_checked_at", "DATETIME"),
        ("tracks", "loudness_gain_db", "REAL"),
        ("tracks", "loudness_peak", "REAL"),
        ("tracks", "loudness_source", "VARCHAR(40)"),
        ("tracks", "loudness_fetched_at", "DATETIME"),
        ("tracks", "loudness_error", "VARCHAR(240)"),
        ("tracks", "normalization_gain", "REAL"),
        ("tracks", "normalization_rms", "REAL"),
        ("tracks", "normalization_peak", "REAL"),
        ("tracks", "normalization_analyzed_at", "DATETIME"),
        ("tracks", "normalization_error", "VARCHAR(240)"),
    ]
    conn = sqlite3.connect(settings.sqlite_path)
    conn.execute("PRAGMA foreign_keys=ON")
    cur = conn.cursor()
    for table, col, typedef in migrations:
        try:
            cur.execute(f"SELECT {col} FROM {table} LIMIT 1")
        except sqlite3.OperationalError:
            try:
                cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {typedef}")
            except sqlite3.OperationalError:
                pass
    conn.commit()
    conn.close()


def _repair_legacy_foreign_key_data(sqlite_path: str | None = None) -> None:
    """Repair legacy orphan rows created before FK enforcement.

    Older databases were opened without ``PRAGMA foreign_keys``.  Keep this
    repair explicit instead of relying on cascades which those databases do
    not define.  A room whose creator no longer exists is not transferable, so
    the room and all of its room-scoped data are removed.  Queue items whose
    ordering user disappeared are likewise removed after stopping any playback
    row that referenced them.
    """
    conn = sqlite3.connect(sqlite_path or settings.sqlite_path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
        conn.execute("PRAGMA foreign_keys=ON")
        tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }

        def has(*names: str) -> bool:
            return all(name in tables for name in names)

        def has_columns(table: str, *names: str) -> bool:
            if table not in tables:
                return False
            columns = {
                row[1] for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()
            }
            return all(name in columns for name in names)

        try:
            if has("users", "rooms"):
                # Always repair the nullable pointer, including very old test
                # or development schemas that predate room creator tracking.
                conn.execute(
                    """
                    UPDATE users
                    SET last_active_room_id = NULL
                    WHERE last_active_room_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM rooms
                          WHERE rooms.id = users.last_active_room_id
                      )
                    """
                )
                if has_columns("rooms", "created_by_user_id"):
                    # A missing creator invalidates the whole room. Clear
                    # nullable user pointers before room-scoped cleanup.
                    conn.execute(
                        """
                        UPDATE users
                        SET last_active_room_id = NULL
                        WHERE last_active_room_id IS NOT NULL
                          AND EXISTS (
                              SELECT 1 FROM rooms
                              WHERE rooms.id = users.last_active_room_id
                                AND NOT EXISTS (
                                    SELECT 1 FROM users AS creator
                                    WHERE creator.id = rooms.created_by_user_id
                                )
                          )
                        """
                    )

            if has("room_playback_state", "room_queue_items"):
                # A playback pointer may reference an orphan queue row or a row
                # belonging to another room. Stop it before queue cleanup.
                conn.execute(
                    """
                    UPDATE room_playback_state
                    SET current_queue_item_id = NULL,
                        is_playing = 0,
                        position_ms = 0
                    WHERE current_queue_item_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM room_queue_items
                          WHERE room_queue_items.id = room_playback_state.current_queue_item_id
                            AND room_queue_items.room_id = room_playback_state.room_id
                      )
                    """
                )

            if has("room_playback_state", "room_queue_items", "rooms", "tracks", "users"):
                # Also detach queue rows that are about to be deleted because
                # their room, track, or ordering user is missing.
                conn.execute(
                    """
                    UPDATE room_playback_state
                    SET current_queue_item_id = NULL,
                        is_playing = 0,
                        position_ms = 0
                    WHERE current_queue_item_id IN (
                        SELECT item.id
                        FROM room_queue_items AS item
                        WHERE NOT EXISTS (SELECT 1 FROM rooms WHERE rooms.id = item.room_id)
                           OR NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.id = item.track_id)
                           OR NOT EXISTS (SELECT 1 FROM users WHERE users.id = item.ordered_by_user_id)
                    )
                    """
                )

            if has("room_playback_state", "rooms"):
                conn.execute(
                    """
                    DELETE FROM room_playback_state
                    WHERE NOT EXISTS (
                        SELECT 1 FROM rooms
                        WHERE rooms.id = room_playback_state.room_id
                    )
                    """
                )
                if has("users") and has_columns("rooms", "created_by_user_id"):
                    conn.execute(
                        """
                        DELETE FROM room_playback_state
                        WHERE EXISTS (
                            SELECT 1 FROM rooms
                            WHERE rooms.id = room_playback_state.room_id
                              AND NOT EXISTS (
                                  SELECT 1 FROM users
                                  WHERE users.id = rooms.created_by_user_id
                              )
                        )
                        """
                    )

            if has("room_queue_items", "rooms", "tracks", "users"):
                conn.execute(
                    """
                    DELETE FROM room_queue_items
                    WHERE NOT EXISTS (SELECT 1 FROM rooms WHERE rooms.id = room_queue_items.room_id)
                       OR NOT EXISTS (SELECT 1 FROM tracks WHERE tracks.id = room_queue_items.track_id)
                       OR NOT EXISTS (SELECT 1 FROM users WHERE users.id = room_queue_items.ordered_by_user_id)
                    """
                )
                if has_columns("rooms", "created_by_user_id"):
                    conn.execute(
                        """
                        DELETE FROM room_queue_items
                        WHERE EXISTS (
                            SELECT 1 FROM rooms
                            WHERE rooms.id = room_queue_items.room_id
                              AND NOT EXISTS (
                                  SELECT 1 FROM users
                                  WHERE users.id = rooms.created_by_user_id
                              )
                        )
                        """
                    )

            if has("room_members", "rooms", "users"):
                conn.execute(
                    """
                    DELETE FROM room_members
                    WHERE NOT EXISTS (SELECT 1 FROM rooms WHERE rooms.id = room_members.room_id)
                       OR NOT EXISTS (SELECT 1 FROM users WHERE users.id = room_members.user_id)
                    """
                )
                if has_columns("rooms", "created_by_user_id"):
                    conn.execute(
                        """
                        DELETE FROM room_members
                        WHERE EXISTS (
                            SELECT 1 FROM rooms
                            WHERE rooms.id = room_members.room_id
                              AND NOT EXISTS (
                                  SELECT 1 FROM users
                                  WHERE users.id = rooms.created_by_user_id
                              )
                        )
                        """
                    )

            if has("rooms", "users") and has_columns("rooms", "created_by_user_id"):
                conn.execute(
                    """
                    DELETE FROM rooms
                    WHERE NOT EXISTS (
                        SELECT 1 FROM users WHERE users.id = rooms.created_by_user_id
                    )
                    """
                )
                # Clear pointers to any other missing room rows.
                conn.execute(
                    """
                    UPDATE users
                    SET last_active_room_id = NULL
                    WHERE last_active_room_id IS NOT NULL
                      AND NOT EXISTS (
                          SELECT 1 FROM rooms WHERE rooms.id = users.last_active_room_id
                      )
                    """
                )

            if has("user_playlist_items", "user_playlists", "tracks"):
                conn.execute(
                    """
                    DELETE FROM user_playlist_items
                    WHERE NOT EXISTS (
                            SELECT 1 FROM user_playlists
                            WHERE user_playlists.id = user_playlist_items.playlist_id
                        )
                       OR NOT EXISTS (
                            SELECT 1 FROM tracks
                            WHERE tracks.id = user_playlist_items.track_id
                        )
                    """
                )

            if has("user_playlists", "users"):
                if "user_playlist_items" in tables:
                    conn.execute(
                        """
                        DELETE FROM user_playlist_items
                        WHERE playlist_id IN (
                            SELECT id FROM user_playlists
                            WHERE NOT EXISTS (
                                SELECT 1 FROM users
                                WHERE users.id = user_playlists.user_id
                            )
                        )
                        """
                    )
                conn.execute(
                    """
                    DELETE FROM user_playlists
                    WHERE NOT EXISTS (
                        SELECT 1 FROM users WHERE users.id = user_playlists.user_id
                    )
                    """
                )

            if has("track_order_stats", "tracks"):
                conn.execute(
                    """
                    DELETE FROM track_order_stats
                    WHERE NOT EXISTS (
                        SELECT 1 FROM tracks WHERE tracks.id = track_order_stats.track_id
                    )
                    """
                )

            violations = conn.execute("PRAGMA foreign_key_check").fetchall()
            if violations:
                preview = ", ".join(repr(row) for row in violations[:10])
                raise RuntimeError(
                    "legacy foreign-key repair left violations: " + preview
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
    finally:
        conn.close()


def init_db() -> None:
    from app import models  # noqa: F401

    _configure_sqlite_database()
    SQLModel.metadata.create_all(engine)
    _migrate_columns()
    _repair_legacy_foreign_key_data()
    _bootstrap_default_admin()


def _bootstrap_default_admin() -> None:
    from app.core.security import hash_password
    from app.models import User

    bootstrap = settings.admin.bootstrap
    username = bootstrap.username.strip()
    if not bootstrap.enabled or not username or not bootstrap.password:
        return

    with Session(engine) as session:
        user_count = session.exec(select(func.count()).select_from(User)).one()
        if int(user_count) > 0:
            return
        user = User(username=username, password_hash=hash_password(bootstrap.password), is_admin=True)
        session.add(user)
        session.commit()


def get_session():
    with Session(engine) as session:
        yield session
