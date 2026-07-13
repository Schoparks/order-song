from __future__ import annotations

import os
import re
import secrets
import shutil
from pathlib import Path
from typing import Any

import tomllib
from pydantic import BaseModel, ConfigDict, Field, field_validator


PROJECT_ROOT = Path(__file__).resolve().parents[3]
CONFIG_TEMPLATE_PATH = PROJECT_ROOT / "config_template.toml"
CONFIG_PATH = PROJECT_ROOT / "config.toml"
DEFAULT_JWT_SECRET = "dev-secret-change-me"
_INSECURE_JWT_SECRETS = {"", DEFAULT_JWT_SECRET, "change-me", "secret"}


def _positive_int(value: int) -> int:
    return max(1, int(value))


def _non_negative_int(value: int) -> int:
    return max(0, int(value))


def _volume(value: int) -> int:
    return max(0, min(100, int(value)))


class ServerSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    port: int = 5732
    cors_allow_origins: str = "*"

    _valid_port = field_validator("port")(_positive_int)


class AppSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    name: str = "order-song"


class DatabaseSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sqlite_path: str = "backend/order_song.sqlite3"


class AuthSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    jwt_secret: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    jwt_exp_minutes: int = 60 * 24 * 14

    _valid_exp = field_validator("jwt_exp_minutes")(_positive_int)


class AdminBootstrapSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    username: str = "admin"
    password: str = ""


class AdminSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    bootstrap: AdminBootstrapSettings = Field(default_factory=AdminBootstrapSettings)


class BilibiliSearchSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    page_size: int = 20
    result_limit: int = 10

    _valid_page_size = field_validator("page_size")(_positive_int)
    _valid_result_limit = field_validator("result_limit")(_positive_int)


class NeteaseSearchSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    api_limit: int = 50
    result_limit: int = 24

    _valid_api_limit = field_validator("api_limit")(_positive_int)
    _valid_result_limit = field_validator("result_limit")(_positive_int)


class QQMusicSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = False
    session_path: str = "backend/qq_music_session.json"
    device_path: str = "backend/qq_music_device.json"
    qr_path: str = "backend/qq_music_login_qr.png"
    search_limit: int = 24
    playlist_page_size: int = 100
    login_timeout_s: int = 180
    refresh_interval_s: int = 21600
    request_timeout_s: float = 20.0

    _valid_search_limit = field_validator("search_limit")(_positive_int)
    _valid_playlist_page_size = field_validator("playlist_page_size")(_positive_int)
    _valid_login_timeout = field_validator("login_timeout_s")(_positive_int)
    _valid_refresh_interval = field_validator("refresh_interval_s")(_positive_int)

    @field_validator("request_timeout_s")
    @classmethod
    def _valid_request_timeout(cls, value: float) -> float:
        return max(0.1, float(value))


class SearchSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    aggregate_limit: int = 40
    max_duration_s: int = 1800
    attempts: int = 3
    timeout_s: float = 12.0
    bilibili: BilibiliSearchSettings = Field(default_factory=BilibiliSearchSettings)
    netease: NeteaseSearchSettings = Field(default_factory=NeteaseSearchSettings)

    _valid_aggregate_limit = field_validator("aggregate_limit")(_positive_int)
    _valid_max_duration = field_validator("max_duration_s")(_positive_int)
    _valid_attempts = field_validator("attempts")(_positive_int)

    @field_validator("timeout_s")
    @classmethod
    def _valid_timeout(cls, value: float) -> float:
        return max(0.1, float(value))


class TrendingSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    limit: int = 50

    _valid_limit = field_validator("limit")(_positive_int)


class RoomsSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    list_limit: int = 200
    member_preview_count: int = 3
    history_limit: int = 200
    default_volume: int = 50
    stale_check_interval_s: int = 300
    stale_timeout_s: int = 1800

    _valid_list_limit = field_validator("list_limit")(_positive_int)
    _valid_member_preview_count = field_validator("member_preview_count")(_non_negative_int)
    _valid_history_limit = field_validator("history_limit")(_positive_int)
    _valid_default_volume = field_validator("default_volume")(_volume)
    _valid_stale_check_interval = field_validator("stale_check_interval_s")(_positive_int)
    _valid_stale_timeout = field_validator("stale_timeout_s")(_positive_int)


class ClientSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    sync_interval_ms: int = 5000
    trending_sync_interval_ms: int = 60000
    room_check_interval_ms: int = 15000
    rooms_refresh_interval_ms: int = 5000
    search_history_limit: int = 30

    _valid_sync_interval = field_validator("sync_interval_ms")(_positive_int)
    _valid_trending_sync_interval = field_validator("trending_sync_interval_ms")(_positive_int)
    _valid_room_check_interval = field_validator("room_check_interval_ms")(_positive_int)
    _valid_rooms_refresh_interval = field_validator("rooms_refresh_interval_ms")(_positive_int)
    _valid_search_history_limit = field_validator("search_history_limit")(_positive_int)


class AudioLoudnessSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    enabled: bool = True
    ffmpeg_path: str = "ffmpeg"
    timeout_s: float = 45.0
    max_duration_s: int = 0
    prefetch_min_interval_s: int = 45
    target_lufs: float = -16.0
    true_peak_headroom_db: float = -1.0
    min_gain_db: float = -12.0
    max_gain_db: float = 12.0

    _valid_max_duration = field_validator("max_duration_s")(_non_negative_int)
    _valid_prefetch_min_interval = field_validator("prefetch_min_interval_s")(_positive_int)

    @field_validator("timeout_s")
    @classmethod
    def _valid_timeout(cls, value: float) -> float:
        return max(0.1, float(value))

    @field_validator("min_gain_db", "max_gain_db")
    @classmethod
    def _valid_gain(cls, value: float) -> float:
        return max(-36.0, min(36.0, float(value)))


class UpstreamSettings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    yt_dlp_timeout_s: int = 20
    bilibili_audio_timeout_s: float = 10.0
    netease_playlist_timeout_s: float = 15.0
    netease_playlist_detail_timeout_s: float = 30.0
    netease_playlist_detail_batch_size: int = 500

    _valid_yt_dlp_timeout = field_validator("yt_dlp_timeout_s")(_positive_int)
    _valid_detail_batch_size = field_validator("netease_playlist_detail_batch_size")(_positive_int)

    @field_validator(
        "bilibili_audio_timeout_s",
        "netease_playlist_timeout_s",
        "netease_playlist_detail_timeout_s",
    )
    @classmethod
    def _valid_timeout(cls, value: float) -> float:
        return max(0.1, float(value))


