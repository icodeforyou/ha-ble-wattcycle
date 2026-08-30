"""The WattCycle BLE Battery integration.

Reverse-engineered for interoperability under EU Directive 2009/24/EC Art. 6.
Not affiliated with or endorsed by WattCycle.
"""

from __future__ import annotations

import logging

import voluptuous as vol
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import config_validation as cv

from .const import (
    ATTR_DATA,
    CONF_DEVICE_TYPE,
    CONF_PROTOCOL_MODE,
    CONF_QUIET_LOGGING,
    CONF_SCAN_INTERVAL,
    CONF_USE_HILINK_AUTH,
    DEFAULT_QUIET_LOGGING,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    SERVICE_SEND_RAW,
)
from .coordinator import WattCycleConnection, WattCycleCoordinator
from .protocol import DeviceType

PLATFORMS: list[Platform] = [Platform.SENSOR, Platform.BINARY_SENSOR]

type WattCycleConfigEntry = ConfigEntry[WattCycleCoordinator]


def _apply_log_level(entry: WattCycleConfigEntry) -> None:
    """Quiet the integration's own logger once the user is happy it works."""
    quiet = entry.options.get(CONF_QUIET_LOGGING, DEFAULT_QUIET_LOGGING)
    logging.getLogger(__package__).setLevel(logging.ERROR if quiet else logging.NOTSET)


async def async_setup_entry(hass: HomeAssistant, entry: WattCycleConfigEntry) -> bool:
    """Set up WattCycle BLE from a config entry."""
    _apply_log_level(entry)
    address: str = entry.data["address"]
    device_type = DeviceType(entry.data.get(CONF_DEVICE_TYPE, DeviceType.WATT.value))
    use_hilink_auth: bool = entry.data.get(CONF_USE_HILINK_AUTH, False)
    protocol_hint: str | None = entry.data.get(CONF_PROTOCOL_MODE)
    scan_interval: int = entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)

    connection = WattCycleConnection(
        hass, address, device_type, use_hilink_auth, protocol_hint
    )
    coordinator = WattCycleCoordinator(hass, entry, connection, scan_interval)
    await coordinator.async_config_entry_first_refresh()

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    _async_register_services(hass)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: WattCycleConfigEntry) -> bool:
    """Unload a config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.async_shutdown()
    if not hass.config_entries.async_loaded_entries(DOMAIN):
        hass.services.async_remove(DOMAIN, SERVICE_SEND_RAW)
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: WattCycleConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


def _async_register_services(hass: HomeAssistant) -> None:
    """Register the send_raw service once."""
    if hass.services.has_service(DOMAIN, SERVICE_SEND_RAW):
        return

    async def _handle_send_raw(call: ServiceCall) -> None:
        entry_id: str = call.data["entry_id"]
        entry = hass.config_entries.async_get_entry(entry_id)
        if entry is None or entry.domain != DOMAIN:
            raise HomeAssistantError(f"Unknown WattCycle config entry: {entry_id}")
        raw: str = call.data[ATTR_DATA].replace(" ", "")
        try:
            payload = bytes.fromhex(raw)
        except ValueError as err:
            raise HomeAssistantError(f"Invalid hex in '{ATTR_DATA}': {err}") from err
        coordinator: WattCycleCoordinator = entry.runtime_data
        await coordinator.connection.async_write_raw(payload)

    hass.services.async_register(
        DOMAIN,
        SERVICE_SEND_RAW,
        _handle_send_raw,
        schema=vol.Schema(
            {
                vol.Required("entry_id"): cv.string,
                vol.Required(ATTR_DATA): cv.string,
            }
        ),
    )
