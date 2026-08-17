"""Pure-python port of the Quark Drive skill API client.

This module reimplements the network protocol used by the official
quarkclouddrive skill CLI (scripts/quark-drive.cjs):

- Request signing: sha256("METHOD&path&timestamp&signKey") sent via the
  ``x-pan-client-id`` / ``x-pan-tm`` / ``x-pan-token`` headers.
- OAuth flow: authorize page url -> user scans QR code -> user receives an
  ``AAC-xxxx`` agent auth code -> exchanged for access/refresh tokens.
- Token rotation via /agent/v1/oauth/access_token/rotate.
- File upload with sha1/md5 hashes and v1 proof codes.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
import hashlib
import logging
import struct
import time
from typing import Any
import uuid
from urllib.parse import quote

import aiohttp
from aiohttp import ClientTimeout
import yarl

from .const import (
    AGENT_ID,
    API_BASE_URL,
    CLIENT_ID,
    DEFAULT_DEVICE_ID,
    PATH_AGENT_AUTH_CODE,
    PATH_CREATE_DIR,
    PATH_FILE_MOVE,
    PATH_FILE_DELETE,
    PATH_FILE_SEARCH,
    PATH_GET_AUTHORIZE_PAGE_URL,
    PATH_GET_DOWNLOAD_URL,
    PATH_GET_UPLOAD_URLS,
    PATH_TOKEN_ROTATE,
    PATH_UPLOAD_FINISH,
    PATH_UPLOAD_PRE,
    PATH_USER_INFO,
    PATH_VIP_INFO,
    PATH_UPDATE_HASH,
    REQUEST_TIMEOUT,
    SIGN_KEY,
    UPLOAD_TIMEOUT,
    FORM_UPLOAD_SIZE_LIMIT,
    MAX_CONCURRENT_PART_UPLOADS,
)

_LOGGER = logging.getLogger(__name__)


class QuarkAuthError(Exception):
    """Raised when authentication fails and cannot be refreshed."""


class QuarkApiError(Exception):
    """Raised when the Quark API returns an error."""


class QuarkAuthCodeExpired(QuarkApiError):
    """The AAC auth code has expired (single-use / short-lived)."""


class QuarkAlreadyAuthorized(QuarkApiError):
    """The account is already bound to this device (second confirmation)."""


def _md5hex(text: str) -> str:
    return hashlib.md5(text.encode()).hexdigest()


_SHA1_INIT = (0x67452301, 0xEFCDAB89, 0x98BADCFE, 0x10325476, 0xC3D2E1F0)

_unpack16 = struct.Struct(">16I").unpack_from
_M32 = 0xFFFFFFFF


def _sha1_blocks(h: list[int], data: bytes) -> list[int]:
    """Compress 64-byte aligned data through SHA1 (no padding applied).

    Optimized pure-python core (struct batch unpack + tight loops) used
    only to export the running state for ``parallel_sha1_ctx``; the file
    digests themselves use hashlib (C speed).
    """
    h0, h1, h2, h3, h4 = h
    w = [0] * 80
    for off in range(0, len(data), 64):
        w[0:16] = _unpack16(data, off)
        for i in range(16, 80):
            x = w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16]
            w[i] = ((x << 1) & _M32) | (x >> 31)
        a, b, c, d, e = h0, h1, h2, h3, h4
        for i in range(80):
            if i < 20:
                f = (b & c) | (~b & d)
                k = 0x5A827999
            elif i < 40:
                f = b ^ c ^ d
                k = 0x6ED9EBA1
            elif i < 60:
                f = (b & c) | (b & d) | (c & d)
                k = 0x8F1BBCDC
            else:
                f = b ^ c ^ d
                k = 0xCA62C1D6
            temp = (
                (((a << 5) & _M32) | (a >> 27)) + (f & _M32) + e + k + w[i]
            ) & _M32
            e = d
            d = c
            c = ((b << 30) & _M32) | (b >> 2)
            b = a
            a = temp
        h0 = (h0 + a) & _M32
        h1 = (h1 + b) & _M32
        h2 = (h2 + c) & _M32
        h3 = (h3 + d) & _M32
        h4 = (h4 + e) & _M32
    return [h0, h1, h2, h3, h4]


def _sha1_context_state(data: bytes) -> list[int]:
    """Return the running SHA1 state (h0..h4) after all complete blocks.

    Mirrors the skill CLI's ``exportSha1State`` (used for
    ``parallel_sha1_ctx``); no finalization/padding is applied.
    """
    full_blocks = len(data) - (len(data) % 64)
    return _sha1_blocks(list(_SHA1_INIT), data[:full_blocks])


def _hashes_and_part_states(
    read_at: Callable[[int, int], bytes],
    file_size: int,
    part_size: int,
) -> tuple[str, str, dict[int, list[int]]]:
    """Single pass over the file: hashlib sha1/md5 + SHA1 part-boundary states.

    One full read replaces the CLI's pipelined hash: returns
    ``(sha1hex, md5hex, {k: state_after_part_k})`` for k = 1..n_parts-1.
    """
    sha1 = hashlib.sha1()
    md5 = hashlib.md5()
    h = list(_SHA1_INIT)
    states: dict[int, list[int]] = {}
    leftover = b""
    n_parts = (file_size + part_size - 1) // part_size
    for k in range(1, n_parts + 1):
        end = min(k * part_size, file_size)
        chunk = read_at((k - 1) * part_size, end)
        sha1.update(chunk)
        md5.update(chunk)
        buf = leftover + chunk
        full_blocks = len(buf) - (len(buf) % 64)
        h = _sha1_blocks(h, buf[:full_blocks])
        leftover = buf[full_blocks:]
        if k < n_parts:
            states[k] = list(h)
    return sha1.hexdigest(), md5.hexdigest(), states


class QuarkCloudApi:
    """Quark Cloud Drive open API client."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        access_token: str = "",
        refresh_token: str = "",
        device_id: str = DEFAULT_DEVICE_ID,
        user_id: str = "",
        on_tokens_updated: Callable[[str, str, str, str], None] | None = None,
    ) -> None:
        self._session = session
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._device_id = device_id or DEFAULT_DEVICE_ID
        self._user_id = user_id
        self._on_tokens_updated = on_tokens_updated
        self._rotating = False

    @property
    def user_id(self) -> str:
        return self._user_id

    def set_tokens(
        self, access_token: str, refresh_token: str, user_id: str, device_id: str
    ) -> None:
        self._access_token = access_token
        if refresh_token:
            self._refresh_token = refresh_token
        if user_id:
            self._user_id = user_id
        if device_id:
            self._device_id = device_id

    def _sign_headers(self, method: str, path: str) -> tuple[dict[str, str], str]:
        """Build signature headers exactly like the skill CLI does."""
        tm = str(int(time.time() * 1000))
        raw = f"{method.upper()}&{path}&{tm}&{SIGN_KEY}"
        token = hashlib.sha256(raw.encode()).hexdigest()
        headers = {
            "x-pan-client-id": CLIENT_ID,
            "x-pan-tm": tm,
            "x-pan-token": token,
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        return headers, token

    def _build_url(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        include_auth: bool = True,
        include_device_id: bool = True,
    ) -> str:
        """Build the request URL like the CLI's ``buildUrl``.

        ``include_auth=False`` mirrors ``getUserInfo``/``getVipInfo`` which
        carry the token via the ``Authorization`` header only.
        ``include_device_id=False`` mirrors ``DownloadApi.buildUrl``.
        """
        query: dict[str, str] = {"req_id": str(uuid.uuid4())}
        if include_auth:
            query["access_token"] = self._access_token
        if include_device_id and self._device_id:
            query["device_id"] = self._device_id
        if params:
            for key, value in params.items():
                if value is not None:
                    query[str(key)] = str(value)
        quoted = "&".join(
            f"{quote(key, safe='')}={quote(value, safe='')}" for key, value in query.items()
        )
        return f"{API_BASE_URL}{path}?{quoted}"

    async def _raw_request(
        self,
        method: str,
        path: str,
        *,
        auth_free: bool = False,
        no_query_auth: bool = False,
        include_device_id: bool = True,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        if path.startswith("http"):
            url = path
        elif auth_free:
            query = {"req_id": str(uuid.uuid4())}
            for key, value in (params or {}).items():
                if value is not None:
                    query[str(key)] = str(value)
            quoted = "&".join(
                f"{quote(k, safe='')}={quote(v, safe='')}" for k, v in query.items()
            )
            url = f"{API_BASE_URL}{path}?{quoted}"
        else:
            url = self._build_url(
                path,
                params,
                include_auth=not no_query_auth,
                include_device_id=include_device_id,
            )
        headers, _ = self._sign_headers(method, path)
        # Headers injected by the skill CLI's node network adapter
        # (injectRequestHeaders): X-Agent-ID on every request to the drive
        # API host, Authorization: Bearer whenever an access token exists
        # (the CLI does this for all drive API urls, oauth ones included).
        headers["X-Agent-ID"] = AGENT_ID
        if self._access_token:
            headers["Authorization"] = f"Bearer {self._access_token}"
        if body is not None:
            _LOGGER.debug(
                "%s %s body=%s", method, path, str(body)[:3000]
            )
        async with self._session.request(
            method,
            url,
            headers=headers,
            json=body,
            timeout=ClientTimeout(total=timeout),
        ) as resp:
            data = await resp.json(content_type=None)
            if not isinstance(data, dict):
                raise QuarkApiError(f"Unexpected response from {path}")
            _LOGGER.debug(
                "%s %s -> http=%s status=%s errno=%s error_info=%s resp=%s",
                method,
                path,
                resp.status,
                data.get("status"),
                data.get("errno"),
                data.get("error_info") or data.get("agent_msg"),
                str(data)[:3000],
            )
            # The server may hand out a rotated token in a response header
            # (CLI: handleSpecialResponseHeaders).
            new_token = resp.headers.get(
                "x-new-access-token"
            ) or resp.headers.get("X-New-Access-Token")
            if new_token and new_token != self._access_token:
                _LOGGER.debug("received x-new-access-token; persisting rotation")
                self._access_token = new_token
                if self._on_tokens_updated:
                    self._on_tokens_updated(
                        self._access_token,
                        self._refresh_token,
                        self._user_id,
                        self._device_id,
                    )
            if resp.status in (401, 403):
                raise QuarkAuthError(data.get("error_info") or "unauthorized")
            return data

    async def _request(
        self,
        method: str,
        path: str,
        *,
        no_query_auth: bool = False,
        include_device_id: bool = True,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: int = REQUEST_TIMEOUT,
    ) -> dict[str, Any]:
        """Signed request with token rotation and one replay.

        CLI parity (node network adapter ``handleTokenRefreshReplay`` /
        ``pA``): errno 11000 means "not authenticated" and propagates
        immediately; the rotate+replay set is {11001, 11017, 12003, 12004}
        and each request is replayed at most once. A failing rotation is
        logged, not raised - the request is replayed with the current
        token regardless (the server keeps it valid for a grace period).
        """
        retried = False
        while True:
            try:
                data = await self._raw_request(
                    method, path, no_query_auth=no_query_auth,
                    include_device_id=include_device_id, params=params,
                    body=body, timeout=timeout,
                )
            except QuarkAuthError:
                if retried:
                    raise
                retried = True
                await self._rotate_and_notify()
                continue
            if data.get("status") == 0:
                return data.get("data") or {}
            error_info = (
                data.get("error_info")
                or data.get("agent_msg")
                or data.get("message")
                or f"status={data.get('status')}"
            )
            errno = data.get("errno")
            if errno == 11000:
                # CLI parity: handleAccessTokenNotAuth - relogin required.
                raise QuarkAuthError(error_info)
            if not retried and (
                errno in (11001, 11017, 12003, 12004)
                or self._looks_like_auth_error(error_info)
            ):
                retried = True
                await self._rotate_and_notify()
                continue
            raise QuarkApiError(error_info)

    @staticmethod
    def _looks_like_auth_error(message: str) -> bool:
        lowered = message.lower()
        return any(
            keyword in lowered
            for keyword in ("token", "unauthorized", "auth", "未授权", "认证")
        )

    async def _rotate_and_notify(self) -> None:
        """Rotate the refresh token; failures never break the caller.

        CLI parity (``handleTokenRefreshReplay``): a failing rotation is
        only logged, and the original request is replayed with the current
        token regardless - the server keeps it valid during a grace
        period ("Refresh token rotation rate limited, current access
        token still valid").
        """
        if self._rotating:
            return
        self._rotating = True
        try:
            result = await self.rotate_refresh_token(
                self._refresh_token, self._device_id
            )
            self.set_tokens(
                result["access_token"],
                result.get("refresh_token", ""),
                self._user_id,
                self._device_id,
            )
            if self._on_tokens_updated:
                self._on_tokens_updated(
                    self._access_token, self._refresh_token, self._user_id, self._device_id
                )
        except QuarkAuthError as err:
            _LOGGER.debug("Token rotation failed, replaying with current token: %s", err)
        finally:
            self._rotating = False

    # ------------------------------------------------------------------
    # OAuth
    # ------------------------------------------------------------------

    async def get_authorize_page_url(
        self,
        device_name: str = "Home Assistant",
        current_user_id: str = "",
    ) -> dict[str, str]:
        """Return the authorize page url the user opens to scan the QR code.

        CLI parity: ``is_cloud_agent``/``is_unsure_agent`` are always sent
        (stringified booleans); ``current_user_id`` only when one exists.
        """
        body: dict[str, Any] = {
            "client_device_id": self._device_id,
            "device_name": device_name,
            "agent_id": AGENT_ID,
            "client_id": CLIENT_ID,
            "work_dir": "/config",
            "is_cloud_agent": "false",
            "is_unsure_agent": "false",
        }
        if current_user_id:
            body["current_user_id"] = current_user_id
        data = await self._raw_request(
            "POST", PATH_GET_AUTHORIZE_PAGE_URL, auth_free=True, body=body
        )
        if data.get("status") != 0:
            raise QuarkApiError(data.get("error_info") or "failed to get authorize url")
        inner = data.get("data") or {}
        return {
            "authorize_page_url": inner.get("authorize_page_url", ""),
            "page_code": inner.get("page_code", ""),
            "device_id": inner.get("device_id", ""),
        }

    async def exchange_agent_auth_code(self, auth_code: str) -> dict[str, str]:
        """Exchange the AAC-xxxx code (obtained after QR scan) for tokens.

        Mirrors the skill CLI: any status other than ``expired`` /
        ``second_confirmed`` is a success as long as ``access_token`` is
        present (``confirmed`` is the normal response). In the
        ``second_confirmed`` case the account is already bound to this
        device; the CLI reuses its locally stored token, here we rotate
        the returned refresh token instead.
        """
        data = await self._raw_request(
            "GET",
            PATH_AGENT_AUTH_CODE,
            auth_free=True,
            params={"agent_auth_code": auth_code.strip()},
        )
        if data.get("status") != 0:
            raise QuarkApiError(data.get("error_info") or data.get("agent_msg") or "invalid auth code")
        inner = data.get("data") or {}
        status = inner.get("status")
        if status == "expired":
            raise QuarkAuthCodeExpired("auth code expired, scan again")
        device_id = inner.get("device_id", "") or self._device_id
        if status == "second_confirmed":
            refresh_token = inner.get("refresh_token", "")
            if not refresh_token:
                raise QuarkAlreadyAuthorized(
                    "account already authorized for this device; revoke it in the Quark app first"
                )
            rotated = await self.rotate_refresh_token(refresh_token, device_id)
            return {
                "access_token": rotated["access_token"],
                "access_token_expires_at": "",
                "refresh_token": rotated.get("refresh_token") or refresh_token,
                "refresh_token_expires_at": "",
                "device_id": device_id,
                "user_id": inner.get("user_id", ""),
            }
        access_token = inner.get("access_token")
        if not access_token:
            raise QuarkApiError(
                "auth code not confirmed or no accessToken returned; "
                "complete the scan confirmation and retry"
            )
        return {
            "access_token": access_token,
            "access_token_expires_at": str(inner.get("access_token_expires_at", "")),
            "refresh_token": inner.get("refresh_token", ""),
            "refresh_token_expires_at": str(inner.get("refresh_token_expires_at", "")),
            "device_id": device_id,
            "user_id": inner.get("user_id", ""),
        }

    async def rotate_refresh_token(
        self, refresh_token: str, device_id: str
    ) -> dict[str, str]:
        data = await self._raw_request(
            "POST",
            PATH_TOKEN_ROTATE,
            auth_free=True,
            body={"refresh_token": refresh_token, "device_id": device_id},
        )
        if data.get("status") != 0:
            raise QuarkAuthError(
                data.get("error_info") or "token rotation failed"
            )
        inner = data.get("data") or {}
        if not inner.get("access_token"):
            raise QuarkAuthError("token rotation returned no access token")
        return {
            "access_token": inner["access_token"],
            "refresh_token": inner.get("refresh_token", ""),
            "expires_in": str(inner.get("expires_in", "")),
        }

    # ------------------------------------------------------------------
    # Business APIs
    # ------------------------------------------------------------------

    async def get_user_info(self) -> dict[str, Any]:
        """GET /open/v1/user/info.

        CLI parity: query carries ``req_id``+``device_id`` only (no
        ``access_token``); the token travels in the Authorization header.
        """
        return await self._request(
            "GET",
            PATH_USER_INFO,
            no_query_auth=True,
            params={"device_id": self._device_id},
        )

    async def get_vip_info(self) -> dict[str, Any]:
        """GET /open/v1/user/get_vip_info.

        CLI parity: query carries ``req_id``+``access_token``+``device_id``
        (getVipInfo sets access_token explicitly and appends device params).
        """
        return await self._request("GET", PATH_VIP_INFO)

    async def search_files(
        self,
        keyword: str,
        size: int = 50,
        category: int | None = None,
        page: int | None = None,
    ) -> dict[str, Any]:
        """POST /agent/v1/file/search.

        CLI parity: wrapper passes ``{keyword, size||10, category, page}``;
        undefined fields (category/page) are dropped by JSON.stringify.
        """
        body: dict[str, Any] = {
            "search_type": "mix",
            "keyword": keyword,
            "size": size,
        }
        if category is not None:
            body["category"] = category
        if page is not None:
            body["page"] = page
        return await self._request("POST", PATH_FILE_SEARCH, body=body)

    async def create_folder(
        self, dir_path: str, pdir_fid: str = ""
    ) -> dict[str, Any]:
        """POST /open/v1/dir.

        CLI parity: ``pdir_fid`` is omitted for the drive root (undefined
        fields are dropped by JSON.stringify in the CLI).
        """
        body: dict[str, Any] = {"dir_path": dir_path}
        if pdir_fid:
            body["pdir_fid"] = pdir_fid
        return await self._request("POST", PATH_CREATE_DIR, body=body)

    async def move_files(
        self, fid_list: list[str], to_pdir_fid: str, action_type: int = 1
    ) -> dict[str, Any]:
        """POST /open/v1/file/move.

        CLI parity: body key order ``fid_list, to_pdir_fid, action_type``
        (FileBrowser.moveFiles -> api.moveFiles).
        """
        return await self._request(
            "POST",
            PATH_FILE_MOVE,
            body={
                "fid_list": fid_list,
                "to_pdir_fid": to_pdir_fid,
                "action_type": action_type,
            },
        )

    async def get_download_url(self, fid: str) -> dict[str, Any]:
        """POST /open/v1/file/get_download_url.

        CLI parity (DownloadApi.buildUrl): the query carries
        ``req_id``+``access_token`` only, no ``device_id``.
        Note: the open API rejects files >50MB here (errno 23018).
        """
        return await self._request(
            "POST",
            PATH_GET_DOWNLOAD_URL,
            include_device_id=False,
            body={"fid": fid},
        )

    async def delete_files(self, fid_list: list[str]) -> dict[str, Any]:
        """POST /open/v1/file/delete (``action_type: 2``).

        Real deletion endpoint (absent from the official CLI but validated
        against the same open API/credentials by the quark-drive-ext
        project). Used so backup deletion does not leave a trash folder.
        """
        return await self._request(
            "POST",
            PATH_FILE_DELETE,
            body={"fid_list": fid_list, "action_type": 2},
        )

    def download_cookie_header(self) -> str:
        """Cookie header the CLI sends when GETting file download urls."""
        return f"x_pan_client_id={CLIENT_ID};x_pan_access_token={self._access_token}"

    # ------------------------------------------------------------------
    # Upload
    # ------------------------------------------------------------------

    async def _proof_codes(
        self,
        read_at: Callable[[int, int], bytes],
        size: int,
        x_pan_token: str,
    ) -> dict[str, str]:
        """Port of the skill's v1 proof code calculation (non-blocking)."""

        def seed(value: str) -> str:
            return _md5hex(_md5hex(value))

        def offset(seed_hex: str) -> int:
            if size == 0:
                return 0
            return int(seed_hex[:16], 16) % size

        async def code_at(offset_value: int) -> str:
            chunk = await asyncio.to_thread(read_at, offset_value, offset_value + 8)
            return base64.b64encode(chunk).decode()

        seed1 = seed(f"{self._user_id}{x_pan_token}")
        seed2 = seed(str(size))
        return {
            "proof_version": "v1",
            "proof_seed1": seed1,
            "proof_seed2": seed2,
            "proof_code1": await code_at(offset(seed1)),
            "proof_code2": await code_at(offset(seed2)),
        }

    async def upload_file(
        self,
        file_path: str,
        filename: str,
        pdir_fid: str,
        size: int,
        read_at: Callable[[int, int], bytes],
        content_type: str = "application/octet-stream",
        on_progress: Callable[[int], None] | None = None,
    ) -> dict[str, Any]:
        """Upload a local file following the skill CLI's upload strategies.

        - ``size < 10MB`` -> FormUpload: upload_pre with sha1/md5 and a
          single-part ``part_info_list`` carrying the SHA1 running state,
          PUT via the url returned by upload_pre, finish with the ETag.
        - otherwise -> PrepositiveUpload: upload_pre with ``hash_update``
          and empty sha1, then ``update/hash``, ``get_upload_urls``,
          PUT every part (signed), finish with ETags.

        Hashes are computed internally (single pass).
        """
        if size < FORM_UPLOAD_SIZE_LIMIT:
            return await self._upload_small(
                filename, pdir_fid, size, read_at, content_type, on_progress
            )
        return await self._upload_large(
            filename, pdir_fid, size, read_at, content_type, on_progress
        )

    async def _upload_pre_body(
        self,
        filename: str,
        pdir_fid: str,
        content_type: str,
        read_at: Callable[[int, int], bytes],
        size: int,
    ) -> dict[str, Any]:
        """Build the base upload_pre body (mirrors pt() in the skill CLI)."""
        path = PATH_UPLOAD_PRE
        headers, x_pan_token = self._sign_headers("POST", path)
        body: dict[str, Any] = {
            "file_name": filename,
            "size": size,
            "pdir_fid": pdir_fid,
            "format_type": content_type,
            "parallel_upload": True,
            "agent_id": AGENT_ID,
        }
        if self._user_id:
            body.update(await self._proof_codes(read_at, size, x_pan_token))
        return body

    async def _upload_small(
        self,
        filename: str,
        pdir_fid: str,
        size: int,
        read_at: Callable[[int, int], bytes],
        content_type: str,
        on_progress: Callable[[int], None] | None,
    ) -> dict[str, Any]:
        """FormUpload strategy (files < 10MB in the skill CLI)."""
        body = await self._upload_pre_body(
            filename, pdir_fid, content_type, read_at, size
        )
        whole = await asyncio.to_thread(read_at, 0, size)
        body["sha1"] = hashlib.sha1(whole).hexdigest()
        body["md5"] = hashlib.md5(whole).hexdigest()
        body["hash_update"] = False
        body["part_info_list"] = [
            {
                "part_number": 1,
                "part_size": size,
                "parallel_sha1_ctx": {
                    "part_offset": 0,
                    "h": _sha1_context_state(whole),
                },
            }
        ]
        pre = await self._request("POST", PATH_UPLOAD_PRE, body=body)
        if pre.get("finish"):
            return pre
        task_id = pre.get("task_id")
        targets = pre.get("upload_urls") or []
        if not task_id or not targets:
            raise QuarkApiError("upload_pre returned no task/url")
        target = targets[0]
        etag = await self._put_part(
            target, whole, pre.get("common_headers") or {}
        )
        if on_progress:
            on_progress(size)
        finish = await self._request(
            "POST",
            PATH_UPLOAD_FINISH,
            body={
                "task_id": task_id,
                "part_info_list": [{"part_number": 1, "etag": etag}],
            },
        )
        if not finish.get("finish"):
            raise QuarkApiError("upload_finish did not complete")
        return finish

    async def _upload_large(
        self,
        filename: str,
        pdir_fid: str,
        size: int,
        read_at: Callable[[int, int], bytes],
        content_type: str,
        on_progress: Callable[[int], None] | None,
    ) -> dict[str, Any]:
        """PrepositiveUpload strategy (files >= 10MB in the skill CLI).

        One hash pass computes sha1+md5+part states together after
        ``upload_pre`` returns the server's part size.
        """
        body = await self._upload_pre_body(
            filename, pdir_fid, content_type, read_at, size
        )
        body["sha1"] = ""
        body["hash_update"] = True
        pre = await self._request("POST", PATH_UPLOAD_PRE, body=body)
        if pre.get("finish"):
            return pre
        task_id = pre.get("task_id")
        part_size = int(pre.get("part_size") or 0)
        if not task_id or part_size <= 0:
            raise QuarkApiError("upload_pre returned no task")

        part_numbers = list(range(1, (size + part_size - 1) // part_size + 1))
        # Single pass: hashlib sha1/md5 + SHA1 state at each part boundary
        # (the server requires them for its parallel hash pipeline).
        _LOGGER.debug(
            "hashing file once (%d bytes, part_size=%d, %d parts)",
            size,
            part_size,
            len(part_numbers),
        )
        sha1, md5, states = await asyncio.to_thread(
            _hashes_and_part_states, read_at, size, part_size
        )

        upd = await self._request(
            "POST",
            PATH_UPDATE_HASH,
            body={"task_id": task_id, "sha1": sha1, "md5": md5},
        )
        if upd.get("finish"):
            return upd

        part_info: list[dict[str, Any]] = []
        for n in part_numbers:
            entry: dict[str, Any] = {
                "part_number": n,
                "part_size": min(part_size, size - (n - 1) * part_size),
            }
            if n > 1:
                entry["parallel_sha1_ctx"] = {
                    "part_offset": (n - 1) * part_size,
                    "h": states[n - 1],
                }
            part_info.append(entry)
        try:
            urls_data = await self._request(
                "POST",
                PATH_GET_UPLOAD_URLS,
                body={"task_id": task_id, "part_info_list": part_info},
            )
        except QuarkApiError as err:
            _LOGGER.warning(
                "get_upload_urls failed (%s); falling back to single-part upload",
                err,
            )
            return await self._upload_small(
                filename, pdir_fid, size, read_at, content_type, on_progress
            )
        common_headers: dict[str, str] = (
            urls_data.get("common_headers")
            or pre.get("common_headers")
            or {}
        )
        url_map = {
            u.get("part_number"): u for u in urls_data.get("upload_urls") or []
        }
        if set(url_map) != set(part_numbers):
            raise QuarkApiError("missing upload urls")

        # CLI parity: up to maxConcurrentPartSize (6) parts in parallel,
        # sharing one session (keep-alive) across all PUTs.
        sem = asyncio.Semaphore(MAX_CONCURRENT_PART_UPLOADS)
        uploaded = 0
        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_PART_UPLOADS)

        async def put_part(info: dict[str, Any]) -> dict[str, Any]:
            nonlocal uploaded
            n = info["part_number"]
            start = (n - 1) * part_size
            async with sem:
                chunk = await asyncio.to_thread(
                    read_at, start, start + info["part_size"]
                )
                # CLI parity: retry each part PUT up to 3 times.
                etag = ""
                last_err: Exception | None = None
                for attempt in range(3):
                    try:
                        etag = await self._put_part(
                            url_map[n], chunk, common_headers, session=direct
                        )
                        last_err = None
                        break
                    except (QuarkApiError, aiohttp.ClientError, TimeoutError) as err:
                        last_err = err
                        _LOGGER.warning(
                            "part %d PUT attempt %d failed: %s", n, attempt + 1, err
                        )
                        await asyncio.sleep(0.5 * (2**attempt))
                if last_err is not None:
                    raise last_err
            uploaded += len(chunk)
            if on_progress:
                on_progress(uploaded)
            return {"part_number": n, "etag": etag}

        connector = aiohttp.TCPConnector(limit=MAX_CONCURRENT_PART_UPLOADS)
        async with aiohttp.ClientSession(connector=connector) as direct:
            parts_etag = sorted(
                await asyncio.gather(*(put_part(info) for info in part_info)),
                key=lambda p: p["part_number"],
            )

        finish = await self._request(
            "POST",
            PATH_UPLOAD_FINISH,
            body={"task_id": task_id, "part_info_list": parts_etag},
        )
        if not finish.get("finish"):
            raise QuarkApiError("upload_finish did not complete")
        return finish

    async def _put_part(
        self,
        target: dict[str, Any],
        chunk: bytes,
        common_headers: dict[str, str],
        session: aiohttp.ClientSession | None = None,
        connector: aiohttp.TCPConnector | None = None,
    ) -> str:
        """PUT one part to OSS with the signed headers, return the ETag.

        Mirrors the skill CLI's Node ``uploadChunk`` exactly: only the
        ``common_headers`` plus ``Authorization`` are sent. aiohttp's
        automatic headers (User-Agent/Accept/Accept-Encoding) are
        suppressed because the OSS V4 signature check fails otherwise.
        """
        signature = (target.get("signature_info") or {}).get("signature", "")
        url = target.get("upload_url") or ""
        if not url:
            raise QuarkApiError("upload url missing")
        headers = {**common_headers, "Authorization": signature}
        # encoded=True: keep the URL exactly as the server signed it.
        # yarl's default requoting can break the OSS V4 signature check.
        own_session = session is None
        if own_session:
            session = aiohttp.ClientSession(connector=connector)
        try:
            async with session.put(
                yarl.URL(url, encoded=True),
                data=chunk,
                headers=headers,
                skip_auto_headers=(
                    "accept",
                    "accept-encoding",
                    "user-agent",
                    "content-type",
                ),
                timeout=ClientTimeout(total=UPLOAD_TIMEOUT),
            ) as resp:
                if resp.status not in (200, 201, 204):
                    text = await resp.text()
                    raise QuarkApiError(
                        f"part PUT failed: http={resp.status} {text[:300]}"
                    )
                return resp.headers.get("ETag", "")
        finally:
            if own_session:
                await session.close()
