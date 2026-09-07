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
            "protocol_mode": connection.protocol_mode,
            "firmware_version": connection.firmware_version,
            "last_tx_frames": connection.last_tx,
            "last_rx_frames": connection.last_rx,
            "last_ack": (
                {
                    "command": f"0x{connection.last_ack.command:02x}",
                    "status": connection.last_ack.error,
                }
                if connection.last_ack
                else None
            ),
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
        "event_log": {
            "record_index": connection.record_info.index if connection.record_info else None,
            "record_capacity": (
                connection.record_info.capacity if connection.record_info else None
            ),
            "bms_clock_raw": connection.bms_clock.hex if connection.bms_clock else None,
            "bms_clock_read_at": (
                connection.bms_clock_read_at.isoformat() if connection.bms_clock_read_at else None
            ),
            "records": [
                {
                    **{k: v for k, v in asdict(rec).items() if k != "header"},
                    "header": rec.header.hex(" "),
                    "sequence": rec.sequence,
                    "timestamp_raw": rec.timestamp,
                    "protections": rec.active_protections,
                    "warnings": rec.active_warnings,
                }
                for rec in connection.events
            ],
        },
    }
