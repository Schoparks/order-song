from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
import time
from contextlib import suppress
from dataclasses import asdict
from pathlib import Path
from typing import Any, Awaitable, Callable, TypeVar
from urllib.parse import parse_qs, urljoin, urlparse

from app.core.config import resolve_project_path, settings
from app.qq_music_qr import terminal_qr_from_png, validate_qr_png


logger = logging.getLogger(__name__)
T = TypeVar("T")
_EXPECTED_URL_UNSET = object()
_QQ_AUDIO_CDN = "https://isure.stream.qqmusic.qq.com/"
_QQ_AUDIO_HOST = "isure.stream.qqmusic.qq.com"


class QQMusicUnavailable(RuntimeError):
    pass


class QQMusicService:
    def __init__(self) -> None:
        self._client: Any | None = None
        self._credential_type: Any | None = None
        self._qr_session_type: Any | None = None
        self._qr_login_type: Any | None = None
        self._song_file_info_type: Any | None = None
        self._song_file_type: Any | None = None
        self._device_manager_type: Any | None = None
        self._credential_errors: tuple[type[BaseException], ...] = ()
        self._api_lock = asyncio.Lock()
        self._login_task: asyncio.Task[None] | None = None
        self._refresh_task: asyncio.Task[None] | None = None
        self._audio_urls: dict[str, tuple[str, float]] = {}
        self._audio_url_tasks: dict[str, asyncio.Task[str | None]] = {}

    @property
    def enabled(self) -> bool:
        return settings.qq_music.enabled

    @property
    def logged_in(self) -> bool:
        credential = getattr(self._client, "credential", None)
        return bool(credential and credential.musicid and credential.musickey)

    @property
    def _session_path(self) -> Path:
        return resolve_project_path(settings.qq_music.session_path)

    @property
    def _device_path(self) -> Path:
        return resolve_project_path(settings.qq_music.device_path)

    @property
    def _qr_path(self) -> Path:
        return resolve_project_path(settings.qq_music.qr_path)

    def _load_dependency(self) -> None:
        try:
            from qqmusic_api import (
                Client,
                Credential,
                CredentialExpiredError,
                CredentialInvalidError,
                CredentialRefreshError,
                LoginAuthExpiredError,
            )
            from qqmusic_api.models.login import QRLoginType
            from qqmusic_api.modules.login_utils import QRCodeLoginSession
            from qqmusic_api.modules.song import SongFileInfo, SongFileType
            from qqmusic_api.utils.device import DeviceManager
        except ImportError as exc:
            raise RuntimeError(
                "已启用 qq_music.enabled，但缺少 qqmusic-api-python/Pillow；请重新安装 backend/requirements.txt"
            ) from exc

        self._credential_type = Credential
        self._qr_session_type = QRCodeLoginSession
        self._qr_login_type = QRLoginType
        self._song_file_info_type = SongFileInfo
        self._song_file_type = SongFileType
        self._device_manager_type = DeviceManager
        self._credential_errors = (
            CredentialExpiredError,
            CredentialInvalidError,
            CredentialRefreshError,
            LoginAuthExpiredError,
        )
        credential = self._load_credential()
        self._device_path.parent.mkdir(parents=True, exist_ok=True)
        # Keep the library device state in memory and persist it atomically below.
        self._client = Client(credential=credential, device_path=None)

    async def _restore_device(self) -> None:
        if not self._device_path.exists():
            return
        try:
            device = await self._device_manager_type._load_device(self._device_path)
            self._client._device_store.device = device
        except (OSError, ValueError, TypeError, KeyError):
            logger.warning("QQ 音乐设备文件无法读取，将使用新的本地设备身份", exc_info=True)

    @staticmethod
    def _atomic_json_write(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(raw_temp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as file:
                json.dump(payload, file, ensure_ascii=False)
            os.replace(temp_path, path)
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            with suppress(OSError):
                temp_path.unlink()
            raise

    @staticmethod
    def _atomic_bytes_write(path: Path, payload: bytes) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, raw_temp_path = tempfile.mkstemp(
            prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
        )
        temp_path = Path(raw_temp_path)
        try:
            with os.fdopen(fd, "wb") as file:
                file.write(payload)
            os.replace(temp_path, path)
        except BaseException:
            with suppress(OSError):
                os.close(fd)
            with suppress(OSError):
                temp_path.unlink()
            raise

    def _save_qr_png(self, payload: bytes) -> Path | None:
        try:
            validate_qr_png(payload)
            self._atomic_bytes_write(self._qr_path, payload)
            return self._qr_path
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.warning("QQ 登录二维码 PNG 无法保存到本机", exc_info=True)
            return None

    def _clear_qr_png(self) -> None:
        with suppress(OSError):
            self._qr_path.unlink()

    def _load_credential(self) -> Any:
        assert self._credential_type is not None
        try:
            data = json.loads(self._session_path.read_text(encoding="utf-8"))
            return self._credential_type.model_validate(data)
        except (OSError, ValueError, TypeError):
            return self._credential_type()

    def _save_credential(self) -> None:
        if not self.logged_in:
            return
        payload = self._client.credential.model_dump(mode="json", by_alias=True)
        try:
            self._atomic_json_write(self._session_path, payload)
        except (OSError, TypeError, ValueError):
            logger.warning("QQ 音乐登录态已生效，但无法保存到本机", exc_info=True)

    async def _save_device(self) -> None:
        store = getattr(self._client, "_device_store", None)
        device = getattr(store, "device", None)
        if device is None:
            return
        try:
            await asyncio.to_thread(self._atomic_json_write, self._device_path, asdict(device))
        except (OSError, TypeError, ValueError):
            logger.warning("QQ 音乐设备身份无法保存到本机", exc_info=True)

    def _clear_credential(self, *, schedule_login: bool = True) -> None:
        if self._client is not None and self._credential_type is not None:
            self._client.credential = self._credential_type()
        self._audio_urls.clear()
        current_task = asyncio.current_task()
        for task in self._audio_url_tasks.values():
            if task is not current_task:
                task.cancel()
        self._audio_url_tasks.clear()
        with suppress(OSError):
            self._session_path.unlink()
        if schedule_login:
            self._ensure_login_task()

    def _ensure_login_task(self, first_session: Any | None = None) -> None:
        if not self.enabled or self._client is None or self.logged_in:
            return
        if self._login_task is not None and not self._login_task.done():
            return
        self._login_task = asyncio.create_task(
            self._login_loop(first_session), name="qq-music-qr-login"
        )

    async def _bounded(self, operation: Awaitable[T]) -> T:
        async with asyncio.timeout(settings.qq_music.request_timeout_s):
            return await operation

    def _remember_audio_url(self, source_track_id: str, url: str, expiration_s: int) -> None:
        ttl = max(1.0, min(float(expiration_s or 300), 86400.0) - 60.0)
        self._audio_urls[source_track_id] = (url, time.monotonic() + ttl)

    def current_audio_url(
        self,
        source_track_id: str,
        expected_url: str | None | object = _EXPECTED_URL_UNSET,
    ) -> str | None:
        if not self.enabled or not self.logged_in:
            return None
        cached = self._audio_urls.get(source_track_id)
        if cached is None:
            return None
        url, expires_at = cached
        if time.monotonic() >= expires_at:
            self._audio_urls.pop(source_track_id, None)
            return None
        if expected_url is not _EXPECTED_URL_UNSET and expected_url != url:
            return None
        return url

    async def start(self) -> None:
        if not self.enabled:
            return
        self._load_dependency()
        await self._restore_device()
        if self.logged_in:
            try:
                expired = await self._bounded(self._client.login.check_expired())
                if expired:
                    await self._refresh_credential()
            except self._credential_errors:
                logger.warning("QQ 音乐登录态恢复失败，将重新扫码登录", exc_info=True)
                self._clear_credential(schedule_login=False)
            except Exception:
                logger.warning("暂时无法验证 QQ 音乐登录态，将保留本地会话并在请求时重试", exc_info=True)

        if not self.logged_in:
            first_session = None
            try:
                first_session = await self._create_qr_session()
            except Exception:
                logger.warning("暂时无法获取 QQ 音乐登录二维码，将在后台重试", exc_info=True)
            self._ensure_login_task(first_session)
        else:
            print("QQ 音乐：登录态已恢复，可用", flush=True)
        self._refresh_task = asyncio.create_task(
            self._refresh_loop(), name="qq-music-credential-refresh"
        )

    async def close(self) -> None:
        tasks = [task for task in (self._login_task, self._refresh_task) if task]
        tasks.extend(self._audio_url_tasks.values())
        for task in tasks:
            task.cancel()
        for task in tasks:
            with suppress(asyncio.CancelledError):
                await task
        try:
            if self._client is not None:
                await self._client.close()
        finally:
            self._clear_qr_png()
            self._client = None
            self._login_task = None
            self._refresh_task = None
            self._audio_urls.clear()
            self._audio_url_tasks.clear()

    async def _create_qr_session(self) -> Any:
        session = self._qr_session_type(
            self._client.login,
            self._qr_login_type.QQ,
            timeout_seconds=settings.qq_music.login_timeout_s,
        )
        qr = await self._bounded(session.get_qrcode())
        await self._save_device()
        self._clear_qr_png()
        png_path = self._save_qr_png(qr.data)
        rendered: str | None = None
        try:
            rendered = terminal_qr_from_png(qr.data)
        except (RuntimeError, TypeError, ValueError):
            logger.warning("QQ 登录二维码无法在当前终端渲染", exc_info=True)

        print("\nQQ 音乐解析已开启，请使用手机 QQ 扫码登录。", flush=True)
        if rendered:
            print("可先尝试扫描下方终端二维码：", flush=True)
            print(rendered, flush=True)
        if png_path is not None:
            print(f"可靠备用：请直接打开上游原始二维码 PNG：{png_path.resolve()}", flush=True)
        else:
            print("原始二维码 PNG 保存失败，请查看上方日志。", flush=True)
        print("扫码并在手机上确认后，登录态会自动保存和续期。\n", flush=True)
        return session

    async def _login_loop(self, session: Any | None) -> None:
        while self.enabled and not self.logged_in:
            try:
                if session is None:
                    session = await self._create_qr_session()
                credential = await session.wait_qrcode_login()
                async with self._api_lock:
                    self._client.credential = credential
                    self._save_credential()
                self._clear_qr_png()
                print("QQ 音乐登录成功。", flush=True)
                return
            except asyncio.CancelledError:
                self._clear_qr_png()
                raise
            except Exception:
                self._clear_qr_png()
                logger.warning("QQ 音乐二维码已失效或登录失败，正在生成新二维码")
                await asyncio.sleep(3)
                session = None

    async def _refresh_credential(self) -> None:
        credential = await self._bounded(self._client.login.refresh_credential())
        self._client.credential = credential
        self._save_credential()
        await self._save_device()

    async def _refresh_loop(self) -> None:
        while True:
            await asyncio.sleep(settings.qq_music.refresh_interval_s)
            if not self.logged_in:
                continue
            try:
                async with self._api_lock:
                    expired = self._client.credential.is_expired()
                    if not expired:
                        expired = await self._bounded(self._client.login.check_expired())
                    if expired:
                        await self._refresh_credential()
            except asyncio.CancelledError:
                raise
            except self._credential_errors:
                logger.warning("QQ 音乐登录态已失效，请在服务端控制台重新扫码", exc_info=True)
                self._clear_credential()
            except Exception:
                logger.warning("QQ 音乐登录态续期失败，下次请求会再次尝试", exc_info=True)

    def _require_ready(self) -> None:
        if not self.enabled:
            raise QQMusicUnavailable("QQ 音乐解析未开启")
        if self._client is None or not self.logged_in:
            raise QQMusicUnavailable("QQ 音乐尚未登录，请在服务端控制台扫码")

    async def _authenticated(self, operation: Callable[[], Awaitable[T]]) -> T:
        self._require_ready()
        failed_key = self._client.credential.musickey
        need_login = False
        try:
            try:
                return await self._bounded(operation())
            except self._credential_errors:
                async with self._api_lock:
                    if not self.logged_in:
                        need_login = True
                        raise QQMusicUnavailable(
                            "QQ 音乐登录态已失效，请在服务端控制台重新扫码"
                        )
                    if self._client.credential.musickey == failed_key:
                        try:
                            await self._refresh_credential()
                        except Exception as exc:
                            self._clear_credential(schedule_login=False)
                            need_login = True
                            raise QQMusicUnavailable(
                                "QQ 音乐登录态已失效，请在服务端控制台重新扫码"
                            ) from exc
                retry_key = self._client.credential.musickey
                try:
                    return await self._bounded(operation())
                except self._credential_errors as exc:
                    async with self._api_lock:
                        if self._client.credential.musickey == retry_key:
                            self._clear_credential(schedule_login=False)
                            need_login = True
                    raise QQMusicUnavailable(
                        "QQ 音乐登录态已失效，请在服务端控制台重新扫码"
                    ) from exc
        finally:
            await self._save_device()
            if need_login:
                self._ensure_login_task()

    def _file_info(self, song: Any) -> Any | None:
        file_type = None
        if song.file.size_128mp3 > 0:
            file_type = self._song_file_type.MP3_128
        elif song.file.size_96aac > 0:
            file_type = self._song_file_type.ACC_96
        elif song.file.size_48aac > 0:
            file_type = self._song_file_type.ACC_48
        if file_type is None:
            return None
        return self._song_file_info_type(song.mid, file_type, song.type, song.file.media_mid)

    def _is_free_candidate(self, song: Any) -> bool:
        pay = song.pay
        # pay_month is not a standard-playback entitlement flag: current QQ
        # responses can set it to 1 while pay_play is 0 and return the complete
        # requested M500 file. vi/try offsets are likewise present on free full
        # files, so actual trial rejection happens by requiring the exact
        # requested standard filename below and rejecting RS01/RS02 URLs.
        return bool(
            song.mid
            and song.status == 0
            and song.interval > 0
            and song.interval < settings.search.max_duration_s
            and pay.pay_play == 0
            and pay.time_free == 0
            and self._file_info(song) is not None
        )

    @staticmethod
    def _is_full_standard_url(item: Any, url: str) -> bool:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        try:
            port = parsed.port
        except ValueError:
            return False
        if (
            parsed.scheme != "https"
            or parsed.username is not None
            or parsed.password is not None
            or port not in (None, 443)
            or host != _QQ_AUDIO_HOST
        ):
            return False
        query = parse_qs(parsed.query)
        path_name = parsed.path.removeprefix("/")
        return bool(
            item.vkey
            and path_name == item.filename
            and item.vkey in query.get("vkey", [])
            and not path_name.upper().startswith(("RS01", "RS02"))
        )

    async def _playable_urls(self, songs: list[Any]) -> dict[str, str]:
        candidates = [song for song in songs if self._is_free_candidate(song)]
        urls: dict[str, str] = {}
        for offset in range(0, len(candidates), 100):
            batch = candidates[offset : offset + 100]
            file_info = [item for song in batch if (item := self._file_info(song)) is not None]
            if not file_info:
                continue
            response = await self._client.song.get_song_urls(file_info)
            for item in response.data:
                if item.result != 0 or not item.purl:
                    continue
                url = item.purl if urlparse(item.purl).scheme == "https" else urljoin(_QQ_AUDIO_CDN, item.purl)
                if self._is_full_standard_url(item, url):
                    urls[item.mid] = url
                    self._remember_audio_url(item.mid, url, response.expiration)
        return urls

    @staticmethod
    def _track_payload(song: Any) -> dict[str, Any]:
        return {
            "source_track_id": song.mid,
            "title": song.title or song.name or song.mid,
            "artist": " / ".join(singer.name for singer in song.singer if singer.name) or None,
            "duration_ms": song.interval * 1000,
            "cover_url": song.cover_url(300) or None,
        }

    async def search_tracks(self, keyword: str) -> list[dict[str, Any]]:
        async def operation() -> list[dict[str, Any]]:
            response = await self._client.search.search_by_type(
                keyword, num=settings.qq_music.search_limit * 2, highlight=False
            )
            playable = await self._playable_urls(response.song)
            return [
                self._track_payload(song)
                for song in response.song
                if song.mid in playable
            ][: settings.qq_music.search_limit]

        return await self._authenticated(operation)

    async def playlist_tracks(self, playlist_id: int) -> dict[str, Any]:
        async def operation() -> dict[str, Any]:
            request = self._client.songlist.get_detail(
                playlist_id, num=settings.qq_music.playlist_page_size, page=1
            )
            songs: list[Any] = []
            title = f"QQ 音乐歌单 {playlist_id}"
            total = 0
            async for page in request.paginate():
                if not songs:
                    title = page.info.title or title
                    total = page.total
                songs.extend(page.songs)
            playable = await self._playable_urls(songs)
            tracks = [self._track_payload(song) for song in songs if song.mid in playable]
            return {"name": title, "tracks": tracks, "total": total or len(songs)}

        return await self._authenticated(operation)

    async def resolve_audio_url(self, source_track_id: str, *, force: bool = False) -> str | None:
        existing = self._audio_url_tasks.get(source_track_id)
        if existing and not existing.done():
            return await asyncio.shield(existing)

        if force:
            self._audio_urls.pop(source_track_id, None)
        else:
            cached = self.current_audio_url(source_track_id)
            if cached:
                return cached

        async def operation() -> str | None:
            response = await self._client.song.query_song([source_track_id])
            if not response.tracks:
                return None
            urls = await self._playable_urls(response.tracks[:1])
            return urls.get(source_track_id)

        task = asyncio.create_task(
            self._authenticated(operation),
            name=f"qq-audio-url-{source_track_id}",
        )
        self._audio_url_tasks[source_track_id] = task

        def discard(completed: asyncio.Task[str | None]) -> None:
            if self._audio_url_tasks.get(source_track_id) is completed:
                self._audio_url_tasks.pop(source_track_id, None)

        task.add_done_callback(discard)
        return await asyncio.shield(task)


qq_music = QQMusicService()
