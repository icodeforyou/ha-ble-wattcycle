"""Connection management and DataUpdateCoordinator for WattCycle BLE batteries.

Holds a single BLE connection open (through an ESPHome Bluetooth proxy or a local adapter),
subscribes to notifications, and polls telemetry. The read path (telemetry) is treated as safe.
The write path (send_raw) is unverified and dangerous — see docs/TESTPLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    COMMAND_TIMEOUT,
    CONNECT_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    MAX_CONNECT_ATTEMPTS,
)
from .protocol import (
    HILINK_AUTH_KEY,
    JBD_CMD_BASIC_INFO,
    JBD_CMD_CELL_VOLTAGES,
    JBD_END,
    JBD_START,
    UUIDS,
    WATT_HEAD,
    BatteryState,
    DeviceType,
    jbd_build_read_frame,
    jbd_parse_basic_info,
    jbd_parse_cell_voltages,
    watt_analog_read_frame,
    watt_decode_analog_quantity,
    watt_expected_length,
    watt_parse_frame,
)

_LOGGER = logging.getLogger(__name__)


class WattCycleConnection:
    """Owns the persistent BLE link and turns read commands into decoded telemetry."""

    def __init__(
        self,
        hass: HomeAssistant,
        address: str,
        device_type: DeviceType,
        use_hilink_auth: bool,
    ) -> None:
        self._hass = hass
        self._address = address
        self._device_type = device_type
        self._use_hilink_auth = use_hilink_auth
        self._uuids = UUIDS[device_type]
        self._client: BleakClientWithServiceCache | None = None
        self._lock = asyncio.Lock()
        self._rx = bytearray()
        self._authed = False
        self._firmware_version: int | None = None
        # Pending single-frame waiter (register -> future) for request/response.
        self._waiters: list[asyncio.Future[BatteryState | list[float]]] = []
        # Rolling capture of raw frames for diagnostics (hex strings).
        self.last_tx: list[str] = []
        self.last_rx: list[str] = []

    @property
    def firmware_version(self) -> int | None:
        return self._firmware_version

    @property
    def connected(self) -> bool:
        return self._client is not None and self._client.is_connected

    async def _ensure_connected(self) -> None:
        if self.connected:
            return
        ble_device = bluetooth.async_ble_device_from_address(
            self._hass, self._address, connectable=True
        )
        if ble_device is None:
            raise UpdateFailed(f"WattCycle {self._address} not found by any adapter/proxy")

        self._client = await establish_connection(
            BleakClientWithServiceCache,
            ble_device,
            self._address,
            max_attempts=MAX_CONNECT_ATTEMPTS,
            timeout=CONNECT_TIMEOUT,
        )
        client = self._client
        assert client is not None
        self._rx.clear()
        self._authed = False

        # Proactive pairing handles GATT status 5 (insufficient authentication).
        # Not all backends/proxies support it; failure here is non-fatal.
        try:
            await asyncio.wait_for(client.pair(), timeout=CONNECT_TIMEOUT)
        except (BleakError, NotImplementedError, asyncio.TimeoutError, EOFError) as err:
            _LOGGER.debug("pair() skipped for %s: %s", self._address, err)

        await client.start_notify(self._uuids["notify"], self._on_notify)
        await self._maybe_auth()

    async def _maybe_auth(self) -> None:
        """Write the HiLink key to the WATT auth characteristic if configured."""
        auth_uuid = self._uuids.get("auth")
        if not (self._use_hilink_auth and auth_uuid) or self._client is None:
            return
        try:
            await self._client.write_gatt_char(auth_uuid, HILINK_AUTH_KEY, response=True)
            self._authed = True
            _LOGGER.debug("Sent HiLink auth key to %s", self._address)
        except BleakError as err:
            _LOGGER.warning("HiLink auth write failed for %s: %s", self._address, err)

    def _on_notify(self, _char: object, data: bytearray) -> None:
        self.last_rx = [bytes(data).hex()] + self.last_rx[:4]
        self._rx += data
        if self._device_type is DeviceType.WATT:
            self._consume_watt()
        else:
            self._consume_jbd()

    def _resolve(self, value: BatteryState | list[float]) -> None:
        for fut in self._waiters:
            if not fut.done():
                fut.set_result(value)
                break

    def _consume_watt(self) -> None:
        while True:
            start = self._rx.find(bytes([WATT_HEAD]))
            if start < 0:
                self._rx.clear()
                return
            if start:
                del self._rx[:start]
            if len(self._rx) < 8:
                return
            total = watt_expected_length(bytes(self._rx))
            if total is None or len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            parsed = watt_parse_frame(frame)
            if parsed is None:
                continue
            self._firmware_version = parsed.version
            try:
                state = watt_decode_analog_quantity(parsed.payload)
            except (IndexError, ValueError):
                _LOGGER.debug("Failed to decode WATT analog payload: %s", frame.hex())
                continue
            state.firmware_version = parsed.version
            self._resolve(state)

    def _consume_jbd(self) -> None:
        while True:
            start = self._rx.find(bytes([JBD_START]))
            if start < 0:
                self._rx.clear()
                return
            if start:
                del self._rx[:start]
            if len(self._rx) < 4:
                return
            length = self._rx[3]
            total = length + 7  # DD cmd status len ... chk(2) 77
            if len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            if frame[-1] != JBD_END:
                continue
            cmd = frame[1]
            payload = frame[4 : 4 + length]
            if cmd == JBD_CMD_BASIC_INFO:
                self._resolve(jbd_parse_basic_info(payload))
            elif cmd == JBD_CMD_CELL_VOLTAGES:
                self._resolve(jbd_parse_cell_voltages(payload))

    async def _request(self, frame: bytes) -> BatteryState | list[float]:
        assert self._client is not None
        fut: asyncio.Future = self._hass.loop.create_future()
        self._waiters.append(fut)
        self.last_tx = [frame.hex()] + self.last_tx[:4]
        try:
            await self._client.write_gatt_char(self._uuids["write"], frame, response=True)
            return await asyncio.wait_for(fut, timeout=COMMAND_TIMEOUT)
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    async def async_poll(self) -> BatteryState:
        """Connect if needed and return a decoded telemetry snapshot."""
        async with self._lock:
            await self._ensure_connected()
            if self._device_type is DeviceType.WATT:
                return await self._poll_watt()
            return await self._poll_jbd()

    async def _poll_watt(self) -> BatteryState:
        frame = watt_analog_read_frame(self._firmware_version)
        result = await self._request(frame)
        assert isinstance(result, BatteryState)
        return result

    async def _poll_jbd(self) -> BatteryState:
        basic = await self._request(jbd_build_read_frame(JBD_CMD_BASIC_INFO))
        assert isinstance(basic, BatteryState)
        try:
            cells = await self._request(jbd_build_read_frame(JBD_CMD_CELL_VOLTAGES))
            if isinstance(cells, list):
                basic.cell_voltages = cells
                basic.cell_count = len(cells)
        except asyncio.TimeoutError:
            _LOGGER.debug("JBD cell-voltage read timed out; reporting basic info only")
        return basic

    async def async_write_raw(self, data: bytes) -> None:
        """Write a raw frame to the write characteristic. UNVERIFIED / DANGEROUS."""
        async with self._lock:
            await self._ensure_connected()
            assert self._client is not None
            await self._client.write_gatt_char(self._uuids["write"], data, response=True)

    async def async_disconnect(self) -> None:
        client = self._client
        self._client = None
        if client is not None:
            try:
                await client.disconnect()
            except BleakError as err:
                _LOGGER.debug("Error disconnecting %s: %s", self._address, err)


class WattCycleCoordinator(DataUpdateCoordinator[BatteryState]):
    """Polls a WattCycle battery and holds the connection open between updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        connection: WattCycleConnection,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} {entry.title}",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.entry = entry
        self.connection = connection

    async def _async_update_data(self) -> BatteryState:
        try:
            return await self.connection.async_poll()
        except (BleakError, asyncio.TimeoutError, EOFError) as err:
            # Drop the connection so the next cycle re-establishes cleanly.
            await self.connection.async_disconnect()
            raise UpdateFailed(f"Error polling {self.entry.title}: {err}") from err

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.connection.async_disconnect()
