from __future__ import annotations

from datetime import datetime
from typing import Optional

from pydantic import BaseModel, ConfigDict, Field

from app.models import RoomMode, TrackSource


class UserPublic(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    username: str
    is_admin: bool = False
    created_at: datetime


class RegisterIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)
    password: str = Field(min_length=4, max_length=128)


class LoginIn(BaseModel):
    username: str
    password: str


class LoginOut(BaseModel):
    token: str
    user: UserPublic


class UpdateUsernameIn(BaseModel):
    username: str = Field(min_length=1, max_length=64)


class UpdatePasswordIn(BaseModel):
    old_password: str = Field(min_length=4, max_length=128)
    new_password: str = Field(min_length=4, max_length=128)


class RoomOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    name: str
    created_by_user_id: int
    created_at: datetime


class CreateRoomIn(BaseModel):
    name: Optional[str] = Field(default=None, max_length=120)


class TrackOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: int
    source: TrackSource
    source_track_id: str
    title: str
    artist: Optional[str] = None
    duration_ms: Optional[int] = None
    cover_url: Optional[str] = None
    audio_url: Optional[str] = None
    loudness_gain_db: Optional[float] = None
    loudness_peak: Optional[float] = None
    loudness_source: Optional[str] = None
    loudness_error: Optional[str] = None


class SearchTrackPartOut(BaseModel):
    source: TrackSource
    source_track_id: str
    title: str
    artist: Optional[str] = None
    duration_ms: Optional[int] = None
    cover_url: Optional[str] = None


class SearchTrackOut(SearchTrackPartOut):
    parts: list[SearchTrackPartOut] = Field(default_factory=list)


class PlaybackStateOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    room_id: int
    mode: RoomMode
    current_queue_item_id: Optional[int] = None
    is_playing: bool
    position_ms: int
    volume: int
    updated_at: datetime


class PlaybackControlIn(BaseModel):
    position_ms: Optional[int] = None
    expected_queue_item_id: Optional[int] = None


class VolumeControlIn(BaseModel):
    volume: int = Field(default=50, ge=0, le=100)
