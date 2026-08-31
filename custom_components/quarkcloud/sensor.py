"""Sensors for the Quark Cloud Drive integration.

Mirrors the skill CLI's ``get-user-info`` command: it calls both
``/open/v1/user/get_vip_info`` and ``/open/v1/user/info`` and only reports
success when both respond. The backup count sensor reuses the same search
endpoint (``/agent/v1/file/search``) the backup agent lists backups with.
"""

from __future__ import annotations

from datetime import datetime, timedelta
import logging
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import PERCENTAGE, UnitOfInformation
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.util import dt as dt_util
from homeassistant.helpers.update_coordinator import (
    CoordinatorEntity,
    DataUpdateCoordinator,
    UpdateFailed,
)

from . import QuarkCloudConfigEntry
from .api import QuarkApiError, QuarkCloudApi
from .const import BACKUP_FILE_PREFIX, DOMAIN

_LOGGER = logging.getLogger(__name__)

UPDATE_INTERVAL = timedelta(minutes=30)

# Known vip_type values; unknown ones still display as the raw server value.
# Keys must be lowercase (hassfest requirement for translation state keys).
VIP_OPTIONS = ["normal", "vip", "svip", "88vip", "partner"]


def _parse_created_at(value: Any) -> datetime | None:
    """Parse vip info ``created_at`` (e.g. ``2025-02-13T07:27:20.000+0800``)."""
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%f%z")
    except ValueError:
        return None
    return dt_util.as_utc(parsed)


def _ms_to_datetime(value: Any) -> datetime | None:
    """Convert a millisecond epoch (vip ``expires_in``) to UTC datetime."""
    if not isinstance(value, (int, float)) or value <= 0:
        return None
    return dt_util.utc_from_timestamp(value / 1000)


async def _fetch_drive_info(api: QuarkCloudApi) -> dict[str, Any]:
    """Fetch user + vip info (both must succeed, like the CLI)."""
    try:
        user = await api.get_user_info()
        vip = await api.get_vip_info()
    except QuarkApiError as err:
        raise UpdateFailed(str(err)) from err

    # Count uploaded backups (single search call, same as the backup agent).
    backup_count: int | None = None
    try:
        result = await api.search_files(BACKUP_FILE_PREFIX, size=100)
        backup_count = sum(
            1
            for item in result.get("file_list") or []
            if str(item.get("filename", "")).endswith(".tar")
        )
    except QuarkApiError as err:
        _LOGGER.debug("Backup count search failed: %s", err)

    return {
        "nickname": user.get("nickname", ""),
        "user_id": user.get("user_id", ""),
        "avatar_url": user.get("avatar_url", ""),
        "vip_type": vip.get("vip_type", ""),
        "used": vip.get("used"),
        "capacity": vip.get("capacity"),
        "membership_expiry": _ms_to_datetime(vip.get("expires_in")),
        "account_created": _parse_created_at(vip.get("created_at")),
        "backup_count": backup_count,
    }


async def async_setup_entry(
    hass: HomeAssistant,
    entry: QuarkCloudConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Quark Cloud Drive sensors from a config entry."""
    api: QuarkCloudApi = entry.runtime_data

    coordinator: DataUpdateCoordinator[dict[str, Any]] = DataUpdateCoordinator(
        hass,
        _LOGGER,
        name=f"{DOMAIN}_{entry.entry_id}",
        update_method=lambda: _fetch_drive_info(api),
        update_interval=UPDATE_INTERVAL,
    )
    await coordinator.async_refresh()
    # A failed first refresh (e.g. token not yet rotated) must not crash
    # platform setup: entities are created unavailable and the coordinator
    # keeps retrying every UPDATE_INTERVAL until it succeeds.
    data: dict[str, Any] = coordinator.data or {}

    device_unique_id = data.get("user_id") or entry.entry_id
    device = DeviceInfo(
        identifiers={(DOMAIN, device_unique_id)},
        name="Quark Cloud Drive",
        manufacturer="Quark",
        model=data.get("nickname") or "Quark Cloud Drive",
        sw_version="1.0.15",
        serial_number=data.get("user_id") or None,
        configuration_url="https://pan.quark.cn",
    )

    descriptions = [
        SensorEntityDescription(
            key="nickname",
            translation_key="nickname",
            icon="mdi:account",
        ),
        SensorEntityDescription(
            key="membership",
            translation_key="membership",
            device_class=SensorDeviceClass.ENUM,
            options=VIP_OPTIONS,
            icon="mdi:card-account-details-star",
        ),
        SensorEntityDescription(
            key="membership_expiry",
            translation_key="membership_expiry",
            device_class=SensorDeviceClass.TIMESTAMP,
            icon="mdi:calendar-clock",
        ),
        SensorEntityDescription(
            key="account_created",
            translation_key="account_created",
            device_class=SensorDeviceClass.TIMESTAMP,
            icon="mdi:calendar-account",
        ),
        SensorEntityDescription(
            key="used",
            translation_key="used",
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfInformation.GIGABYTES,
            icon="mdi:cloud-upload",
        ),
        SensorEntityDescription(
            key="capacity",
            translation_key="capacity",
            device_class=SensorDeviceClass.DATA_SIZE,
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=UnitOfInformation.GIGABYTES,
            icon="mdi:cloud",
        ),
        SensorEntityDescription(
            key="usage_percent",
            translation_key="usage_percent",
            state_class=SensorStateClass.MEASUREMENT,
            native_unit_of_measurement=PERCENTAGE,
            icon="mdi:gauge",
        ),
        SensorEntityDescription(
            key="backup_count",
            translation_key="backup_count",
            state_class=SensorStateClass.MEASUREMENT,
            icon="mdi:backup-restore",
        ),
    ]
    async_add_entities(
        (
            QuarkMembershipSensor(coordinator, description, device, entry.entry_id)
            if description.key == "membership"
            else QuarkSensor(coordinator, description, device, entry.entry_id)
        )
        for description in descriptions
    )


def _to_gb(value: Any) -> float | None:
    if not isinstance(value, (int, float)) or value < 0:
        return None
    return round(value / 1000**3, 2)


class QuarkSensor(CoordinatorEntity[DataUpdateCoordinator], SensorEntity):
    """A single Quark Cloud Drive info sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: DataUpdateCoordinator,
        description: SensorEntityDescription,
        device: DeviceInfo,
        entry_id: str,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = description
        self._attr_unique_id = f"{entry_id}_{description.key}"
        self._attr_device_info = device

    @property
    def native_value(self) -> str | float | datetime | None:
        data = self.coordinator.data or {}
        key = self.entity_description.key
        if key in ("membership_expiry", "account_created", "backup_count"):
            return data.get(key)
        if key == "nickname":
            return data.get("nickname") or None
        if key == "membership":
            raw = data.get("vip_type")
            return raw.lower() if raw else None
        if key == "usage_percent":
            used = data.get("used")
            capacity = data.get("capacity")
            if (
                isinstance(used, (int, float))
                and isinstance(capacity, (int, float))
                and capacity > 0
            ):
                return round(used / capacity * 100, 1)
            return None
        value = _to_gb(data.get(key))
        return value


class QuarkMembershipSensor(QuarkSensor):
    """Membership sensor: enum device class with runtime-extensible options."""

    @property
    def options(self) -> list[str]:
        """Enum options; extend with unknown server values at runtime."""
        raw = (self.coordinator.data or {}).get("vip_type")
        raw_lower = raw.lower() if raw else None
        if raw_lower and raw_lower not in VIP_OPTIONS:
            return [*VIP_OPTIONS, raw_lower]
        return VIP_OPTIONS
