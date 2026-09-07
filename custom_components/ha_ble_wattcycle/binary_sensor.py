"""Binary sensor platform for WattCycle BLE.

Charging/discharging are derived from the sign of the pack current. Positive current is
charging — verified 2026-09-07 on a DISCOVER 12V 314Ah (the BMS Ah counter rose while the
current read +12 A on shore power).

The FET, balancing and protection sensors come straight from the JBD basic-info frame
(bytes 12-20) and are only created when the pack reports them. They answer *why* a charge or
discharge stopped: a charge FET that is off together with a tripped cell-overvoltage flag is
the BMS protecting itself, a charge FET that is on with zero current is the charger finishing.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WattCycleConfigEntry
from .coordinator import WattCycleCoordinator
from .entity import WattCycleEntity
from .protocol import JBD_PROTECTION_BITS, BatteryState

# Ignore tiny idle currents to avoid flapping.
CURRENT_DEADBAND = 0.2  # A


@dataclass(frozen=True, kw_only=True)
class WattCycleBinaryDescription(BinarySensorEntityDescription):
    """Describes a WattCycle binary sensor."""

    value_fn: Callable[[BatteryState], bool | None]
    exists_fn: Callable[[BatteryState], bool] = lambda _state: True
    attributes_fn: Callable[[BatteryState], dict[str, Any]] | None = None


def _has_jbd_status(state: BatteryState) -> bool:
    return state.protection_status is not None


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
    # --- JBD status: MOSFETs ------------------------------------------------------------
    WattCycleBinaryDescription(
        key="charge_fet",
        translation_key="charge_fet",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda s: s.charge_fet_on,
        exists_fn=lambda s: s.charge_fet_on is not None,
    ),
    WattCycleBinaryDescription(
        key="discharge_fet",
        translation_key="discharge_fet",
        device_class=BinarySensorDeviceClass.POWER,
        value_fn=lambda s: s.discharge_fet_on,
        exists_fn=lambda s: s.discharge_fet_on is not None,
    ),
    # --- JBD status: balancing -----------------------------------------------------------
    WattCycleBinaryDescription(
        key="balancing",
        translation_key="balancing",
        device_class=BinarySensorDeviceClass.RUNNING,
        value_fn=lambda s: bool(s.balance_status) if s.balance_status is not None else None,
        exists_fn=lambda s: s.balance_status is not None,
        attributes_fn=lambda s: {"cells": s.balancing_cells},
    ),
    WattCycleBinaryDescription(
        key="heating",
        translation_key="heating",
        device_class=BinarySensorDeviceClass.HEAT,
        value_fn=lambda s: s.heating_on,
        exists_fn=lambda s: s.heating_on is not None,
    ),
    # --- JBD status: advisory warnings (do not open the FETs). Bits 14-15 are undefined in
    # the app and bit 15 is permanently set on the DISCOVER 314Ah, so they are masked out.
    WattCycleBinaryDescription(
        key="warning",
        translation_key="warning",
        value_fn=lambda s: bool(s.warning_status & 0x3FFF) if s.warning_status is not None else None,
        exists_fn=lambda s: s.warning_status is not None,
        attributes_fn=lambda s: {"active": s.active_warnings, "raw": s.warning_status},
    ),
    # --- JBD status: any protection tripped -------------------------------------------
    WattCycleBinaryDescription(
        # Deliberately no PROBLEM device class: a tripped protection is the BMS doing its
        # job (e.g. cell overvoltage at end of charge), not a fault. Shows plain on/off.
        key="protection",
        translation_key="protection",
        value_fn=lambda s: bool(s.protection_status) if s.protection_status is not None else None,
        exists_fn=_has_jbd_status,
        attributes_fn=lambda s: {
            "active": s.active_protections,
            "raw": s.protection_status,
        },
    ),
) + tuple(
    # One diagnostic sensor per individual protection flag.
    WattCycleBinaryDescription(
        key=f"protection_{name}",
        translation_key=f"protection_{name}",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda s, _name=name: s.protection_active(_name),
        exists_fn=_has_jbd_status,
    )
    for name in JBD_PROTECTION_BITS
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattCycleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WattCycle binary sensors."""
    coordinator = entry.runtime_data
    state = coordinator.data
    async_add_entities(
        WattCycleBinarySensor(coordinator, desc)
        for desc in BINARY_SENSORS
        if desc.exists_fn(state)
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

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        fn = self.entity_description.attributes_fn
        return fn(self.coordinator.data) if fn else None
