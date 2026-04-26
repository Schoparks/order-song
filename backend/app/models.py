from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


def utcnow() -> datetime:
    return datetime.utcnow()


class RoomMode(str, Enum):
    order_only = "order_only"
    play_enabled = "play_enabled"


class TrackSource(str, Enum):
    qq = "qq"
    netease = "netease"
    kugou = "kugou"
    bilibili = "bilibili"


class User(SQLModel, table=True):
    __tablename__ = "users"

    id: Optional[int] = Field(default=None, primary_key=True)
    username: str = Field(index=True, unique=True, max_length=64)
    password_hash: str
    created_at: datetime = Field(default_factory=utcnow, index=True)
    last_active_room_id: Optional[int] = Field(default=None, foreign_key="rooms.id")


class Room(SQLModel, table=True):
    __tablename__ = "rooms"

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(max_length=120)
    created_by_user_id: int = Field(foreign_key="users.id", index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class RoomMember(SQLModel, table=True):
    __tablename__ = "room_members"
    __table_args__ = (UniqueConstraint("room_id", "user_id", name="uq_room_members_room_user"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    room_id: int = Field(foreign_key="rooms.id", index=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    joined_at: datetime = Field(default_factory=utcnow, index=True)


class Track(SQLModel, table=True):
    __tablename__ = "tracks"
    __table_args__ = (UniqueConstraint("source", "source_track_id", name="uq_tracks_source_id"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    source: TrackSource = Field(index=True)
    source_track_id: str = Field(index=True, max_length=128)
    title: str = Field(max_length=200)
    artist: Optional[str] = Field(default=None, max_length=120)
    duration_ms: Optional[int] = None
    cover_url: Optional[str] = None
    audio_url: Optional[str] = None


class QueueStatus(str, Enum):
    queued = "queued"
    playing = "playing"
    played = "played"
    removed = "removed"


class RoomQueueItem(SQLModel, table=True):
    __tablename__ = "room_queue_items"

    id: Optional[int] = Field(default=None, primary_key=True)
    room_id: int = Field(foreign_key="rooms.id", index=True)
    track_id: int = Field(foreign_key="tracks.id", index=True)
    ordered_by_user_id: int = Field(foreign_key="users.id", index=True)
    status: QueueStatus = Field(default=QueueStatus.queued, index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class RoomPlaybackState(SQLModel, table=True):
    __tablename__ = "room_playback_state"

    room_id: int = Field(foreign_key="rooms.id", primary_key=True)
    mode: RoomMode = Field(default=RoomMode.order_only)
    current_queue_item_id: Optional[int] = Field(default=None, foreign_key="room_queue_items.id")
    is_playing: bool = Field(default=False)
    position_ms: int = Field(default=0)
    volume: int = Field(default=50)
    updated_at: datetime = Field(default_factory=utcnow, index=True)


class UserPlaylist(SQLModel, table=True):
    __tablename__ = "user_playlists"

    id: Optional[int] = Field(default=None, primary_key=True)
    user_id: int = Field(foreign_key="users.id", index=True)
    name: str = Field(max_length=120)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class UserPlaylistItem(SQLModel, table=True):
    __tablename__ = "user_playlist_items"
    __table_args__ = (UniqueConstraint("playlist_id", "track_id", name="uq_playlist_track"),)

    id: Optional[int] = Field(default=None, primary_key=True)
    playlist_id: int = Field(foreign_key="user_playlists.id", index=True)
    track_id: int = Field(foreign_key="tracks.id", index=True)
    created_at: datetime = Field(default_factory=utcnow, index=True)


class TrackOrderStats(SQLModel, table=True):
    __tablename__ = "track_order_stats"

    track_id: int = Field(foreign_key="tracks.id", primary_key=True)
    order_count: int = Field(default=0, index=True)
    last_ordered_at: datetime = Field(default_factory=utcnow, index=True)

