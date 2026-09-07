"""Pure WattCycle BLE protocol logic — no Home Assistant imports.

Reverse-engineered for interoperability under EU Directive 2009/24/EC Art. 6.
Not affiliated with or endorsed by WattCycle. No firmware or app code is redistributed.

See docs/PROTOCOL.md for the full derivation. This module is deliberately free of any
Home Assistant or bleak dependency so it can be unit-tested in isolation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum


# ---------------------------------------------------------------------------
# UUIDs per device type (§2 of PROTOCOL.md)
# ---------------------------------------------------------------------------
def uuid128(short: str) -> str:
    """Expand a 16-bit UUID to its full 128-bit base form (lowercase)."""
    return f"0000{short.lower()}-0000-1000-8000-00805f9b34fb"


class DeviceType(str, Enum):
    """Supported WattCycle BMS protocol families."""

    WATT = "watt"
    JBD = "jbd"


UUIDS: dict[DeviceType, dict[str, str | None]] = {
    DeviceType.WATT: {
        "service": uuid128("fff0"),
        "write": uuid128("fff2"),
        "notify": uuid128("fff1"),
        "auth": uuid128("fffa"),
    },
    DeviceType.JBD: {
        "service": uuid128("ff00"),
        "write": uuid128("ff02"),
        "notify": uuid128("ff01"),
        "auth": None,
    },
}

NAME_PREFIXES: dict[DeviceType, tuple[str, ...]] = {
    DeviceType.WATT: ("XDZN", "WT"),
    DeviceType.JBD: ("WT",),
}

# Manufacturer IDs seen in advertisements (§6). JBD = 0x2000, JK = 0x0B65.
MANUFACTURER_JBD = 0x2000
MANUFACTURER_JK = 0x0B65

# HiLink auth key written to the WATT auth characteristic (§5).
HILINK_AUTH_KEY = b"HiLink"

# WATT frame constants (§3).
WATT_HEAD = 0x7E
WATT_HEAD_ALT = 0x1E
WATT_TAIL = 0x0D
WATT_FUNC_READ = 0x03
WATT_FUNC_WRITE = 0x06
WATT_DEFAULT_ADDRESS = 0x01
WATT_MIN_FRAME_SIZE = 11
WATT_ERROR_FUNC = 0x86  # write error response (0x06 | 0x80)

# WATT data points (§4).
DP_ANALOG_QUANTITY = 140

# JBD frame constants (§7).
JBD_START = 0xDD
JBD_END = 0x77
JBD_READ = 0xA5
JBD_WRITE = 0x5A
JBD_CMD_BASIC_INFO = 0x03
JBD_CMD_CELL_VOLTAGES = 0x04
# WattCycle-specific JBD commands, from the app's JbdBleProtocolHandler. None of them need a
# password or factory mode. Only restart is exposed by this integration; the others are
# documented so nobody has to rediscover them, and deliberately NOT wired to entities.
JBD_CMD_SYSTEM_TIME = 0x06  # read: u32 BE timestamp
JBD_CMD_RECORD_TOTAL = 0x07  # read: u32 BE number of fault records
JBD_CMD_RECORD_CURRENT = 0x08  # read: next fault record (see docs/PROTOCOL.md)
JBD_CMD_RESTORE_DEFAULTS = 0x0A  # write [0x18, 0x81] — DESTRUCTIVE, never send
JBD_CMD_RESTART_SYSTEM = 0x0E  # write [0x81, 0x18] — soft-reboots the BMS
JBD_CMD_CONTROL_MOS = 0xFB  # write [target 1=charge/0=discharge, 1=off/0=on]
JBD_CMD_CONTROL_HEATING = 0xFD  # write [1=on/2=off, delay h, delay min, start °C, stop °C]

JBD_RESTART_PAYLOAD = bytes([0x81, 0x18])

# JBD response status byte (frame[2]) for write commands.
JBD_STATUS_OK = 0x00
JBD_ERRORS: dict[int, str] = {
    0x80: "command not supported",
    0x81: "invalid operation",
    0x82: "checksum error",
    0x83: "password mismatch",
}

# JBD basic-info protection bitfield (payload bytes 16-17, big-endian). Bit order taken
# from the app's JbdProtectionStatus constructor; bits 0-12 match the public Xiaoxiang
# layout, 13-15 are WattCycle additions. Key -> bit index.
JBD_PROTECTION_BITS: dict[str, int] = {
    "cell_overvoltage": 0,
    "cell_undervoltage": 1,
    "pack_overvoltage": 2,
    "pack_undervoltage": 3,
    "charge_overtemperature": 4,
    "charge_undertemperature": 5,
    "discharge_overtemperature": 6,
    "discharge_undertemperature": 7,
    "charge_overcurrent": 8,
    "discharge_overcurrent": 9,
    "short_circuit": 10,
    "ic_error": 11,
    "mos_software_lock": 12,
    "charge_mos_broken": 13,
    "discharge_mos_broken": 14,
    "mos_overtemperature": 15,
}

# JBD basic-info FET/status byte (payload byte 20), bit order from the app's JbdFetStatus.
JBD_FET_CHARGE = 0x01
JBD_FET_DISCHARGE = 0x02
JBD_FET_PREDISCHARGE = 0x04
JBD_FET_HEATING_INDICATOR = 0x08
JBD_FET_HEATING_ON = 0x10
JBD_FET_FORCED_DISCHARGE = 0x20
JBD_FET_FACTORY_MODE = 0x40
JBD_FET_CURRENT_UNIT_100MA = 0x80  # when set, current/capacity are in 100 mA / 100 mAh units

# Warning bitfield (payload byte after NTCs + 1, u16 BE) — advisory, does not open FETs.
# Bit order from the app's JbdWarningStatus constructor.
JBD_WARNING_BITS: dict[str, int] = {
    "cell_high_voltage": 0,
    "cell_low_voltage": 1,
    "pack_high_voltage": 2,
    "pack_low_voltage": 3,
    "charge_high_temperature": 4,
    "charge_low_temperature": 5,
    "discharge_high_temperature": 6,
    "discharge_low_temperature": 7,
    "charge_overcurrent": 8,
    "discharge_overcurrent": 9,
    "cell_voltage_difference": 10,
    "low_capacity": 11,
    "disconnection": 12,
    "heating_mos_broken": 13,
}


# ---------------------------------------------------------------------------
# Modbus CRC-16 (poly 0xA001, init 0xFFFF). WATT transmits it little-endian (§3.1).
# ---------------------------------------------------------------------------
def modbus_crc16(data: bytes) -> int:
    """Standard Modbus CRC-16. Returns the value transmitted low-byte-first."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


