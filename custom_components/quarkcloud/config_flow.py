"""Config flow for the Quark Cloud Drive integration.

The flow mirrors the official skill login:

1. Home Assistant requests an authorize page url from the Quark open API.
2. The user opens the url, scans the QR code with the Quark Drive app and
   receives an ``AAC-xxxx`` agent auth code.
3. The user pastes that code back here and it is exchanged for tokens.
"""

from __future__ import annotations

import logging
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
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

STEP_USER_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_AUTH_CODE): str,
    }
)

_LOGGER = logging.getLogger(__name__)


class QuarkCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle the Quark Cloud Drive config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._authorize_url: str = ""
        self._device_id: str = DEFAULT_DEVICE_ID

    def _show_user_form(self, errors: dict[str, str] | None = None) -> ConfigFlowResult:
        return self.async_show_form(
            step_id="user",
            data_schema=STEP_USER_SCHEMA,
            description_placeholders={"authorize_url": self._authorize_url},
            errors=errors or {},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Show the authorize link and ask for the AAC code."""
        errors: dict[str, str] = {}
        if user_input is None:
            description = {"authorize_url": self._authorize_url}
            try:
                api = QuarkCloudApi(async_get_clientsession(self.hass))
                page = await api.get_authorize_page_url("Home Assistant")
                self._authorize_url = page["authorize_page_url"]
                self._device_id = page.get("device_id") or DEFAULT_DEVICE_ID
                description = {"authorize_url": self._authorize_url}
            except (QuarkApiError, aiohttp.ClientError, TimeoutError) as err:
                errors["base"] = "cannot_connect"
                _LOGGER.error("Failed to get authorize page url: %s", err)
            return self.async_show_form(
                step_id="user",
                data_schema=STEP_USER_SCHEMA,
                description_placeholders=description,
                errors=errors,
            )

        auth_code = user_input[CONF_AUTH_CODE].strip()
        try:
            api = QuarkCloudApi(
                async_get_clientsession(self.hass),
                device_id=self._device_id,
            )
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
                CONF_DEVICE_ID: tokens["device_id"] or self._device_id,
                CONF_USER_ID: tokens["user_id"],
            },
        )
