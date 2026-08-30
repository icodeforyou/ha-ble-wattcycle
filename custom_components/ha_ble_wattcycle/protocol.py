"""Pure WattCycle BLE protocol logic — no Home Assistant imports.

Reverse-engineered for interoperability under EU Directive 2009/24/EC Art. 6.
Not affiliated with or endorsed by WattCycle. No firmware or app code is redistributed.

See docs/PROTOCOL.md for the full derivation. This module is deliberately free of any
Home Assistant or bleak dependency so it can be unit-tested in isolation.
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
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
    current: float | None = None  # A (sign convention unverified — see docs)
    voltage: float | None = None  # V (pack)
    remaining_capacity: float | None = None  # Ah
    total_capacity: float | None = None  # Ah
    design_capacity: float | None = None  # Ah
    cycles: int | None = None
    soc: int | None = None  # %
    soh: int | None = None  # %
    balance_current: float | None = None  # A
    firmware_version: int | None = None

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
    state.voltage = round(struct.unpack(">H", payload[0:2])[0] / 100.0, 2)  # 10 mV units
    state.current = round(struct.unpack(">h", payload[2:4])[0] / 100.0, 2)  # signed, 10 mA
    state.remaining_capacity = round(struct.unpack(">H", payload[4:6])[0] / 100.0, 2)
    state.total_capacity = round(struct.unpack(">H", payload[6:8])[0] / 100.0, 2)
    state.cycles = struct.unpack(">H", payload[8:10])[0]
    ntc_count = payload[22] if len(payload) > 22 else 0
    state.soc = payload[19] if len(payload) > 19 else None
    temps = []
    for i in range(ntc_count):
        base = 23 + i * 2
        if len(payload) >= base + 2:
            raw = struct.unpack(">H", payload[base : base + 2])[0]
            temps.append(round((raw - 2731) / 10.0, 1))
    state.cell_temperatures = temps
    return state


def jbd_parse_cell_voltages(payload: bytes) -> list[float]:
    """Decode a JBD 0x04 cell-voltage payload: array of u16 millivolts."""
    count = len(payload) // 2
    return [round(struct.unpack(">H", payload[i * 2 : i * 2 + 2])[0] / 1000.0, 3) for i in range(count)]


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
