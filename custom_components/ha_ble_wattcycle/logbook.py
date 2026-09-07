"""Describe BMS event-log records in the Home Assistant logbook."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.components.logbook import LOGBOOK_ENTRY_MESSAGE, LOGBOOK_ENTRY_NAME
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, EVENT_BMS_EVENT


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[[str, str, Callable[[Event], dict[str, str]]], None],
) -> None:
    """Register the BMS event description."""

    @callback
    def _describe(event: Event) -> dict[str, str]:
        data = event.data
        when = data.get("event_time")
        suffix = f" (BMS time {when})" if when else ""
        return {
            LOGBOOK_ENTRY_NAME: f"{data.get('device_name', 'WattCycle')} BMS",
            LOGBOOK_ENTRY_MESSAGE: f"logged: {data.get('summary', '')}{suffix}",
        }

    async_describe_event(DOMAIN, EVENT_BMS_EVENT, _describe)
