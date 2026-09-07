"""Connection management and DataUpdateCoordinator for WattCycle BLE batteries.

Holds a single BLE connection open (through an ESPHome Bluetooth proxy or a local adapter),
subscribes to notifications, and polls telemetry. The read path (telemetry) is treated as safe.
The write path (send_raw) is unverified and dangerous — see docs/TESTPLAN.md.
"""

from __future__ import annotations

import asyncio
import logging
import struct
from datetime import datetime, timedelta, timezone

from bleak.exc import BleakError
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection
from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    COMMAND_TIMEOUT,
    CONF_PROTOCOL_MODE,
    CONNECT_TIMEOUT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    EVENT_RECORDS_PER_POLL,
    MAX_CONNECT_ATTEMPTS,
    MAX_EVENT_RECORDS,
    PAIR_TIMEOUT,
    POLL_TIMEOUT,
    PROBE_TIMEOUT,
)
from .protocol import (
    BMC_CMD_BATTERY_INFO,
    BMC_CMD_CELL_VOLTAGE,
    BMC_CMD_HANDSHAKE,
    BMC_HEADER,
    HILINK_AUTH_KEY,
    JBD_CMD_BASIC_INFO,
    JBD_CMD_CELL_VOLTAGES,
    JBD_CMD_RECORD_CURRENT,
    JBD_CMD_RECORD_TOTAL,
    JBD_CMD_RESTART_SYSTEM,
    JBD_CMD_SYSTEM_TIME,
    JBD_END,
    JBD_START,
    UUIDS,
    WATT_HEAD,
    BatteryState,
    DeviceType,
    FaultRecord,
    JbdAck,
    JbdClock,
    JbdRecordInfo,
    bmc_build_frame,
    bmc_decode_battery_info,
    bmc_decode_cell_voltages,
    bmc_expected_length,
    bmc_parse_frame,
    jbd_build_read_frame,
    jbd_build_restart_frame,
    jbd_parse_basic_info,
    jbd_parse_cell_voltages,
    jbd_parse_clock,
    jbd_parse_fault_record,
    jbd_parse_record_info,
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
        protocol_hint: str | None = None,
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
        self._write_with_response = True
        self._firmware_version: int | None = None
        # Wire protocol actually spoken over the link ("watt"/"jbd"/"bmc"). The GATT
        # service picks the characteristics; the protocol is confirmed by probing.
        # A persisted hint from an earlier discovery skips the probe ladder entirely.
        if protocol_hint in ("watt", "jbd", "bmc"):
            self._protocol_mode = protocol_hint
            self._mode_locked = True
        else:
            self._protocol_mode = device_type.value
            self._mode_locked = False
        self._bmc_handshaken = False
        # The analog-read request variant this device answers (learned by probing).
        self._watt_frame: bytes | None = None
        # Pending single-frame waiter (register -> future) for request/response.
        self._waiters: list[asyncio.Future[BatteryState | list[float] | bool]] = []
        # Rolling capture of raw frames for diagnostics (hex strings).
        self.last_tx: list[str] = []
        self.last_rx: list[str] = []
        self.last_ack: JbdAck | None = None
        # BMS record log (JBD 0x07/0x08) and the "system time" (0x06) read alongside it.
        self.record_info: JbdRecordInfo | None = None
        self.events: list[FaultRecord] = []
        self.bms_clock: JbdClock | None = None
        self.bms_clock_read_at: datetime | None = None
        self.bms_restart_count = 0  # clock went backwards since HA started
        self.bms_last_restart: datetime | None = None  # wall-clock, set when a restart is seen
        self._event_log_supported: bool | None = None

    @property
    def firmware_version(self) -> int | None:
        return self._firmware_version

    @property
    def protocol_mode(self) -> str | None:
        """The confirmed wire protocol, or None while still probing."""
        return self._protocol_mode if self._mode_locked else None

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
        self._bmc_handshaken = False

        # Log the full GATT table once per connect — the single most useful diagnostic
        # for unknown module revisions. Silence via the quiet_logging option if noisy.
        for service in client.services:
            chars = ", ".join(
                f"{char.uuid}[{'|'.join(char.properties)}]"
                for char in service.characteristics
            )
            _LOGGER.info("%s GATT service %s: %s", self._address, service.uuid, chars)

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
            if not self._mode_locked:
                self._protocol_mode = detected.value

        # Proactive pairing handles GATT status 5 (insufficient authentication).
        # Not all backends/proxies support it; failure here is non-fatal.
        try:
            await asyncio.wait_for(client.pair(), timeout=PAIR_TIMEOUT)
        except (BleakError, NotImplementedError, asyncio.TimeoutError, EOFError) as err:
            _LOGGER.debug("pair() skipped for %s: %s", self._address, err)

        # Use the write type the characteristic actually supports; some modules only
        # accept write-without-response on the command characteristic.
        try:
            write_char = client.services.get_characteristic(self._uuids["write"])
            self._write_with_response = bool(
                write_char is None or "write" in write_char.properties
            )
        except (BleakError, AttributeError):
            self._write_with_response = True
        _LOGGER.debug(
            "%s write characteristic uses response=%s",
            self._address,
            self._write_with_response,
        )

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
            await self._client.write_gatt_char(
                auth_uuid, HILINK_AUTH_KEY, response=self._write_with_response
            )
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
        if self._protocol_mode == "bmc":
            self._consume_bmc()
        elif self._protocol_mode == "jbd":
            self._consume_jbd()
        else:
            self._consume_watt()

    def _resolve(self, value: BatteryState | list[float] | bool) -> None:
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
            status = frame[2]
            payload = frame[4 : 4 + length]
            if status == 0 and cmd == JBD_CMD_BASIC_INFO:
                self._resolve(jbd_parse_basic_info(payload))
            elif status == 0 and cmd == JBD_CMD_CELL_VOLTAGES:
                self._resolve(jbd_parse_cell_voltages(payload))
            elif status == 0 and cmd == JBD_CMD_RECORD_TOTAL:
                info = jbd_parse_record_info(payload)
                self._resolve(info if info is not None else JbdAck(cmd, 0xFF))
            elif status == 0 and cmd == JBD_CMD_SYSTEM_TIME:
                clock = jbd_parse_clock(payload)
                self._resolve(clock if clock is not None else JbdAck(cmd, 0xFF))
            elif status == 0 and cmd == JBD_CMD_RECORD_CURRENT:
                record = jbd_parse_fault_record(payload)
                self._resolve(record if record is not None else JbdAck(cmd, 0xFF))
            else:
                # Write acknowledgement, or a read the BMS rejected (status != 0).
                ack = JbdAck(cmd, status)
                self.last_ack = ack
                _LOGGER.debug("JBD ack for 0x%02x: %s", cmd, ack.error)
                self._resolve(ack)

    def _consume_bmc(self) -> None:
        while True:
            start = self._rx.find(bytes([BMC_HEADER]))
            if start < 0:
                self._rx.clear()
                return
            if start:
                del self._rx[:start]
            total = bmc_expected_length(bytes(self._rx))
            if total is None or len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            parsed = bmc_parse_frame(frame)
            if parsed is None:
                # Bad checksum/garbage: skip this header byte and rescan.
                continue
            cmd, payload = parsed
            try:
                if cmd == BMC_CMD_HANDSHAKE:
                    self._resolve(True)
                elif cmd == BMC_CMD_BATTERY_INFO:
                    self._resolve(bmc_decode_battery_info(payload))
                elif cmd == BMC_CMD_CELL_VOLTAGE:
                    self._resolve(bmc_decode_cell_voltages(payload))
            except (IndexError, ValueError, struct.error):
                _LOGGER.debug("Failed to decode BMC frame: %s", frame.hex())

    async def _request(
        self, frame: bytes, timeout: float = COMMAND_TIMEOUT
    ) -> BatteryState | list[float] | bool:
        assert self._client is not None
        fut: asyncio.Future = self._hass.loop.create_future()
        self._waiters.append(fut)
        self.last_tx = [frame.hex()] + self.last_tx[:4]
        try:
            await self._client.write_gatt_char(
                self._uuids["write"], frame, response=self._write_with_response
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            if fut in self._waiters:
                self._waiters.remove(fut)

    async def async_poll(self) -> BatteryState:
        """Connect if needed and return a decoded telemetry snapshot."""
        async with self._lock:
            await self._ensure_connected()
            if self._protocol_mode == "jbd":
                return await self._poll_jbd()
            if self._protocol_mode == "bmc":
                return await self._poll_bmc()
            return await self._poll_watt()

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
            # Some modules gate the data path behind the HiLink handshake.
            if not self._authed and not self._auth_unavailable and await self._force_auth():
                _LOGGER.warning(
                    "%s: no telemetry response; sent HiLink auth and re-probing", self._address
                )
                try:
                    return await self._probe_watt_variants()
                except asyncio.TimeoutError:
                    pass
            # Last resort: the fff0 service may carry a different wire protocol
            # (JBD-behind-UART or the new BMC protocol) — probe those too.
            return await self._probe_other_protocols()

    async def _probe_other_protocols(self) -> BatteryState:
        """Probe JBD and BMC wire protocols over the current characteristics."""
        self._protocol_mode = "jbd"
        self._rx.clear()
        try:
            result = await self._request(
                jbd_build_read_frame(JBD_CMD_BASIC_INFO), timeout=PROBE_TIMEOUT
            )
            if isinstance(result, BatteryState):
                self._mode_locked = True
                _LOGGER.warning(
                    "%s speaks the JBD protocol over %s — locking JBD mode",
                    self._address,
                    self._uuids["write"],
                )
                return await self._poll_jbd_extras(result)
        except asyncio.TimeoutError:
            _LOGGER.debug("%s: no answer to JBD basic-info probe", self._address)

        self._protocol_mode = "bmc"
        self._rx.clear()
        try:
            await self._request(bmc_build_frame(BMC_CMD_HANDSHAKE), timeout=PROBE_TIMEOUT)
            self._bmc_handshaken = True
            self._mode_locked = True
            _LOGGER.warning(
                "%s answered the BMC handshake — locking BMC mode", self._address
            )
            return await self._poll_bmc()
        except asyncio.TimeoutError:
            _LOGGER.debug("%s: no answer to BMC handshake; trying direct read", self._address)
        try:
            result = await self._request(
                bmc_build_frame(BMC_CMD_BATTERY_INFO), timeout=PROBE_TIMEOUT
            )
            if isinstance(result, BatteryState):
                _LOGGER.warning(
                    "%s answered a BMC battery-info read — locking BMC mode", self._address
                )
                self._bmc_handshaken = True
                self._mode_locked = True
                return result
        except asyncio.TimeoutError:
            pass

        self._protocol_mode = DeviceType.WATT.value
        self._rx.clear()
        raise asyncio.TimeoutError(
            f"{self._address}: no response to any protocol probe "
            "(WATT 0x7E/0x1E ±infoData, JBD, BMC)"
        )

    async def _poll_bmc(self) -> BatteryState:
        if not self._bmc_handshaken:
            try:
                await self._request(bmc_build_frame(BMC_CMD_HANDSHAKE), timeout=PROBE_TIMEOUT)
            except asyncio.TimeoutError:
                _LOGGER.debug("%s: BMC handshake unanswered; continuing", self._address)
            self._bmc_handshaken = True
        state = await self._request(bmc_build_frame(BMC_CMD_BATTERY_INFO))
        assert isinstance(state, BatteryState)
        try:
            cells = await self._request(
                bmc_build_frame(BMC_CMD_CELL_VOLTAGE), timeout=PROBE_TIMEOUT
            )
            if isinstance(cells, list):
                state.cell_voltages = cells
                state.cell_count = len(cells)
        except asyncio.TimeoutError:
            _LOGGER.debug("%s: BMC cell-voltage read timed out", self._address)
        return state

    async def _poll_jbd_extras(self, basic: BatteryState) -> BatteryState:
        """Augment a JBD basic-info result with cell voltages (best effort)."""
        try:
            cells = await self._request(
                jbd_build_read_frame(JBD_CMD_CELL_VOLTAGES), timeout=PROBE_TIMEOUT
            )
            if isinstance(cells, list):
                basic.cell_voltages = cells
                basic.cell_count = len(cells)
        except asyncio.TimeoutError:
            pass
        return basic

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
            self._protocol_mode = "watt"
            self._mode_locked = True
            _LOGGER.info("%s answers analog read variant: %s", self._address, label)
            return result
        raise asyncio.TimeoutError(
            f"{self._address}: no response to any analog-read variant (0x7E/0x1E, ±infoData)"
        )

    @property
    def event_count(self) -> int | None:
        """Records written according to 0x07 (index field). Meaning unverified."""
        return self.record_info.index if self.record_info else None

    async def _refresh_event_log(self) -> None:
        """Read 0x07 and 0x06 every poll, then EVENT_RECORDS_PER_POLL records via 0x08.

        Field observations on the DISCOVER 314Ah (2026-09-07): 0x07 answers two u16
        (226, 300 — index and ring size?), 0x06 answers six bytes of unknown format, each 0x08
        round trip takes ~5 s and 0x07 resets the record cursor (header byte 3 restarts at 2).
        Records are deduplicated on their raw header and kept newest-first. Experimental.
        """
        if self._event_log_supported is False:
            return
        result = await self._request(jbd_build_read_frame(JBD_CMD_RECORD_TOTAL))
        if isinstance(result, JbdAck):
            _LOGGER.info(
                "%s does not answer the record-log count (%s); disabling", self._address, result.error
            )
            self._event_log_supported = False
            return
        if not isinstance(result, JbdRecordInfo):
            return
        self._event_log_supported = True
        self.record_info = result
        clock = await self._request(jbd_build_read_frame(JBD_CMD_SYSTEM_TIME))
        if isinstance(clock, JbdClock):
            read_at = datetime.now(timezone.utc)
            prev = self.bms_clock.elapsed if self.bms_clock else None
            now_el = clock.elapsed
            if prev is not None and now_el is not None and now_el < prev:
                self.bms_restart_count += 1
                since = clock.time_since_restart or timedelta(0)
                self.bms_last_restart = (read_at - since).replace(microsecond=0)
                _LOGGER.warning(
                    "%s BMS restarted at about %s (clock %s -> %s)",
                    self._address, self.bms_last_restart.isoformat(), prev, now_el,
                )
            self.bms_clock = clock
            self.bms_clock_read_at = read_at
        seen = {rec.key for rec in self.events}
        for _ in range(EVENT_RECORDS_PER_POLL):
            rec = await self._request(jbd_build_read_frame(JBD_CMD_RECORD_CURRENT))
            if not isinstance(rec, FaultRecord):
                _LOGGER.debug("%s record read stopped: %r", self._address, rec)
                break
            if rec.key in seen:
                continue
            seen.add(rec.key)
            self.events.insert(0, rec)
        del self.events[MAX_EVENT_RECORDS:]

    @property
    def bms_first_power_estimate(self) -> datetime | None:
        """Approximate first power-on: read time minus the day counter and time of day.

        Only exact if the BMS never restarted (a restart resets the time of day but not the
        day counter), so treat as ±1 day. Rounded to the minute.
        """
        if self.bms_clock is None or self.bms_clock_read_at is None:
            return None
        el = self.bms_clock.elapsed
        if el is None:
            return None
        return (self.bms_clock_read_at - el).replace(second=0, microsecond=0)

    def event_time(self, record: FaultRecord) -> datetime | None:
        """Wall-clock time of a record: read time minus its age on the BMS clock."""
        if self.bms_clock is None or self.bms_clock_read_at is None:
            return None
        clock_dt = self.bms_clock.bms_datetime
        if clock_dt is None:
            return None
        rec_dt = record.bms_datetime(clock_dt.month)
        if rec_dt is None or rec_dt > clock_dt:
            # Records carry the same day counter + time of day; a record "in the future"
            # predates a BMS restart (time of day reset) and cannot be placed reliably.
            return None
        return self.bms_clock_read_at - (clock_dt - rec_dt)

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
        try:
            await self._refresh_event_log()
        except asyncio.TimeoutError:
            _LOGGER.debug("JBD event-log read timed out; keeping previous log")
        return basic

    async def async_restart_bms(self) -> JbdAck:
        """Soft-reboot the BMS (the app's 'Reboot system'). JBD packs only.

        The BMS answers with an ack and then drops the link while it restarts, so the
        connection is torn down afterwards and the next poll reconnects. Expect every
        12 V load fed only by the battery to lose power for a moment.
        """
        if self._protocol_mode != "jbd":
            raise ValueError("BMS restart is only known for JBD-protocol packs")
        async with self._lock:
            await self._ensure_connected()
            frame = jbd_build_restart_frame()
            _LOGGER.info("Sending BMS restart to %s: %s", self._address, frame.hex())
            try:
                result = await self._request(frame)
            except asyncio.TimeoutError:
                _LOGGER.warning(
                    "%s: no ack to restart within timeout; rx seen since: %s",
                    self._address, self.last_rx[:3],
                )
                raise
            finally:
                # Whatever happened, the BMS is about to (or did) drop us.
                await self.async_disconnect()
        if not isinstance(result, JbdAck) or result.command != JBD_CMD_RESTART_SYSTEM:
            raise ValueError(f"Unexpected reply to restart: {result!r}")
        return result

    async def async_write_raw(self, data: bytes) -> None:
        """Write a raw frame to the write characteristic. UNVERIFIED / DANGEROUS."""
        async with self._lock:
            await self._ensure_connected()
            assert self._client is not None
            await self._client.write_gatt_char(
                self._uuids["write"], data, response=self._write_with_response
            )

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
        self._seen_events: set[tuple[int, int, int]] | None = None

    async def _async_update_data(self) -> BatteryState:
        try:
            # Hard cap per update: the first refresh runs during HA startup and must not
            # stall bootstrap while connect retries + frame probing grind on.
            async with asyncio.timeout(POLL_TIMEOUT):
                state = await self.connection.async_poll()
            self._async_persist_protocol_mode()
            return state
        except (BleakError, asyncio.TimeoutError, EOFError) as err:
            # Drop the connection so the next cycle re-establishes cleanly.
            await self.connection.async_disconnect()
            detail = str(err) or type(err).__name__
            raise UpdateFailed(f"Error polling {self.entry.title}: {detail}") from err

    def _async_publish_new_events(self) -> None:
        """Placeholder: logbook events are held back until we know what a record represents.

        Field data shows consecutive 0x08 reads ~5 s apart with identical content, i.e. likely
        periodic snapshots — firing one logbook entry per record would be noise. Re-enable
        (see git history / logbook.py) once the record semantics and timestamp are verified.
        """
        return

    def _async_persist_protocol_mode(self) -> None:
        """Store the probed wire protocol on the entry so restarts skip the ladder."""
        mode = self.connection.protocol_mode
        if mode is None or self.entry.data.get(CONF_PROTOCOL_MODE) == mode:
            return
        _LOGGER.info("Persisting discovered protocol '%s' for %s", mode, self.entry.title)
        self.hass.config_entries.async_update_entry(
            self.entry, data={**self.entry.data, CONF_PROTOCOL_MODE: mode}
        )

    async def async_shutdown(self) -> None:
        await super().async_shutdown()
        await self.connection.async_disconnect()
