"""Config flow for WattCycle BLE Battery."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.core import callback
from homeassistant.helpers import config_validation as cv

from .const import (
    CONF_ADDRESS,
    CONF_DEVICE_TYPE,
    CONF_SCAN_INTERVAL,
    CONF_USE_HILINK_AUTH,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .protocol import DeviceType, detect_device_type


def _device_type_from_service_info(info: BluetoothServiceInfoBleak) -> DeviceType | None:
    return detect_device_type(
        service_uuids=list(info.service_uuids),
        manufacturer_ids=list(info.manufacturer_data.keys()),
        name=info.name,
    )


class WattCycleConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for WattCycle BLE."""

    VERSION = 1

    def __init__(self) -> None:
        self._discovered_address: str | None = None
        self._discovered_name: str | None = None
        self._discovered_type: DeviceType | None = None
        self._discovered: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        """Handle a flow initialized by Bluetooth discovery."""
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovered_address = discovery_info.address
        self._discovered_name = discovery_info.name or discovery_info.address
        self._discovered_type = _device_type_from_service_info(discovery_info)
        self.context["title_placeholders"] = {"name": self._discovered_name}
        return await self.async_step_confirm()

    async def async_step_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm a single discovered device."""
        assert self._discovered_address is not None
        device_type = self._discovered_type or DeviceType.WATT
        if user_input is not None:
            return self.async_create_entry(
                title=self._discovered_name or self._discovered_address,
                data={
                    CONF_ADDRESS: self._discovered_address,
                    CONF_DEVICE_TYPE: device_type.value,
                    CONF_USE_HILINK_AUTH: user_input.get(CONF_USE_HILINK_AUTH, False),
                },
            )
        return self.async_show_form(
            step_id="confirm",
            data_schema=vol.Schema(
                {vol.Optional(CONF_USE_HILINK_AUTH, default=False): cv.boolean}
            ),
            description_placeholders={
                "name": self._discovered_name or self._discovered_address,
                "device_type": device_type.value,
            },
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Handle manual setup by picking a discovered device."""
        if user_input is not None:
            address = user_input[CONF_ADDRESS]
            await self.async_set_unique_id(address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            info = self._discovered[address]
            device_type = _device_type_from_service_info(info) or DeviceType.WATT
            return self.async_create_entry(
                title=info.name or address,
                data={
                    CONF_ADDRESS: address,
                    CONF_DEVICE_TYPE: device_type.value,
                    CONF_USE_HILINK_AUTH: user_input.get(CONF_USE_HILINK_AUTH, False),
                },
            )

        current = self._async_current_ids()
        for info in async_discovered_service_info(self.hass, connectable=True):
            if info.address in current:
                continue
            if _device_type_from_service_info(info) is not None:
                self._discovered[info.address] = info

        if not self._discovered:
            return self.async_abort(reason="no_devices_found")

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {
                            address: f"{info.name or address} ({address})"
                            for address, info in self._discovered.items()
                        }
                    ),
                    vol.Optional(CONF_USE_HILINK_AUTH, default=False): cv.boolean,
                }
            ),
        )

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: ConfigEntry) -> OptionsFlow:
        return WattCycleOptionsFlow()


class WattCycleOptionsFlow(OptionsFlow):
    """Handle options for WattCycle BLE."""

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current = self.config_entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_SCAN_INTERVAL, default=current): vol.All(
                        vol.Coerce(int), vol.Range(min=5, max=3600)
                    )
                }
            ),
        )
