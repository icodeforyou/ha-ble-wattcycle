"""Button platform for WattCycle BLE — BMS restart.

Ports the WattCycle app's "Reboot system" (JBD write 0x0E [0x81 0x18]). The app manual
describes it as a soft reboot that clears temporary alarms; on this pack a cell-overvoltage
protection that latched at the end of a charge is the expected use. Loads fed only by the
battery lose power briefly while the BMS restarts — including a Bluetooth proxy powered from
the leisure battery.
"""

from __future__ import annotations

import asyncio
import logging

from bleak.exc import BleakError

from homeassistant.components.button import (
    ButtonDeviceClass,
    ButtonEntity,
    ButtonEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import WattCycleConfigEntry
from .const import RESTART_CONFIRM_ATTEMPTS, RESTART_REFRESH_DELAY
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
    """Send the restart and confirm it through the BMS clock.

    The DISCOVER 314Ah reboots immediately without acknowledging, so a timeout on the ack is
    the normal outcome there. After a timeout the pack is polled again once it is back; if its
    clock shows the time of day reset (restart detected by the coordinator), the restart
    succeeded. Only if the clock kept running is the command reported as ignored.
    """
    conn = coordinator.connection
    restarts_before = conn.bms_restart_count
    timed_out = False
    try:
        ack = await conn.async_restart_bms()
    except TimeoutError:
        timed_out = True
    except (ValueError, OSError, BleakError) as err:
        raise HomeAssistantError(f"BMS restart failed: {type(err).__name__}: {err}") from err
    else:
        if not ack.ok:
            raise HomeAssistantError(f"BMS refused the restart: {ack.error}")
        _LOGGER.info("BMS acknowledged restart")

    # Give the BMS time to come back, then let the coordinator read the clock.
    await asyncio.sleep(RESTART_REFRESH_DELAY)
    for _ in range(RESTART_CONFIRM_ATTEMPTS):
        await coordinator.async_refresh()
        if conn.bms_restart_count > restarts_before:
            _LOGGER.info(
                "BMS restart confirmed by its clock at %s", conn.bms_last_restart
            )
            return
        if not coordinator.last_update_success:
            await asyncio.sleep(RESTART_REFRESH_DELAY)
            continue
        break

    if timed_out:
        raise HomeAssistantError(
            "BMS restart: the frame was sent but the BMS neither acknowledged it nor reset its "
            "clock — the command appears to have been ignored."
        )
    _LOGGER.warning("BMS acknowledged the restart but its clock did not reset; check manually")