# ---------------------------------------------------------------------------
# WATT frame construction
# ---------------------------------------------------------------------------
def watt_build_read_frame(
    register: int,
    read_count: int = 0,
    info_data: bytes | None = None,
    head: int = WATT_HEAD,
) -> bytes:
    """Build a WATT read frame (function 0x03). See §3."""
    body = bytearray()
    body.append(head)
    body.append(0x01 if info_data else 0x00)
    body.append(WATT_DEFAULT_ADDRESS)
    body.append(WATT_FUNC_READ)
    body += struct.pack(">H", register)
    body += struct.pack(">H", read_count)
    if info_data:
        body += info_data
    body += struct.pack("<H", modbus_crc16(bytes(body)))
    body.append(WATT_TAIL)
    return bytes(body)


def watt_build_write_frame(register: int, data: bytes, head: int = WATT_HEAD) -> bytes:
    """Build a WATT write frame (function 0x06). DANGEROUS — see docs/TESTPLAN.md."""
    body = bytearray()
    body.append(head)
    body.append(0x00)
    body.append(WATT_DEFAULT_ADDRESS)
    body.append(WATT_FUNC_WRITE)
    body += struct.pack(">H", register)
    body += struct.pack(">H", len(data))
    body += data
    body += struct.pack("<H", modbus_crc16(bytes(body)))
    body.append(WATT_TAIL)
    return bytes(body)


def watt_build_info_data(
    address: int = 1, voltage_count: int = 32, temperature_count: int = 32
) -> bytes:
    """infoData block appended to the analog read frame on firmware version >= 4 (§4)."""
    return struct.pack(">HBHH", 5, address, voltage_count, temperature_count)


def watt_analog_read_frame(firmware_version: int | None = None) -> bytes:
    """Analog-quantity (DP 140) read command. version>=4 needs the infoData block."""
    if firmware_version is not None and firmware_version >= 4:
        return watt_build_read_frame(DP_ANALOG_QUANTITY, info_data=watt_build_info_data())
    return watt_build_read_frame(DP_ANALOG_QUANTITY)


