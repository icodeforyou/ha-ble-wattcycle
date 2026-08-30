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
    PROBE_TIMEOUT,
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
    watt_analog_probe_frames,
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
        self._auth_unavailable = False
        self._firmware_version: int | None = None
        # The analog-read request variant this device answers (learned by probing).
        self._watt_frame: bytes | None = None
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

        # The configured device type is a guess from the advertisement; the GATT table is
        # authoritative (the vendor app does the same). "WT*" names exist in both families.
        detected = self._detect_type_from_gatt(client)
        if detected is not None and detected is not self._device_type:
            _LOGGER.warning(
                "%s: GATT services indicate %s protocol (configured as %s) — switching",
                self._address,
                detected.value,
                self._device_type.value,
            )
            self._device_type = detected
            self._uuids = UUIDS[detected]

        # Proactive pairing handles GATT status 5 (insufficient authentication).
        # Not all backends/proxies support it; failure here is non-fatal.
        try:
            await asyncio.wait_for(client.pair(), timeout=CONNECT_TIMEOUT)
        except (BleakError, NotImplementedError, asyncio.TimeoutError, EOFError) as err:
            _LOGGER.debug("pair() skipped for %s: %s", self._address, err)

        try:
            await client.start_notify(self._uuids["notify"], self._on_notify)
        except BleakError as err:
            available = [service.uuid for service in client.services]
            raise UpdateFailed(
                f"Failed to subscribe to {self._uuids['notify']} on {self._address}: {err!r}. "
                f"Available services: {available}"
            ) from err
        await self._maybe_auth()

    def _detect_type_from_gatt(self, client: BleakClientWithServiceCache) -> DeviceType | None:
        """Pick the protocol family from the services the device actually exposes."""
        service_uuids = {service.uuid.lower() for service in client.services}
        for device_type in (DeviceType.WATT, DeviceType.JBD):
            if UUIDS[device_type]["service"] in service_uuids:
                return device_type
        return None

    async def _maybe_auth(self) -> None:
        """Write the HiLink key to the WATT auth characteristic if configured."""
        auth_uuid = self._uuids.get("auth")
        if not (self._use_hilink_auth and auth_uuid) or self._client is None:
            return
        await self._force_auth()

    async def _force_auth(self) -> bool:
        """Write the HiLink key to the auth characteristic. Returns True on success."""
        auth_uuid = self._uuids.get("auth")
        if not auth_uuid or self._client is None or self._auth_unavailable:
            return False
        try:
            await self._client.write_gatt_char(auth_uuid, HILINK_AUTH_KEY, response=True)
            self._authed = True
            _LOGGER.debug("Sent HiLink auth key to %s", self._address)
            return True
        except BleakError as err:
            # A missing characteristic will not appear later — remember and stop trying,
            # so this cannot repeat every poll cycle.
            self._auth_unavailable = True
            _LOGGER.info(
                "HiLink auth not available on %s (%s); will not retry", self._address, err
            )
            return False

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

    async def _request(
        self, frame: bytes, timeout: float = COMMAND_TIMEOUT
    ) -> BatteryState | list[float]:
        assert self._client is not None
        fut: asyncio.Future = self._hass.loop.create_future()
        self._waiters.append(fut)
        self.last_tx = [frame.hex()] + self.last_tx[:4]
        try:
            await self._client.write_gatt_char(self._uuids["write"], frame, response=True)
            return await asyncio.wait_for(fut, timeout=timeout)
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
        # Fast path: we already know which request variant this device answers.
        if self._watt_frame is not None:
            try:
                result = await self._request(self._watt_frame)
                assert isinstance(result, BatteryState)
                return result
            except asyncio.TimeoutError:
                _LOGGER.debug("%s: known frame variant stopped answering; re-probing", self._address)
                self._watt_frame = None

        try:
            return await self._probe_watt_variants()
        except asyncio.TimeoutError:
            # Last resort: some modules gate the data path behind the HiLink handshake.
            if not self._authed and not self._auth_unavailable and await self._force_auth():
                _LOGGER.warning(
                    "%s: no telemetry response; sent HiLink auth and re-probing", self._address
                )
                return await self._probe_watt_variants()
            raise

    async def _probe_watt_variants(self) -> BatteryState:
        """Try each analog-read request variant (0x7E/0x1E, ±infoData) until one answers.

        Mirrors the app's detectProductHeader: some devices only respond to frame head
        0x1E; newer firmware wants the infoData block. Responses always start with 0x7E.
        """
        for label, frame in watt_analog_probe_frames():
            try:
                result = await self._request(frame, timeout=PROBE_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug("%s: no answer to analog read (%s)", self._address, label)
                continue
            assert isinstance(result, BatteryState)
            self._watt_frame = frame
            _LOGGER.info("%s answers analog read variant: %s", self._address, label)
            return result
        raise asyncio.TimeoutError(
            f"{self._address}: no response to any analog-read variant (0x7E/0x1E, ±infoData)"
        )

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
            detail = str(err) or type(err).__name__
            raise UpdateFailed(f"Error polling {self.entry.title}: {detail}") from err

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.connection.async_disconnect()
