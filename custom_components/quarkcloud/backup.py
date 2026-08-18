"""Backup agent for the Quark Cloud Drive integration."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable, Coroutine
import json
import logging
import os
import tempfile
import time
from typing import Any

import aiohttp

from homeassistant.components.backup import (
    AgentBackup,
    BackupAgent,
    BackupAgentError,
    BackupNotFound,
    OnProgressCallback,
    suggested_filename,
)
from homeassistant.core import HomeAssistant, callback

from . import QuarkCloudConfigEntry
from .api import QuarkApiError, QuarkCloudApi
from .const import (
    BACKUP_DIR_NAME,
    BACKUP_FILE_PREFIX,
    DATA_BACKUP_AGENT_LISTENERS,
    DELETED_BACKUP_IDS,
    DOMAIN,
    TRASH_DIR_NAME,
)

_LOGGER = logging.getLogger(__name__)

CACHE_TTL = 120


def _filenames(backup: AgentBackup) -> tuple[str, str]:
    """Return the (tar, metadata) filenames used on the cloud drive."""
    base = suggested_filename(backup).rsplit(".", 1)[0]
    return (
        f"{BACKUP_FILE_PREFIX}{base}.tar",
        f"{BACKUP_FILE_PREFIX}{base}.metadata.json",
    )


async def async_get_backup_agents(hass: HomeAssistant) -> list[BackupAgent]:
    """Return a list of backup agents."""
    entries: list[QuarkCloudConfigEntry] = (
        hass.config_entries.async_loaded_entries(DOMAIN)
    )
    return [QuarkCloudBackupAgent(hass, entry) for entry in entries]


@callback
def async_register_backup_agents_listener(
    hass: HomeAssistant,
    *,
    listener: Callable[[], None],
) -> Callable[[], None]:
    """Register a listener to be called when agents are added/removed."""
    hass.data.setdefault(DATA_BACKUP_AGENT_LISTENERS, []).append(listener)

    @callback
    def remove_listener() -> None:
        hass.data[DATA_BACKUP_AGENT_LISTENERS].remove(listener)
        if not hass.data[DATA_BACKUP_AGENT_LISTENERS]:
            del hass.data[DATA_BACKUP_AGENT_LISTENERS]

    return remove_listener


class QuarkCloudBackupAgent(BackupAgent):
    """Quark Cloud Drive backup agent."""

    domain = DOMAIN

    def __init__(
        self, hass: HomeAssistant, entry: QuarkCloudConfigEntry
    ) -> None:
        self.hass = hass
        self.entry = entry
        self._api: QuarkCloudApi = entry.runtime_data
        self.name = entry.title
        self.unique_id = entry.entry_id
        self._backup_cache: dict[str, AgentBackup] = {}
        self._cache_expiration = 0.0

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _deleted_ids(self) -> set[str]:
        return set(self.entry.data.get(DELETED_BACKUP_IDS, []))

    @callback
    def _tombstone(self, backup_id: str) -> None:
        self.hass.config_entries.async_update_entry(
            self.entry,
            data={
                **self.entry.data,
                DELETED_BACKUP_IDS: sorted(self._deleted_ids() | {backup_id}),
            },
        )

    async def _find_dir(self, name: str) -> str | None:
        """Find a folder by name at the drive root, return its fid."""
        result = await self._api.search_files(name, size=20, category=0)
        for item in result.get("file_list") or []:
            if item.get("filename") == name and str(item.get("category")) == "0":
                return item.get("fid")
        return None

    async def _ensure_dir(self, name: str) -> str:
        fid = await self._find_dir(name)
        if fid:
            return fid
        try:
            # Root creation: no pdir_fid (CLI parity).
            result = await self._api.create_folder(name)
            return result.get("fid") or ""
        except QuarkApiError as err:
            _LOGGER.warning("Could not create folder %s: %s", name, err)
            # Retry search: it may already exist (race).
            return await self._find_dir(name) or ""

    async def _upload_local_file(
        self,
        file_path: str,
        filename: str,
        pdir_fid: str,
        content_type: str = "application/octet-stream",
        on_progress: Callable[[int], None] | None = None,
    ) -> None:
        size = await self.hass.async_add_executor_job(
            os.path.getsize, file_path
        )

        def read_at(start: int, end: int) -> bytes:
            with open(file_path, "rb") as f:
                f.seek(start)
                return f.read(max(0, end - start))

        await self._api.upload_file(
            file_path,
            filename,
            pdir_fid,
            size,
            read_at,
            content_type=content_type,
            on_progress=on_progress,
        )

    async def _search_backup_files(self) -> list[dict[str, Any]]:
        result = await self._api.search_files(BACKUP_FILE_PREFIX, size=100)
        return result.get("file_list") or []

    # ------------------------------------------------------------------
    # BackupAgent API
    # ------------------------------------------------------------------

    async def async_upload_backup(
        self,
        *,
        open_stream: Callable[[], Coroutine[Any, Any, AsyncIterator[bytes]]],
        backup: AgentBackup,
        on_progress: OnProgressCallback,
        **kwargs: Any,
    ) -> None:
        """Upload a backup: spool to disk, hash, upload tar + metadata."""
        tar_name, meta_name = _filenames(backup)
        parent_fid = await self._ensure_dir(BACKUP_DIR_NAME)
        tmp_dir = tempfile.gettempdir()
        tar_path = os.path.join(tmp_dir, f"quark_{tar_name}")
        meta_path = os.path.join(tmp_dir, f"quark_{meta_name}")
        try:
            stream = await open_stream()
            size = 0
            tar_file = await self.hass.async_add_executor_job(
                open, tar_path, "wb"
            )
            try:
                async for chunk in stream:
                    await self.hass.async_add_executor_job(tar_file.write, chunk)
                    size += len(chunk)
                    # No on_progress here: spooling is local disk IO, not an
                    # upload. Reporting it would make the UI show 100%
                    # instantly while hashing/uploading is still running.
            finally:
                await self.hass.async_add_executor_job(tar_file.close)

            # Real upload progress starts once bytes go over the network
            # (reported per part from _upload_local_file).
            await self._upload_local_file(
                tar_path, tar_name, parent_fid, on_progress=on_progress
            )

            def write_meta() -> None:
                with open(meta_path, "wb") as f:
                    f.write(json.dumps(backup.as_dict()).encode())

            await self.hass.async_add_executor_job(write_meta)
            await self._upload_local_file(meta_path, meta_name, parent_fid)
        except QuarkApiError as err:
            if "capacity limit" in str(err) or "32003" in str(err):
                # Drive is full (errno 32003): dedicated friendly message.
                raise BackupAgentError(
                    translation_domain=DOMAIN,
                    translation_key="upload_capacity_limit",
                    translation_placeholders={
                        "size_mb": f"{backup.size / 1048576:.0f}",
                    },
                ) from err
            raise BackupAgentError(
                translation_domain=DOMAIN,
                translation_key="upload_failed",
                translation_placeholders={"error": str(err)},
            ) from err
        finally:
            for path in (tar_path, meta_path):
                try:
                    await self.hass.async_add_executor_job(os.remove, path)
                except OSError:
                    pass
        # New upload may replace a tombstoned backup with the same id.
        if backup.backup_id in self._deleted_ids():
            self.hass.config_entries.async_update_entry(
                self.entry,
                data={
                    **self.entry.data,
                    DELETED_BACKUP_IDS: sorted(
                        self._deleted_ids() - {backup.backup_id}
                    ),
                },
            )
        self._cache_expiration = 0.0

    async def async_download_backup(
        self,
        backup_id: str,
        **kwargs: Any,
    ) -> AsyncIterator[bytes]:
        """Download a backup file (returns the byte stream, onedrive-style)."""
        backup = await self._find_backup_by_id(backup_id)
        tar_name, _ = _filenames(backup)
        target = None
        for item in await self._search_backup_files():
            if item.get("filename") == tar_name:
                target = item
                break
        if not target or not target.get("fid"):
            raise BackupNotFound(f"Backup {backup_id} file not found on drive")
        try:
            info = await self._api.get_download_url(target["fid"])
        except QuarkApiError as err:
            if "size limit" in str(err) or "23018" in str(err):
                raise BackupAgentError(
                    translation_domain=DOMAIN,
                    translation_key="download_size_limit",
                    translation_placeholders={
                        "size_mb": f"{backup.size / 1048576:.0f}",
                        "limit_mb": "50",
                    },
                ) from err
            raise BackupAgentError(f"Failed to get download url: {err}") from err
        urls = info.get("download_url") or info.get("download_urls") or []
        if isinstance(urls, str):
            urls = [urls]
        if not urls:
            raise BackupAgentError(
                translation_domain=DOMAIN,
                translation_key="download_no_url",
            )
        return self._stream_download(urls)

    def _stream_download(self, urls: list[str]) -> AsyncIterator[bytes]:
        """Async generator: GET the download urls and yield the tar bytes."""

        async def _stream() -> AsyncIterator[bytes]:
            session = aiohttp.ClientSession()
            try:
                for url in urls:
                    # CLI parity: file download GETs carry the pan cookie.
                    resp = await session.get(
                        url,
                        headers={
                            "Cookie": self._api.download_cookie_header()
                        },
                    )
                    if resp.status != 200:
                        continue
                    async for chunk in resp.content.iter_chunked(65536):
                        yield chunk
                    return
                raise BackupAgentError(
                    translation_domain=DOMAIN,
                    translation_key="download_all_urls_failed",
                )
            finally:
                await session.close()

        return _stream()

    async def async_delete_backup(self, backup_id: str, **kwargs: Any) -> None:
        """Delete a backup.

        Primary: ``/open/v1/file/delete`` (real deletion, validated by the
        quark-drive-ext project against the same open API). Fallback: move
        to a trash folder + tombstone (official CLI has no delete API).
        """
        backup = await self._find_backup_by_id(backup_id)
        tar_name, meta_name = _filenames(backup)
        fids = [
            item["fid"]
            for item in await self._search_backup_files()
            if item.get("filename") in (tar_name, meta_name) and item.get("fid")
        ]
        if fids:
            try:
                await self._api.delete_files(fids)
                self._tombstone(backup_id)
            except QuarkApiError as err:
                _LOGGER.warning(
                    "file/delete failed (%s); falling back to trash move", err
                )
                try:
                    trash_fid = await self._ensure_dir(TRASH_DIR_NAME)
                    await self._api.move_files(fids, trash_fid)
                    self._tombstone(backup_id)
                except QuarkApiError as move_err:
                    raise BackupAgentError(
                        translation_domain=DOMAIN,
                        translation_key="delete_failed",
                        translation_placeholders={"error": str(move_err)},
                    ) from move_err
        self._backup_cache.pop(backup_id, None)
        self._cache_expiration = 0.0

    async def async_list_backups(self, **kwargs: Any) -> list[AgentBackup]:
        """List backups by reading metadata files from the drive."""
        backups = await self._list_backups()
        return list(backups.values())

    async def async_get_backup(self, backup_id: str, **kwargs: Any) -> AgentBackup:
        """Return a backup."""
        return await self._find_backup_by_id(backup_id)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _find_backup_by_id(self, backup_id: str) -> AgentBackup:
        backups = await self._list_backups()
        if backup := backups.get(backup_id):
            return backup
        raise BackupNotFound(f"Backup {backup_id} not found")

    async def _list_backups(self) -> dict[str, AgentBackup]:
        now = time.time()
        if now <= self._cache_expiration and self._backup_cache:
            return self._backup_cache
        deleted = self._deleted_ids()
        backups: dict[str, AgentBackup] = {}
        try:
            files = await self._search_backup_files()
        except QuarkApiError as err:
            _LOGGER.warning("Failed to search backups: %s", err)
            return {}
        meta_files = [
            item
            for item in files
            if item.get("filename", "").endswith(".metadata.json")
            and item.get("fid")
        ]
        async with aiohttp.ClientSession() as session:
            for meta_file in meta_files:
                try:
                    info = await self._api.get_download_url(meta_file["fid"])
                    urls = (
                        info.get("download_url")
                        or info.get("download_urls")
                        or []
                    )
                    if isinstance(urls, str):
                        urls = [urls]
                    if not urls:
                        continue
                    content = None
                    for url in urls:
                        resp = await session.get(
                            url,
                            headers={
                                "Cookie": self._api.download_cookie_header()
                            },
                        )
                        if resp.status == 200:
                            content = await resp.read()
                            break
                    if content is None:
                        continue
                    backup = AgentBackup.from_dict(json.loads(content))
                except (QuarkApiError, ValueError, OSError) as err:
                    _LOGGER.warning(
                        "Skipping unreadable metadata %s: %s",
                        meta_file.get("filename"),
                        err,
                    )
                    continue
                if backup.backup_id not in deleted:
                    backups[backup.backup_id] = backup
        self._backup_cache = backups
        self._cache_expiration = now + CACHE_TTL
        return backups