def watt_analog_probe_frames() -> list[tuple[str, bytes]]:
    """All analog-read request variants, in probe order.

    The app's detectProductHeader tries frame head 0x7E first and falls back to 0x1E
    (responses always start with 0x7E either way). Newer firmware additionally wants
    the infoData block appended to the read.
    """
    info = watt_build_info_data()
    return [
        ("head 0x7E", watt_build_read_frame(DP_ANALOG_QUANTITY)),
        ("head 0x7E + infoData", watt_build_read_frame(DP_ANALOG_QUANTITY, info_data=info)),
        ("head 0x1E", watt_build_read_frame(DP_ANALOG_QUANTITY, head=WATT_HEAD_ALT)),
        (
            "head 0x1E + infoData",
            watt_build_read_frame(DP_ANALOG_QUANTITY, info_data=info, head=WATT_HEAD_ALT),
        ),
    ]


# ---------------------------------------------------------------------------
# WATT frame parsing
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class WattFrame:
    """A parsed WATT response frame."""

    version: int
    address: int
    func: int
    register: int
    length: int
    payload: bytes
    crc_ok: bool | None


def watt_parse_frame(data: bytes) -> WattFrame | None:
    """Parse a complete WATT response frame (0x7E ... 0x0D). Returns None if malformed."""
    if len(data) < WATT_MIN_FRAME_SIZE:
        return None
    if data[0] != WATT_HEAD or data[-1] != WATT_TAIL:
        return None
    version = data[1]
    address = data[2]
    func = data[3]
    register = struct.unpack(">H", data[4:6])[0]
    length = struct.unpack(">H", data[6:8])[0]
    if len(data) < length + WATT_MIN_FRAME_SIZE:
        return None
    payload = data[8 : 8 + length]
    crc_wire = struct.unpack("<H", data[8 + length : 8 + length + 2])[0]
    crc_calc = modbus_crc16(data[0 : 8 + length])
    return WattFrame(
        version=version,
        address=address,
        func=func,
        register=register,
        length=length,
        payload=payload,
        crc_ok=(crc_wire == crc_calc),
    )


def watt_expected_length(first_packet: bytes) -> int | None:
    """Total expected frame length from the first notify packet (§ calculateExpectedLength)."""
    if len(first_packet) >= 8 and first_packet[0] == WATT_HEAD:
        return struct.unpack(">H", first_packet[6:8])[0] + WATT_MIN_FRAME_SIZE
    return None


def _parse_watt_current(b0: int, b1: int) -> float:
    """Signed WATT current encoding (§4.1). sign=0x80, /10 scale=0x40, 14-bit magnitude."""
    negative = b0 & 0x80
    scaled = b0 & 0x40
    magnitude = b1 | ((b0 & 0x3F) << 8)
    value = magnitude / 10.0 if scaled else float(magnitude)
    return -value if negative else value


