"""Button platform for WattCycle BLE — BMS restart.

Ports the WattCycle app's "Reboot system" (JBD write 0x0E [0x81 0x18]). The app manual
describes it as a soft reboot that clears temporary alarms; on this pack a cell-overvoltage
protection that latched at the end of a charge is the expected use. Loads fed only by the
battery lose power briefly while the BMS restarts — including a Bluetooth proxy powered from
the leisure battery.
"""

from __future__ import annotations

import logging

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later

from . import WattCycleConfigEntry
from .const import RESTART_REFRESH_DELAY
from .coordinator import WattCycleCoordinator
from .entity import WattCycleEntity

_LOGGER = logging.getLogger(__name__)

RESTART_DESCRIPTION = ButtonEntityDescription(
    key="restart_bms",
    translation_key="restart_bms",
    device_class=ButtonDeviceClass.RESTART,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: WattCycleConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the restart button for JBD-protocol packs."""
    coordinator = entry.runtime_data
    if coordinator.connection.protocol_mode != "jbd":
        return
    async_add_entities([WattCycleRestartButton(coordinator)])


class WattCycleRestartButton(WattCycleEntity, ButtonEntity):
    """Soft-reboot the BMS."""

    entity_description = RESTART_DESCRIPTION

    def __init__(self, coordinator: WattCycleCoordinator) -> None:
        super().__init__(coordinator, RESTART_DESCRIPTION.key)

    async def async_press(self) -> None:
        await async_restart_bms(self.coordinator)


async def async_restart_bms(coordinator: WattCycleCoordinator) -> None:
    """Send the restart, surface the BMS's answer, and re-poll once it is back."""
    try:
        ack = await coordinator.connection.async_restart_bms()
    except (ValueError, TimeoutError, OSError) as err:
        raise HomeAssistantError(f"BMS restart failed: {err}") from err
    if not ack.ok:
        raise HomeAssistantError(f"BMS refused the restart: {ack.error}")
    _LOGGER.info("BMS acknowledged restart; re-polling in %ss", RESTART_REFRESH_DELAY)

    async def _refresh(_now) -> None:
        await coordinator.async_request_refresh()

    async_call_later(coordinator.hass, RESTART_REFRESH_DELAY, _refresh)