class Settings(BaseModel):
    model_config = ConfigDict(extra="ignore")

    app: AppSettings = Field(default_factory=AppSettings)
    legacy_app_name: str | None = Field(default=None, alias="app_name")
    server: ServerSettings = Field(default_factory=ServerSettings)
    database: DatabaseSettings = Field(default_factory=DatabaseSettings)
    auth: AuthSettings = Field(default_factory=AuthSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    search: SearchSettings = Field(default_factory=SearchSettings)
    trending: TrendingSettings = Field(default_factory=TrendingSettings)
    rooms: RoomsSettings = Field(default_factory=RoomsSettings)
    client: ClientSettings = Field(default_factory=ClientSettings)
    audio_loudness: AudioLoudnessSettings = Field(default_factory=AudioLoudnessSettings)
    upstream: UpstreamSettings = Field(default_factory=UpstreamSettings)
    qq_music: QQMusicSettings = Field(default_factory=QQMusicSettings)

    @property
    def app_name(self) -> str:
        return self.legacy_app_name or self.app.name

    @property
    def cors_allow_origins(self) -> str:
        return self.server.cors_allow_origins

    @property
    def jwt_secret(self) -> str:
        return self.auth.jwt_secret

    @property
    def jwt_algorithm(self) -> str:
        return self.auth.jwt_algorithm

    @property
    def jwt_exp_minutes(self) -> int:
        return self.auth.jwt_exp_minutes

    @property
    def sqlite_path(self) -> str:
        return resolve_project_path(self.database.sqlite_path).as_posix()

    def public_config(self) -> dict[str, Any]:
        return {
            "trending": {"limit": self.trending.limit},
            "client": self.client.model_dump(),
            "qq_music": {"enabled": self.qq_music.enabled},
        }


def resolve_project_path(path: str | Path) -> Path:
    raw = Path(path)
    if raw.is_absolute():
        return raw
    return (PROJECT_ROOT / raw).resolve()


def ensure_config_file() -> None:
    if CONFIG_PATH.exists():
        return
    if CONFIG_TEMPLATE_PATH.exists():
        shutil.copyfile(CONFIG_TEMPLATE_PATH, CONFIG_PATH)


_TOML_SECTION_RE = re.compile(r"^\s*\[([^\[\]]+)]\s*(?:#.*)?$")
_TOML_KEY_RE = re.compile(r"^\s*([A-Za-z0-9_-]+)\s*=")


def _nested_toml_value(data: dict[str, Any], section: str) -> dict[str, Any] | None:
    value: Any = data
    for part in section.split(".") if section else ():
        if not isinstance(value, dict):
            return None
        value = value.get(part)
    return value if isinstance(value, dict) else None


def _template_key_blocks(content: str) -> list[tuple[str, str, str]]:
    """Return (section, key, comment-preserving block) for simple template keys."""
    lines = content.splitlines(keepends=True)
    section = ""
    pending: list[str] = []
    blocks: list[tuple[str, str, str]] = []
    for line in lines:
        section_match = _TOML_SECTION_RE.match(line.rstrip("\r\n"))
        if section_match:
            section = section_match.group(1).strip()
            pending = []
            continue
        key_match = _TOML_KEY_RE.match(line)
        if key_match:
            comments = list(pending)
            while comments and not comments[0].lstrip().startswith("#"):
                comments.pop(0)
            blocks.append((section, key_match.group(1), "".join(comments + [line]).lstrip("\r\n")))
            pending = []
            continue
        if not line.strip() or line.lstrip().startswith("#"):
            pending.append(line)
        else:
            pending = []
    return blocks


def ensure_config_template_defaults() -> None:
    """Merge missing template keys into local TOML while preserving user text/values."""
    if not CONFIG_PATH.exists() or not CONFIG_TEMPLATE_PATH.exists():
        return
    try:
        with CONFIG_PATH.open("rb") as current_file:
            current = tomllib.load(current_file)
        template_content = CONFIG_TEMPLATE_PATH.read_text(encoding="utf-8")
        content = CONFIG_PATH.read_text(encoding="utf-8")
    except (OSError, ValueError, tomllib.TOMLDecodeError):
        return

    missing: dict[str, list[str]] = {}
    for section, key, block in _template_key_blocks(template_content):
        current_section = _nested_toml_value(current, section)
        if current_section is None or key not in current_section:
            missing.setdefault(section, []).append(block)
    if not missing:
        return

    updated = content
    for section, blocks in missing.items():
        addition = "\n".join(block.rstrip("\r\n") for block in blocks) + "\n"
        header_re = re.compile(rf"(?m)^\s*\[{re.escape(section)}]\s*(?:#.*)?$") if section else None
        section_match = header_re.search(updated) if header_re else None
        if section_match:
            next_header = re.search(r"(?m)^\s*\[", updated[section_match.end() :])
            insert_at = section_match.end() + (next_header.start() if next_header else len(updated[section_match.end() :]))
            before = updated[:insert_at].rstrip("\r\n")
            after = updated[insert_at:].lstrip("\r\n")
            updated = before + "\n" + addition + ("\n" + after if after else "")
        elif section:
            updated = updated.rstrip("\r\n") + f"\n\n[{section}]\n" + addition
        else:
            updated = addition + "\n" + updated.lstrip("\r\n")

    # Inline/dotted tables in an older config can conflict with a section from
    # the newer template (for example `bilibili = {...}` and
    # `[search.bilibili]`). Never replace a valid user config with invalid TOML;
    # model defaults still cover keys that cannot be merged textually.
    try:
        tomllib.loads(updated)
    except (ValueError, tomllib.TOMLDecodeError):
        return

    temp_path = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.defaults.tmp")
    try:
        mode = CONFIG_PATH.stat().st_mode & 0o777
        temp_path.write_text(updated, encoding="utf-8")
        temp_path.chmod(mode)
        os.replace(temp_path, CONFIG_PATH)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def ensure_persistent_jwt_secret() -> None:
    """Replace the template secret in local config with a persistent random one.

    Explicit environment secrets remain authoritative. If the config cannot be
    written, startup validation will fail closed instead of serving a public key.
    """
    if os.getenv("ORDER_SONG_JWT_SECRET") or os.getenv("ORDER_SONG_AUTH__JWT_SECRET"):
        return
    try:
        content = CONFIG_PATH.read_text(encoding="utf-8")
    except OSError:
        return
    pattern = re.compile(
        rf'^(?P<prefix>\s*jwt_secret\s*=\s*)["\']{re.escape(DEFAULT_JWT_SECRET)}["\'](?P<suffix>\s*(?:#.*)?)$',
        re.MULTILINE,
    )
    if not pattern.search(content):
        return
    generated = secrets.token_urlsafe(48)
    updated = pattern.sub(rf'\g<prefix>"{generated}"\g<suffix>', content, count=1)
    temp_path = CONFIG_PATH.with_name(f".{CONFIG_PATH.name}.tmp")
    try:
        temp_path.write_text(updated, encoding="utf-8")
        temp_path.chmod(0o600)
        os.replace(temp_path, CONFIG_PATH)
    except OSError:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass


def _set_nested(data: dict[str, Any], path: tuple[str, ...], value: Any) -> None:
    cur = data
    for key in path[:-1]:
        next_value = cur.get(key)
        if not isinstance(next_value, dict):
            next_value = {}
            cur[key] = next_value
        cur = next_value
    cur[path[-1]] = value


def _coerce_env_value(value: str) -> Any:
    lowered = value.strip().lower()
    if lowered in {"true", "false"}:
        return lowered == "true"
    try:
        return int(value)
    except ValueError:
        pass
    try:
        return float(value)
    except ValueError:
        return value


def _apply_env_overrides(data: dict[str, Any]) -> None:
    aliases: dict[str, tuple[str, ...]] = {
        "APP_NAME": ("app_name",),
        "JWT_SECRET": ("auth", "jwt_secret"),
        "JWT_ALGORITHM": ("auth", "jwt_algorithm"),
        "JWT_EXP_MINUTES": ("auth", "jwt_exp_minutes"),
        "SQLITE_PATH": ("database", "sqlite_path"),
        "CORS_ALLOW_ORIGINS": ("server", "cors_allow_origins"),
    }
    string_paths = {
        ("app_name",),
        ("auth", "jwt_secret"),
        ("auth", "jwt_algorithm"),
        ("database", "sqlite_path"),
        ("server", "cors_allow_origins"),
    }

    prefix = "ORDER_SONG_"
    for key, value in os.environ.items():
        if not key.startswith(prefix):
            continue
        suffix = key[len(prefix) :]
        path = aliases.get(suffix)
        if path is None and "__" in suffix:
            path = tuple(part.lower() for part in suffix.split("__") if part)
        if path is None:
            continue
        parsed = value if path in string_paths else _coerce_env_value(value)
        _set_nested(data, path, parsed)


def load_settings() -> Settings:
    ensure_config_file()
    ensure_config_template_defaults()
    ensure_persistent_jwt_secret()
    data: dict[str, Any] = {}
    if CONFIG_PATH.exists():
        with CONFIG_PATH.open("rb") as f:
            data = tomllib.load(f)
    _apply_env_overrides(data)
    return Settings.model_validate(data)


def validate_auth_configuration(config: Settings) -> None:
    """Refuse to serve tokens signed by a public or empty placeholder secret."""
    secret = config.jwt_secret.strip()
    if secret.lower() in _INSECURE_JWT_SECRETS or len(secret.encode("utf-8")) < 32:
        raise RuntimeError(
            "Refusing to start with an insecure JWT secret. Set at least 32 UTF-8 bytes "
            "in auth.jwt_secret or ORDER_SONG_JWT_SECRET."
        )
    if config.jwt_algorithm != "HS256":
        raise RuntimeError("Unsupported JWT algorithm. auth.jwt_algorithm must be HS256.")


settings = load_settings()