# ---------------------------------------------------------------------------
# Telemetry model
# ---------------------------------------------------------------------------
@dataclass(slots=True)
class BatteryState:
    """Decoded battery telemetry (union of WATT and JBD fields; None when unavailable)."""

    cell_count: int | None = None
    cell_voltages: list[float] = field(default_factory=list)  # V
    temperature_count: int | None = None
    mos_temperature: float | None = None  # C
    pcb_temperature: float | None = None  # C
    cell_temperatures: list[float] = field(default_factory=list)  # C
    current: float | None = None  # A, positive = charging (verified 2026-09-07 on DISCOVER 314Ah)
    voltage: float | None = None  # V (pack)
    remaining_capacity: float | None = None  # Ah
    total_capacity: float | None = None  # Ah
    design_capacity: float | None = None  # Ah
    cycles: int | None = None
    soc: int | None = None  # %
    soh: int | None = None  # %
    balance_current: float | None = None  # A
    firmware_version: int | None = None
    # JBD-only status fields (None when the protocol does not provide them).
    protection_status: int | None = None  # raw 16-bit bitfield, see JBD_PROTECTION_BITS
    charge_fet_on: bool | None = None
    discharge_fet_on: bool | None = None
    balance_status: int | None = None  # raw 32-bit bitfield, bit n = cell n+1 balancing
    fet_status: int | None = None  # raw byte 20, see JBD_FET_* bits
    heating_on: bool | None = None
    warning_status: int | None = None  # raw 16-bit bitfield, see JBD_WARNING_BITS
    protocol_version: str | None = None  # e.g. "2.5"

    def protection_active(self, key: str) -> bool | None:
        """True if the named JBD protection is currently tripped."""
        if self.protection_status is None:
            return None
        return bool(self.protection_status >> JBD_PROTECTION_BITS[key] & 1)

    @property
    def active_protections(self) -> list[str]:
        """Names of all currently tripped protections (empty when none or unknown)."""
        if not self.protection_status:
            return []
        return [k for k, bit in JBD_PROTECTION_BITS.items() if self.protection_status >> bit & 1]

    @property
    def active_warnings(self) -> list[str]:
        """Names of all currently raised warnings (empty when none or unknown)."""
        if not self.warning_status:
            return []
        return [k for k, bit in JBD_WARNING_BITS.items() if self.warning_status >> bit & 1]

    @property
    def balancing_cells(self) -> list[int]:
        """1-based indexes of cells the BMS is currently balancing."""
        if not self.balance_status:
            return []
        return [i + 1 for i in range(32) if self.balance_status >> i & 1]

    @property
    def min_cell_voltage(self) -> float | None:
        return min(self.cell_voltages) if self.cell_voltages else None

    @property
    def max_cell_voltage(self) -> float | None:
        return max(self.cell_voltages) if self.cell_voltages else None

    @property
    def cell_voltage_delta(self) -> float | None:
        if not self.cell_voltages:
            return None
        return round(max(self.cell_voltages) - min(self.cell_voltages), 3)

    @property
    def power(self) -> float | None:
        if self.voltage is None or self.current is None:
            return None
        return round(self.voltage * self.current, 1)


def watt_decode_analog_quantity(payload: bytes) -> BatteryState:
    """Decode the DP 140 payload into a BatteryState (§4)."""
    offset = 0

    def u8() -> int:
        nonlocal offset
        value = payload[offset]
        offset += 1
        return value

    def u16() -> int:
        nonlocal offset
        value = struct.unpack(">H", payload[offset : offset + 2])[0]
        offset += 2
        return value

    def u32() -> int:
        nonlocal offset
        value = struct.unpack(">I", payload[offset : offset + 4])[0]
        offset += 4
        return value

    def i32() -> int:
        nonlocal offset
        value = struct.unpack(">i", payload[offset : offset + 4])[0]
        offset += 4
        return value

    def current2() -> float:
        nonlocal offset
        value = _parse_watt_current(payload[offset], payload[offset + 1])
        offset += 2
        return value

    state = BatteryState()
    state.cell_count = u8()
    state.cell_voltages = [round(u16() / 1000.0, 3) for _ in range(state.cell_count)]
    state.temperature_count = u8()
    state.mos_temperature = round((u16() - 2730) / 10.0, 1)
    state.pcb_temperature = round((u16() - 2730) / 10.0, 1)
    state.cell_temperatures = [
        round((u16() - 2730) / 10.0, 1) for _ in range(max(state.temperature_count - 2, 0))
    ]
    state.current = round(current2(), 2)
    state.voltage = round(u16() / 100.0, 2)
    state.remaining_capacity = round(u16() / 10.0, 1)
    state.total_capacity = round(u16() / 10.0, 1)
    state.cycles = u16()
    state.design_capacity = round(u16() / 10.0, 1)
    state.soc = u16()
    if len(payload) - offset >= 18:
        state.soh = u16()
        u32()  # cumulative capacity (Ah/10) — logged by app, not surfaced
        i32()  # remaining time (min) — logged by app, not surfaced
        u16()
        u16()
        u16()
        state.balance_current = round(current2(), 2)
    return state


# ---------------------------------------------------------------------------
# JBD frame construction / parsing (§7) — standard Xiaoxiang protocol
# ---------------------------------------------------------------------------
def jbd_build_read_frame(command: int) -> bytes:
    """Build a JBD read frame: DD A5 <cmd> 00 <chk_hi> <chk_lo> 77."""
    checksum = (0x10000 - (command & 0xFFFF)) & 0xFFFF
    return bytes([JBD_START, JBD_READ, command, 0x00]) + struct.pack(">H", checksum) + bytes(
        [JBD_END]
    )


