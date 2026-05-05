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
    finally:
        cursor.close()


def _configure_sqlite_database() -> None:
    conn = sqlite3.connect(settings.sqlite_path, timeout=_SQLITE_BUSY_TIMEOUT_MS / 1000)
    try:
        conn.execute(f"PRAGMA busy_timeout={_SQLITE_BUSY_TIMEOUT_MS}")
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


def init_db() -> None:
    from app import models  # noqa: F401

    _configure_sqlite_database()
    SQLModel.metadata.create_all(engine)
    _migrate_columns()
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
