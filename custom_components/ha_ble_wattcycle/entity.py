"""Shared entity base for WattCycle BLE."""

from __future__ import annotations

from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_DEVICE_TYPE, DOMAIN, MANUFACTURER
from .coordinator import WattCycleCoordinator


class WattCycleEntity(CoordinatorEntity[WattCycleCoordinator]):
    """Base entity tying all sensors to one battery device."""

    _attr_has_entity_name = True

    def __init__(self, coordinator: WattCycleCoordinator, key: str) -> None:
        super().__init__(coordinator)
        address = coordinator.entry.data["address"]
        self._attr_unique_id = f"{address}_{key}"
        self._attr_device_info = DeviceInfo(
            connections={(CONNECTION_BLUETOOTH, address)},
            identifiers={(DOMAIN, address)},
            name=coordinator.entry.title,
            manufacturer=MANUFACTURER,
            model=coordinator.entry.data.get(CONF_DEVICE_TYPE, "watt").upper(),
        )