def jbd_parse_basic_info(payload: bytes) -> BatteryState:
    """Decode a JBD 0x03 basic-info payload (standard layout)."""
    state = BatteryState()
    # Byte 20 bit 7 switches current/capacity units from 10 mA(h) to 100 mA(h); the app
    # applies the same scale to remaining and nominal capacity.
    fet = payload[20] if len(payload) > 20 else 0
    scale = 10.0 if fet & JBD_FET_CURRENT_UNIT_100MA else 100.0
    state.voltage = round(struct.unpack(">H", payload[0:2])[0] / 100.0, 2)  # 10 mV units
    state.current = round(struct.unpack(">h", payload[2:4])[0] / scale, 2)  # signed
    state.remaining_capacity = round(struct.unpack(">H", payload[4:6])[0] / scale, 2)
    state.total_capacity = round(struct.unpack(">H", payload[6:8])[0] / scale, 2)
    state.cycles = struct.unpack(">H", payload[8:10])[0]
    # [10:12] production date, [12:14] balance status cells 1-16, [14:16] cells 17-32,
    # [16:18] protection bitfield, [18] software version, [19] RSOC, [20] FET control,
    # [21] cell count, [22] NTC count, [23..] NTC values.
    if len(payload) >= 16:
        low, high = struct.unpack(">HH", payload[12:16])
        state.balance_status = low | (high << 16)
    if len(payload) >= 18:
        state.protection_status = struct.unpack(">H", payload[16:18])[0]
    if len(payload) > 18:
        state.firmware_version = payload[18]
    ntc_count = payload[22] if len(payload) > 22 else 0
    state.soc = payload[19] if len(payload) > 19 else None
    if len(payload) > 20:
        state.fet_status = fet
        state.charge_fet_on = bool(fet & JBD_FET_CHARGE)
        state.discharge_fet_on = bool(fet & JBD_FET_DISCHARGE)
        state.heating_on = bool(fet & JBD_FET_HEATING_ON)
    if len(payload) > 21:
        state.cell_count = payload[21]
    # WattCycle extension after the NTC block (layout from the app's handleBasicInfoResponse):
    # u8 humidity, u16 warning bits, u16 full capacity, u16 actual remaining, i16 balance
    # current (mA), u16 active-balance status, u8 protocol version, u16 reserved, u16 BMS status.
    extra = 23 + ntc_count * 2
    if len(payload) >= extra + 3:
        state.warning_status = struct.unpack(">H", payload[extra + 1 : extra + 3])[0]
    if len(payload) >= extra + 9:
        state.balance_current = round(
            struct.unpack(">h", payload[extra + 7 : extra + 9])[0] / 1000.0, 3
        )
    if len(payload) >= extra + 12:
        raw = payload[extra + 11]
        state.protocol_version = f"{raw // 10}.{raw % 10}"
    temps = []
    for i in range(ntc_count):
        base = 23 + i * 2
        if len(payload) >= base + 2:
            raw = struct.unpack(">H", payload[base : base + 2])[0]
            temps.append(round((raw - 2731) / 10.0, 1))
    state.cell_temperatures = temps
    return state


def jbd_build_write_frame(command: int, data: bytes) -> bytes:
    """Build a JBD write frame: DD 5A <cmd> <len> <data> <chk_hi> <chk_lo> 77.

    Checksum is the app's calculateRequestChecksum: -(cmd + len + sum(data)) & 0xFFFF.
    """
    body = bytes([command & 0xFF, len(data)]) + data
    checksum = (-sum(body)) & 0xFFFF
    return bytes([JBD_START, JBD_WRITE]) + body + struct.pack(">H", checksum) + bytes([JBD_END])


def jbd_build_restart_frame() -> bytes:
    """The frame behind the WattCycle app's 'Reboot system' button."""
    return jbd_build_write_frame(JBD_CMD_RESTART_SYSTEM, JBD_RESTART_PAYLOAD)


@dataclass(frozen=True)
class JbdAck:
    """Response to a JBD write (or a read the BMS rejected)."""

    command: int
    status: int

    @property
    def ok(self) -> bool:
        return self.status == JBD_STATUS_OK

    @property
    def error(self) -> str:
        if self.ok:
            return "ok"
        return JBD_ERRORS.get(self.status, f"status 0x{self.status:02x}")


