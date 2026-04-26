from sqlmodel import Session, SQLModel, create_engine

from app.core.config import settings


def _db_url() -> str:
    # sqlite file under backend/ working directory
    return f"sqlite:///{settings.sqlite_path}"


engine = create_engine(_db_url(), connect_args={"check_same_thread": False})


def init_db() -> None:
    # Import models so they are registered on SQLModel.metadata
    from app import models  # noqa: F401

    SQLModel.metadata.create_all(engine)


def get_session():
    with Session(engine) as session:
        yield session

