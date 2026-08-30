"""Diagnostics for WattCycle BLE — dumps decoded state and raw frames."""

from __future__ import annotations

from dataclasses import asdict
from typing import Any

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from . import WattCycleConfigEntry


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: WattCycleConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    connection = coordinator.connection
    address: str = entry.data["address"]

    service_info = bluetooth.async_last_service_info(hass, address, connectable=True)

    return {
        "entry": {
            "title": entry.title,
            "device_type": entry.data.get("device_type"),
            "use_hilink_auth": entry.data.get("use_hilink_auth"),
            "options": dict(entry.options),
        },
        "connection": {
            "connected": connection.connected,
            "firmware_version": connection.firmware_version,
            "last_tx_frames": connection.last_tx,
            "last_rx_frames": connection.last_rx,
        },
        "advertisement": {
            "rssi": service_info.rssi if service_info else None,
            "name": service_info.name if service_info else None,
            "service_uuids": list(service_info.service_uuids) if service_info else None,
            "manufacturer_ids": (
                list(service_info.manufacturer_data.keys()) if service_info else None
            ),
        },
        "state": asdict(coordinator.data) if coordinator.data else None,
    }
