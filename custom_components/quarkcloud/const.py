"""Constants for the Quark Cloud Drive integration."""

from collections.abc import Callable
from typing import Final

from homeassistant.util.hass_dict import HassKey

DOMAIN: Final = "quarkcloud"
NAME: Final = "Quark Cloud Drive"

# Credentials embedded in the official Quark Drive skill (quarkclouddrive).
# These are the public "wild" agent credentials used by the skill CLI.
CLIENT_ID: Final = "third_party_agent"
SIGN_KEY: Final = "cf134812e2de4032bd1cb7c3727e84b3"
DEFAULT_DEVICE_ID: Final = "wild_claw"
AGENT_ID: Final = "homeassistant"

API_BASE_URL: Final = "https://open-api-drive.quark.cn"

PATH_GET_AUTHORIZE_PAGE_URL: Final = "/agent/v1/get_authorize_page_url"
PATH_AGENT_AUTH_CODE: Final = "/agent/v1/oauth/agent_auth_code"
PATH_TOKEN_ROTATE: Final = "/agent/v1/oauth/access_token/rotate"
PATH_USER_INFO: Final = "/open/v1/user/info"
PATH_VIP_INFO: Final = "/open/v1/user/get_vip_info"
PATH_FILE_SEARCH: Final = "/agent/v1/file/search"
PATH_CREATE_DIR: Final = "/open/v1/dir"
PATH_FILE_MOVE: Final = "/open/v1/file/move"
PATH_FILE_DELETE: Final = "/open/v1/file/delete"
PATH_UPLOAD_PRE: Final = "/open/v1/file/upload_pre"
PATH_UPDATE_HASH: Final = "/open/v1/file/update/hash"
PATH_GET_UPLOAD_URLS: Final = "/open/v1/file/get_upload_urls"
PATH_UPLOAD_FINISH: Final = "/open/v1/file/upload_finish"
PATH_GET_DOWNLOAD_URL: Final = "/open/v1/file/get_download_url"

CONF_AUTH_CODE = "auth_code"
CONF_ACCESS_TOKEN = "access_token"
CONF_REFRESH_TOKEN = "refresh_token"
CONF_DEVICE_ID = "device_id"
CONF_USER_ID = "user_id"
CONF_ACCESS_TOKEN_EXPIRES_AT = "access_token_expires_at"

# Backup storage layout on the cloud drive.
BACKUP_DIR_NAME: Final = "home_assistant_backups"
TRASH_DIR_NAME: Final = "home_assistant_backups_trash"
BACKUP_FILE_PREFIX: Final = "ha_backup_"
DELETED_BACKUP_IDS = "deleted_backup_ids"

DATA_BACKUP_AGENT_LISTENERS: HassKey[list[Callable[[], None]]] = HassKey(
    f"{DOMAIN}.backup_agent_listeners"
)

# aiohttp timeouts.
REQUEST_TIMEOUT: Final = 60
UPLOAD_TIMEOUT: Final = 3600

# Files below this size use the skill CLI's FormUpload strategy,
# larger ones use PrepositiveUpload (same thresholds as the CLI).
FORM_UPLOAD_SIZE_LIMIT: Final = 10 * 1024 * 1024

# CLI parity: fn.maxConcurrentPartSize - concurrent part uploads.
MAX_CONCURRENT_PART_UPLOADS: Final = 6