@dataclass(frozen=True)
class JbdRecordInfo:
    """Reply to 0x07 as observed on the DISCOVER 314Ah: `00 e2 01 2c` = two u16 BE.

    Read as (index=226, capacity=300); 300 also leads every 0x08 record header, so this looks
    like a 300-slot ring buffer with 226 slots written. Interpretation unverified.
    """

    index: int
    capacity: int


def jbd_parse_record_info(payload: bytes) -> JbdRecordInfo | None:
    if len(payload) < 4:
        return None
    index, capacity = struct.unpack(">HH", payload[0:4])
    return JbdRecordInfo(index, capacity)


def _bcd(byte: int) -> int:
    return (byte >> 4) * 10 + (byte & 0x0F)


@dataclass(frozen=True)
class JbdClock:
    """Reply to 0x06: six BCD bytes `ss mm hh dd MM yy` — but not a calendar clock.

    Verified 2026-09-07 on a DISCOVER 314Ah: the time-of-day ticks in real time and the day
    rolls over at 23:59→00:00, yet a BMS restart reset hh:mm:ss to 00:00:00 while keeping
    dd/MM (04/02 before and after) and flipped yy from 01 to 00. So dd/MM is a persistent
    day counter since first power-on (day 1 of month 1 = day 0), hh:mm:ss counts from the
    last restart (and keeps running across the day rollover), and yy is not a year — its
    meaning is unknown and it is ignored.
    """

    raw: bytes

    @property
    def hex(self) -> str:
        return self.raw.hex(" ")

    @property
    def fields(self) -> tuple[int, int, int, int, int, int] | None:
        """(ss, mm, hh, dd, MM, yy) decoded from BCD, or None if malformed."""
        if len(self.raw) < 6:
            return None
        vals = tuple(_bcd(b) for b in self.raw[:6])
        ss, mm, hh, dd, mo, _yy = vals
        if ss > 59 or mm > 59 or hh > 23 or not 1 <= dd <= 31 or not 1 <= mo <= 12:
            return None
        return vals  # type: ignore[return-value]

    @property
    def bms_datetime(self) -> datetime | None:
        """The raw fields laid out as a datetime in year 2000, for display only."""
        f = self.fields
        if f is None:
            return None
        ss, mm, hh, dd, mo, _ = f
        try:
            return datetime(2000, mo, dd, hh, mm, ss)
        except ValueError:
            return None

    @property
    def days_running(self) -> int | None:
        """Days since first power-on, from the persistent dd/MM counter (2000 calendar)."""
        dt = self.bms_datetime
        return None if dt is None else (dt - datetime(2000, 1, 1)).days

    @property
    def time_since_restart(self) -> timedelta | None:
        """hh:mm:ss — time since the last BMS restart, valid until the first day rollover."""
        f = self.fields
        if f is None:
            return None
        ss, mm, hh, *_ = f
        return timedelta(hours=hh, minutes=mm, seconds=ss)

    @property
    def elapsed(self) -> timedelta | None:
        """Day counter plus time of day; decreases only when the BMS restarts."""
        dt = self.bms_datetime
        return None if dt is None else dt - datetime(2000, 1, 1)


def jbd_parse_clock(payload: bytes) -> JbdClock | None:
    if not payload:
        return None
    return JbdClock(bytes(payload))


