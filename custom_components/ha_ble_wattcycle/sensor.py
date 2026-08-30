"""Sensor platform for WattCycle BLE."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfPower,
    UnitOfTemperature,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WattCycleConfigEntry
from .coordinator import WattCycleCoordinator
from .entity import WattCycleEntity
from .protocol import BatteryState

AH = "Ah"


@dataclass(frozen=True, kw_only=True)
class WattCycleSensorDescription(SensorEntityDescription):
    """Describes a WattCycle sensor and how to read it off BatteryState."""

    value_fn: Callable[[BatteryState], float | int | None]
    exists_fn: Callable[[BatteryState], bool] = lambda _state: True


SENSORS: tuple[WattCycleSensorDescription, ...] = (
    WattCycleSensorDescription(
        key="voltage",
        translation_key="voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.voltage,
    ),
    WattCycleSensorDescription(
        key="current",
        translation_key="current",
        device_class=SensorDeviceClass.CURRENT,
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=2,
        value_fn=lambda s: s.current,
    ),
    WattCycleSensorDescription(
        key="power",
        translation_key="power",
        device_class=SensorDeviceClass.POWER,
        native_unit_of_measurement=UnitOfPower.WATT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.power,
    ),
    WattCycleSensorDescription(
        key="soc",
        translation_key="soc",
        device_class=SensorDeviceClass.BATTERY,
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        value_fn=lambda s: s.soc,
    ),
    WattCycleSensorDescription(
        key="soh",
        translation_key="soh",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.soh,
        exists_fn=lambda s: s.soh is not None,
    ),
    WattCycleSensorDescription(
        key="remaining_capacity",
        translation_key="remaining_capacity",
        native_unit_of_measurement=AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.remaining_capacity,
    ),
    WattCycleSensorDescription(
        key="total_capacity",
        translation_key="total_capacity",
        native_unit_of_measurement=AH,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.total_capacity,
    ),
    WattCycleSensorDescription(
        key="design_capacity",
        translation_key="design_capacity",
        native_unit_of_measurement=AH,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.design_capacity,
    ),
    WattCycleSensorDescription(
        key="cycles",
        translation_key="cycles",
        state_class=SensorStateClass.TOTAL_INCREASING,
        value_fn=lambda s: s.cycles,
    ),
    WattCycleSensorDescription(
        key="mos_temperature",
        translation_key="mos_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        value_fn=lambda s: s.mos_temperature,
        exists_fn=lambda s: s.mos_temperature is not None,
    ),
    WattCycleSensorDescription(
        key="pcb_temperature",
        translation_key="pcb_temperature",
        device_class=SensorDeviceClass.TEMPERATURE,
        native_unit_of_measurement=UnitOfTemperature.CELSIUS,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=1,
        entity_registry_enabled_default=False,
        value_fn=lambda s: s.pcb_temperature,
        exists_fn=lambda s: s.pcb_temperature is not None,
    ),
    WattCycleSensorDescription(
        key="cell_voltage_delta",
        translation_key="cell_voltage_delta",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.cell_voltage_delta,
        exists_fn=lambda s: bool(s.cell_voltages),
    ),
    WattCycleSensorDescription(
        key="min_cell_voltage",
        translation_key="min_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.min_cell_voltage,
        exists_fn=lambda s: bool(s.cell_voltages),
    ),
    WattCycleSensorDescription(
        key="max_cell_voltage",
        translation_key="max_cell_voltage",
        device_class=SensorDeviceClass.VOLTAGE,
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        state_class=SensorStateClass.MEASUREMENT,
        suggested_display_precision=3,
        value_fn=lambda s: s.max_cell_voltage,
        exists_fn=lambda s: bool(s.cell_voltages),
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattCycleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up WattCycle sensors."""
    coordinator = entry.runtime_data
    state = coordinator.data
    entities: list[SensorEntity] = [
        WattCycleSensor(coordinator, desc)
        for desc in SENSORS
        if desc.exists_fn(state)
    ]
    # One sensor per detected cell and per temperature probe.
    cell_count = len(state.cell_voltages)
    entities.extend(
        WattCycleCellSensor(coordinator, index) for index in range(cell_count)
    )
    entities.extend(
        WattCycleTempSensor(coordinator, index)
        for index in range(len(state.cell_temperatures))
    )
    async_add_entities(entities)


class WattCycleSensor(WattCycleEntity, SensorEntity):
    """A single scalar sensor derived from BatteryState."""

    entity_description: WattCycleSensorDescription

    def __init__(
        self, coordinator: WattCycleCoordinator, description: WattCycleSensorDescription
    ) -> None:
        super().__init__(coordinator, description.key)
        self.entity_description = description

    @property
    def native_value(self) -> float | int | None:
        return self.entity_description.value_fn(self.coordinator.data)


class WattCycleCellSensor(WattCycleEntity, SensorEntity):
    """Voltage of an individual cell."""

    _attr_device_class = SensorDeviceClass.VOLTAGE
    _attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 3

    def __init__(self, coordinator: WattCycleCoordinator, index: int) -> None:
        super().__init__(coordinator, f"cell_{index + 1}_voltage")
        self._index = index
        self._attr_translation_key = "cell_voltage"
        self._attr_translation_placeholders = {"number": str(index + 1)}

    @property
    def native_value(self) -> float | None:
        cells = self.coordinator.data.cell_voltages
        return cells[self._index] if self._index < len(cells) else None


class WattCycleTempSensor(WattCycleEntity, SensorEntity):
    """One NTC temperature probe."""

    _attr_device_class = SensorDeviceClass.TEMPERATURE
    _attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_suggested_display_precision = 1

    def __init__(self, coordinator: WattCycleCoordinator, index: int) -> None:
        super().__init__(coordinator, f"temperature_{index + 1}")
        self._index = index
        self._attr_translation_key = "ntc_temperature"
        self._attr_translation_placeholders = {"number": str(index + 1)}

    @property
    def native_value(self) -> float | None:
        temps = self.coordinator.data.cell_temperatures
        return temps[self._index] if self._index < len(temps) else None
