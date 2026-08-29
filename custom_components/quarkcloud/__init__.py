"""The Quark Cloud Drive integration."""

from __future__ import annotations

import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import QuarkApiError, QuarkAuthError, QuarkCloudApi
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DATA_BACKUP_AGENT_LISTENERS,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)

type QuarkCloudConfigEntry = ConfigEntry[QuarkCloudApi]

PLATFORMS = ["sensor"]


async def async_setup_entry(
    hass: HomeAssistant, entry: QuarkCloudConfigEntry
) -> bool:
    """Set up Quark Cloud Drive from a config entry."""

    @callback
    def persist_tokens(
        access_token: str,
        refresh_token: str,
        user_id: str,
        device_id: str,
        access_token_expires_at: int = 0,
    ) -> None:
        """Persist rotated tokens so restarts keep working.

        CLI parity (``updatePersistedAccessToken``): skip the write when
        the token is unchanged, so rotations do not spam config entry
        updates (each of which notifies backup listeners / reloads state).
        """
        if (
            entry.data.get(CONF_ACCESS_TOKEN) == access_token
            and entry.data.get(CONF_REFRESH_TOKEN) == refresh_token
            and entry.data.get(CONF_USER_ID) == user_id
            and entry.data.get(CONF_DEVICE_ID) == device_id
            and str(entry.data.get(CONF_ACCESS_TOKEN_EXPIRES_AT, ""))
            == str(access_token_expires_at)
        ):
            return
        hass.config_entries.async_update_entry(
            entry,
            data={
                **entry.data,
                CONF_ACCESS_TOKEN: access_token,
                CONF_REFRESH_TOKEN: refresh_token,
                CONF_USER_ID: user_id,
                CONF_DEVICE_ID: device_id,
                CONF_ACCESS_TOKEN_EXPIRES_AT: str(access_token_expires_at),
            },
        )

    api = QuarkCloudApi(
        async_get_clientsession(hass),
        access_token=entry.data[CONF_ACCESS_TOKEN],
        refresh_token=entry.data[CONF_REFRESH_TOKEN],
        device_id=entry.data[CONF_DEVICE_ID],
        user_id=entry.data[CONF_USER_ID],
        access_token_expires_at=entry.data.get(CONF_ACCESS_TOKEN_EXPIRES_AT, 0),
        on_tokens_updated=persist_tokens,
    )

    try:
        await api.get_user_info()
    except QuarkAuthError as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_auth_failed",
            translation_placeholders={"error": str(err)},
        ) from err
    except (QuarkApiError, TimeoutError) as err:
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="setup_connect_failed",
            translation_placeholders={"error": str(err)},
        ) from err

    entry.runtime_data = api

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    @callback
    def notify_backup_listeners() -> None:
        for listener in hass.data.get(DATA_BACKUP_AGENT_LISTENERS, []):
            listener()

    entry.async_on_unload(entry.async_on_state_change(notify_backup_listeners))

    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: QuarkCloudConfigEntry
) -> bool:
    """Unload a Quark Cloud Drive config entry."""
    notify = hass.data.get(DATA_BACKUP_AGENT_LISTENERS, [])
    for listener in notify:
        listener()
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