@dataclass
class FaultRecord:
    """One entry read with JBD 0x08 ("current record"), as the DISCOVER 314Ah actually sends it.

    Observed 2026-09-07 (68-byte payload): LITTLE-endian, unlike the app's big-endian
    parseFaultRecord, but with the app's field order from the voltage onwards. Header:
    `01 2c 00 SS 06 XX DD hh mm ss` — 0x012c = 300 (ring size), SS = records left in the
    batch, XX a restart-reset counter (was 02, then 00), day (bit 6 set), hour, minute,
    second in binary. Records are written
    every 5 minutes (index rose 226→229 in 15 min); 300 slots = ~25 h of history. So this is a
    periodic snapshot ring, not a fault log. Byte 4 (0x06) is not understood.
    """

    header: bytes  # payload[0:10], raw
    voltage: float
    current: float
    remaining_capacity: float
    nominal_capacity: float
    protection_status: int
    warning_status: int
    temperatures: list[float | None]  # 4 slots, None where the BMS sends 0
    max_cell_voltage: float
    min_cell_voltage: float
    max_cell_index: int
    min_cell_index: int
    fet_status: int
    cell_voltages: list[float] = field(default_factory=list)

    @property
    def sequence(self) -> int:
        """Byte 3 of the header: records remaining in this read batch (counts down to 0)."""
        return self.header[3]

    def bms_datetime(self, month: int) -> datetime | None:
        """Record time on the BMS counter: header bytes 6-9 = day|0x40, hh, mm, ss (binary).

        Byte 5 looked like the month (02) until a BMS restart reset it to 00 while the clock's
        month stayed 02, so it is another restart-reset counter of unknown meaning; the month
        must come from the BMS clock (JbdClock.fields). Laid out in year 2000 like the clock.
        """
        try:
            return datetime(
                2000, month, self.header[6] & 0x1F,
                self.header[7], self.header[8], self.header[9],
            )
        except (ValueError, IndexError):
            return None

    @property
    def unknown_byte5(self) -> int:
        """Header byte 5: 02 before, 00 after a BMS restart. Meaning unknown."""
        return self.header[5]

    @property
    def active_protections(self) -> list[str]:
        return [k for k, bit in JBD_PROTECTION_BITS.items() if self.protection_status >> bit & 1]

    @property
    def active_warnings(self) -> list[str]:
        return [k for k, bit in JBD_WARNING_BITS.items() if self.warning_status >> bit & 1]

    @property
    def charge_fet_on(self) -> bool:
        return bool(self.fet_status & JBD_FET_CHARGE)

    @property
    def discharge_fet_on(self) -> bool:
        return bool(self.fet_status & JBD_FET_DISCHARGE)

    @property
    def key(self) -> bytes:
        """Identity used to spot records already seen."""
        return self.header

    def summary(self) -> str:
        """Short human-readable description."""
        parts = [k.replace("_", " ") for k in self.active_protections]
        if not parts:
            parts = [f"warning: {k.replace('_', ' ')}" for k in self.active_warnings]
        if not parts:
            parts = ["no flags"]
        return (
            f"{', '.join(parts)} — {self.voltage:.2f} V, {self.current:+.2f} A, "
            f"cell max {self.max_cell_voltage:.3f} V (#{self.max_cell_index}), "
            f"min {self.min_cell_voltage:.3f} V (#{self.min_cell_index})"
        )


def _temp_or_none(raw: int) -> float | None:
    return None if raw == 0 else round((raw - 2731) / 10.0, 1)


# Unused cell slots in a record are padded with this value (3.600 V).
_RECORD_CELL_PAD = 3600


def jbd_parse_fault_record(payload: bytes) -> FaultRecord | None:
    """Decode one 0x08 record (little-endian, observed layout). None if too short."""
    if len(payload) < 38:
        return None
    (v, i, rem, nom, prot, warn, t1, t2, t3, t4, vmax, vmin) = struct.unpack(
        "<HhHHHHHHHHHH", payload[10:34]
    )
    max_idx, min_idx, fet = payload[34], payload[35], payload[36]
    cells: list[float] = []
    for pos in range(38, len(payload) - 1, 2):
        raw = struct.unpack("<H", payload[pos : pos + 2])[0]
        if raw == _RECORD_CELL_PAD and len(cells) >= 1:
            break
        cells.append(round(raw / 1000.0, 3))
    return FaultRecord(
        header=bytes(payload[0:10]),
        voltage=round(v / 100.0, 2),
        current=round(i / 100.0, 2),
        remaining_capacity=round(rem / 100.0, 2),
        nominal_capacity=round(nom / 100.0, 2),
        protection_status=prot,
        warning_status=warn,
        temperatures=[_temp_or_none(t) for t in (t1, t2, t3, t4)],
        max_cell_voltage=round(vmax / 1000.0, 3),
        min_cell_voltage=round(vmin / 1000.0, 3),
        max_cell_index=max_idx,
        min_cell_index=min_idx,
        fet_status=fet,
        cell_voltages=cells,
    )


def jbd_parse_cell_voltages(payload: bytes) -> list[float]:
    """Decode a JBD 0x04 cell-voltage payload: array of u16 millivolts."""
    count = len(payload) // 2
    return [round(struct.unpack(">H", payload[i * 2 : i * 2 + 2])[0] / 1000.0, 3) for i in range(count)]


