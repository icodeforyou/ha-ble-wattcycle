"""Binary sensor platform for WattCycle BLE.

Charging/discharging are derived from the sign of the pack current. The sign convention
(positive = charge vs discharge) is UNVERIFIED against real hardware — see docs/PROTOCOL.md
§4.1 and docs/TESTPLAN.md. Adjust CURRENT_DEADBAND / the comparison after field testing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WattCycleConfigEntry
from .coordinator import WattCycleCoordinator
from .entity import WattCycleEntity
from .protocol import BatteryState

# Ignore tiny idle currents to avoid flapping.
CURRENT_DEADBAND = 0.2  # A


@dataclass(frozen=True, kw_only=True)
class WattCycleBinaryDescription(BinarySensorEntityDescription):
    """Describes a WattCycle binary sensor."""

    value_fn: Callable[[BatteryState], bool | None]


BINARY_SENSORS: tuple[WattCycleBinaryDescription, ...] = (
    WattCycleBinaryDescription(
        key="charging",
        translation_key="charging",
        device_class=BinarySensorDeviceClass.BATTERY_CHARGING,
        value_fn=lambda s: (s.current > CURRENT_DEADBAND) if s.current is not None else None,
    ),
    WattCycleBinaryDescription(
        key="discharging",
        translation_key="discharging",
        value_fn=lambda s: (s.current < -CURRENT_DEADBAND) if s.current is not None else None,
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattCycleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WattCycle binary sensors."""
    coordinator = entry.runtime_data
    async_add_entities(
        WattCycleBinarySensor(coordinator, desc) for desc in BINARY_SENSORS
    )


class WattCycleBinarySensor(WattCycleEntity, BinarySensorEntity):
    """A binary sensor derived from BatteryState."""

    entity_description: WattCycleBinaryDescription

    def __init__(
        self, coordinator: WattCycleCoordinator, description: WattCycleBinaryDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def is_on(self) -> bool | None:
        return self.entity_description.value_fn(self.coordinator.data)
