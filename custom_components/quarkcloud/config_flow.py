"""Config flow for the Quark Cloud Drive integration.

Authorization is code-based (no QR scanning inside Home Assistant):

1. The user obtains an ``AAC-``/``CAC-`` agent auth code from the Quark
   desktop client (avatar menu -> Drive Skill authorization).
2. The code is pasted here and exchanged for tokens.

An options flow lets the user paste a new code later (settings →
integration → configure) to refresh the authorization in place.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import (
    QuarkAlreadyAuthorized,
    QuarkApiError,
    QuarkAuthCodeExpired,
    QuarkCloudApi,
)
from .const import (
    CONF_ACCESS_TOKEN,
    CONF_ACCESS_TOKEN_EXPIRES_AT,
    CONF_AUTH_CODE,
    CONF_DEVICE_ID,
    CONF_REFRESH_TOKEN,
    CONF_USER_ID,
    DEFAULT_DEVICE_ID,
    DOMAIN,
    NAME,
)

if TYPE_CHECKING:
    from . import QuarkCloudConfigEntry

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTH_CODE): str,
    }
)

_LOGGER = logging.getLogger(__name__)


class QuarkCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Quark Cloud Drive config flow."""

    VERSION = 1

    def _show_user_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            errors=errors or {},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for the auth code and exchange it for tokens."""
        if user_input is not None:
            auth_code = user_input[CONF_AUTH_CODE].strip()
            try:
                api = QuarkCloudApi(async_get_clientsession(self.hass))
                tokens = await api.exchange_agent_auth_code(auth_code)
            except QuarkAuthCodeExpired:
                _LOGGER.warning("Auth code expired")
                return self._show_user_form(errors={"base": "expired"})
            except QuarkAlreadyAuthorized:
                _LOGGER.warning("Account already authorized for this device")
                return self._show_user_form(errors={"base": "already_authorized"})
            except QuarkApiError as err:
                _LOGGER.warning("Auth code exchange failed: %s", err)
                return self._show_user_form(errors={"base": "invalid_auth_code"})
            except (aiohttp.ClientError, TimeoutError):
                return self._show_user_form(errors={"base": "cannot_connect"})

            return self.async_create_entry(
                title=NAME,
                data={
                    CONF_ACCESS_TOKEN: tokens["access_token"],
                    CONF_ACCESS_TOKEN_EXPIRES_AT: tokens["access_token_expires_at"],
                    CONF_REFRESH_TOKEN: tokens["refresh_token"],
                    CONF_DEVICE_ID: tokens["device_id"] or DEFAULT_DEVICE_ID,
                    CONF_USER_ID: tokens["user_id"],
                },
            )

        return self._show_user_form()

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: QuarkCloudConfigEntry,
    ) -> QuarkCloudOptionsFlow:
        """Create the options flow handler."""
        return QuarkCloudOptionsFlow()


class QuarkCloudOptionsFlow(OptionsFlow):
    """Handle Quark Cloud Drive options (update the auth code).

    Lets the user paste a new ``AAC-``/``CAC-`` authorization code directly
    (obtained from the Quark app/web authorization page) without going
    through remove + re-add. The code is exchanged for fresh tokens, the
    config entry is updated in place and reloaded.
    """

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a new auth code and exchange it."""
        errors: dict[str, str] = {}
        entry = self.config_entry
        if user_input is not None:
            auth_code = user_input[CONF_AUTH_CODE].strip()
            try:
                api = QuarkCloudApi(
                    async_get_clientsession(self.hass),
                    device_id=entry.data.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID),
                )
                tokens = await api.exchange_agent_auth_code(auth_code)
            except QuarkAuthCodeExpired:
                _LOGGER.warning("Auth code expired")
                errors["base"] = "expired"
            except QuarkAlreadyAuthorized:
                _LOGGER.warning("Account already authorized for this device")
                errors["base"] = "already_authorized"
            except QuarkApiError as err:
                _LOGGER.warning("Auth code exchange failed: %s", err)
                errors["base"] = "invalid_auth_code"
            except (aiohttp.ClientError, TimeoutError):
                errors["base"] = "cannot_connect"
            else:
                _LOGGER.info(
                    "Auth code updated for entry %s (status=%s)",
                    entry.title,
                    tokens.get("status"),
                )
                self.hass.config_entries.async_update_entry(
                    entry,
                    data={
                        **entry.data,
                        CONF_ACCESS_TOKEN: tokens["access_token"],
                        CONF_ACCESS_TOKEN_EXPIRES_AT: tokens[
                            "access_token_expires_at"
                        ],
                        CONF_REFRESH_TOKEN: tokens["refresh_token"],
                        CONF_DEVICE_ID: tokens["device_id"]
                        or entry.data.get(CONF_DEVICE_ID, DEFAULT_DEVICE_ID),
                        CONF_USER_ID: tokens["user_id"],
                    },
                )
                # Reload so a fresh API client picks up the new tokens
                # (also clears any ConfigEntryNotReady retry loop).
                await self.hass.config_entries.async_reload(entry.entry_id)
                return self.async_create_entry(title="", data={})

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema({vol.Required(CONF_AUTH_CODE): str}),
            errors=errors,
        )