# ---------------------------------------------------------------------------
# BMC protocol (§ PROTOCOL.md) — present in the app but not yet wired up there;
# likely used by the newest packs (e.g. DISCOVER self-heating: it is the only
# protocol with a heating-control command). Little-endian throughout.
# Frame: AA <cmd> <len> <data...> <chk u16 LE>, chk = sum(cmd + len + data).
# ---------------------------------------------------------------------------
BMC_HEADER = 0xAA
BMC_MIN_FRAME_SIZE = 5
BMC_CMD_HANDSHAKE = 0x00
BMC_CMD_MANUFACTURER_NAME = 0x10
BMC_CMD_PACK_NAME = 0x11
BMC_CMD_RUNNING_STATUS = 0x20
BMC_CMD_BATTERY_INFO = 0x21
BMC_CMD_CELL_VOLTAGE = 0x22
BMC_CMD_CURRENT = 0x23


def bmc_build_frame(command: int, data: bytes = b"") -> bytes:
    """Build a BMC frame: AA cmd len data chk(u16 LE, plain byte sum)."""
    body = bytes([command & 0xFF, len(data)]) + data
    checksum = sum(body) & 0xFFFF
    return bytes([BMC_HEADER]) + body + struct.pack("<H", checksum)


def bmc_expected_length(first_packet: bytes) -> int | None:
    """Total BMC frame length from the first notify packet."""
    if len(first_packet) >= 3 and first_packet[0] == BMC_HEADER:
        return first_packet[2] + BMC_MIN_FRAME_SIZE
    return None


def bmc_parse_frame(data: bytes) -> tuple[int, bytes] | None:
    """Parse a BMC frame; returns (command, payload) or None if malformed."""
    if len(data) < BMC_MIN_FRAME_SIZE or data[0] != BMC_HEADER:
        return None
    length = data[2]
    if len(data) < length + BMC_MIN_FRAME_SIZE:
        return None
    payload = data[3 : 3 + length]
    chk_wire = struct.unpack("<H", data[3 + length : 5 + length])[0]
    chk_calc = sum(data[1 : 3 + length]) & 0xFFFF
    if chk_wire != chk_calc:
        return None
    return data[1], payload


def bmc_decode_battery_info(payload: bytes) -> BatteryState:
    """Decode a BMC battery-info (0x21) payload. Little-endian.

    Capacity units and single-byte temperature encoding are (unverified).
    """
    state = BatteryState()
    (voltage_raw, current_raw) = struct.unpack_from("<ii", payload, 0)
    state.voltage = round(voltage_raw / 1000.0, 2)
    state.current = round(current_raw / 1000.0, 2)
    state.soc = payload[8]
    state.soh = payload[9]
    remaining, full = struct.unpack_from("<ii", payload, 10)
    state.remaining_capacity = round(remaining / 1000.0, 1)  # assume mAh (unverified)
    state.total_capacity = round(full / 1000.0, 1)
    state.cycles = struct.unpack_from("<H", payload, 18)[0]
    if len(payload) >= 26:
        t1, t2, t3, t4, mos, ambient = payload[20:26]
        state.cell_temperatures = [float(t1), float(t2), float(t3), float(t4)]
        state.mos_temperature = float(mos)
        state.pcb_temperature = float(ambient)  # ambient, reused slot
    return state


def bmc_decode_cell_voltages(payload: bytes) -> list[float]:
    """Decode a BMC cell-voltage (0x22) payload: 24 u16 LE slots in mV; zeros unused."""
    count = min(len(payload) // 2, 24)
    cells = [
        struct.unpack_from("<H", payload, i * 2)[0] / 1000.0 for i in range(count)
    ]
    return [round(v, 3) for v in cells if v > 0.0]


# ---------------------------------------------------------------------------
# Device-type detection from advertisement data
# ---------------------------------------------------------------------------
def detect_device_type(
    service_uuids: list[str] | None,
    manufacturer_ids: list[int] | None,
    name: str | None,
) -> DeviceType | None:
    """Best-effort device-type detection from a scan result (§6)."""
    mids = manufacturer_ids or []
    if MANUFACTURER_JBD in mids:
        return DeviceType.JBD
    svcs = {s.lower() for s in (service_uuids or [])}
    if UUIDS[DeviceType.WATT]["service"] in svcs:
        return DeviceType.WATT
    if UUIDS[DeviceType.JBD]["service"] in svcs:
        return DeviceType.JBD
    if name:
        for device_type, prefixes in NAME_PREFIXES.items():
            if any(name.startswith(prefix) for prefix in prefixes):
                return device_type
    return None
