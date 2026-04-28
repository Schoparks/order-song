import sqlite3
from pathlib import Path

from sqlmodel import Session, SQLModel, create_engine, func, select

from app.core.config import settings


def _db_url() -> str:
    return f"sqlite:///{settings.sqlite_path}"


Path(settings.sqlite_path).parent.mkdir(parents=True, exist_ok=True)
engine = create_engine(_db_url(), connect_args={"check_same_thread": False})


def _migrate_columns():
    """Add any missing columns to existing tables (lightweight migration)."""
    migrations = [
        ("users", "is_admin", "BOOLEAN DEFAULT 0"),
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

