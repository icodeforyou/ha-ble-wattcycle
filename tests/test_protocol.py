"""Unit tests for the pure WattCycle protocol logic (no Home Assistant/bleak needed)."""

import struct
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "custom_components" / "ha_ble_wattcycle"))

import protocol as p  # noqa: E402


def test_modbus_crc_known_vector():
    # Modbus test vector: 01 04 02 FF FF -> wire bytes B8 80 (value 0x80B8).
    crc = p.modbus_crc16(bytes([0x01, 0x04, 0x02, 0xFF, 0xFF]))
    assert struct.pack("<H", crc) == bytes([0xB8, 0x80])


def test_watt_read_frame_structure_and_crc():
    frame = p.watt_build_read_frame(p.DP_ANALOG_QUANTITY)
    assert frame[0] == p.WATT_HEAD
    assert frame[-1] == p.WATT_TAIL
    assert frame[3] == p.WATT_FUNC_READ
    assert frame[4:6] == struct.pack(">H", p.DP_ANALOG_QUANTITY)
    # CRC (2 bytes before tail) must validate over everything before it.
    body = frame[:-3]
    assert struct.pack("<H", p.modbus_crc16(body)) == frame[-3:-1]


def test_watt_read_frame_v4_has_info_data():
    frame = p.watt_analog_read_frame(firmware_version=4)
    assert frame[1] == 0x01  # info flag set
    assert p.watt_build_info_data() in frame


def test_watt_analog_probe_frames_cover_both_heads():
    frames = p.watt_analog_probe_frames()
    heads = [frame[0] for _label, frame in frames]
    assert heads == [0x7E, 0x7E, 0x1E, 0x1E]
    # every variant is a valid frame: CRC over everything before crc+tail
    for _label, frame in frames:
        assert frame[-1] == p.WATT_TAIL
        assert struct.pack("<H", p.modbus_crc16(frame[:-3])) == frame[-3:-1]


def _build_watt_analog_frame(new_protocol: bool) -> bytes:
    def u16(v: int) -> bytes:
        return struct.pack(">H", v)

    payload = bytearray()
    payload += bytes([4])  # cell count
    for mv in (3300, 3310, 3295, 3305):
        payload += u16(mv)
    payload += bytes([4])  # temp count (2 special + 2 cell)
    payload += u16(int(25.0 * 10 + 2730))
    payload += u16(int(24.0 * 10 + 2730))
    payload += u16(int(23.5 * 10 + 2730))
    payload += u16(int(23.7 * 10 + 2730))
    # current -12.3 A: neg + /10 scale, magnitude 123
    payload += bytes([0x80 | 0x40 | ((123 >> 8) & 0x3F), 123 & 0xFF])
    payload += u16(1327)  # module voltage /100 = 13.27 V
    payload += u16(500)   # remaining /10 = 50.0 Ah
    payload += u16(1000)  # total /10 = 100.0 Ah
    payload += u16(42)    # cycles
    payload += u16(1000)  # design /10 = 100.0 Ah
    payload += u16(87)    # soc %
    if new_protocol:
        payload += u16(95)          # soh
        payload += struct.pack(">I", 12345)  # cumulative cap
        payload += struct.pack(">i", 600)    # remaining time
        payload += u16(0) + u16(0) + u16(0)  # reserved
        payload += bytes([0x40, 5])          # balance current 0.5 A

    header = bytes([p.WATT_HEAD, 3, 1, p.WATT_FUNC_READ]) + u16(p.DP_ANALOG_QUANTITY) + u16(len(payload))
    frame = bytearray(header + payload)
    frame += struct.pack("<H", p.modbus_crc16(bytes(frame)))
    frame.append(p.WATT_TAIL)
    return bytes(frame)


def test_watt_parse_and_decode_roundtrip():
    frame = _build_watt_analog_frame(new_protocol=False)
    parsed = p.watt_parse_frame(frame)
    assert parsed is not None
    assert parsed.crc_ok is True
    assert parsed.register == p.DP_ANALOG_QUANTITY
    state = p.watt_decode_analog_quantity(parsed.payload)
    assert state.cell_count == 4
    assert state.cell_voltages == [3.3, 3.31, 3.295, 3.305]
    assert state.current == -12.3
    assert state.voltage == 13.27
    assert state.soc == 87
    assert state.cycles == 42
    assert state.mos_temperature == 25.0
    assert state.cell_temperatures == [23.5, 23.7]
    assert state.cell_voltage_delta == 0.015
    assert state.power == round(13.27 * -12.3, 1)


def test_watt_decode_new_protocol_extra_fields():
    frame = _build_watt_analog_frame(new_protocol=True)
    state = p.watt_decode_analog_quantity(p.watt_parse_frame(frame).payload)
    assert state.soh == 95
    assert state.balance_current == 0.5


def test_watt_parse_rejects_bad_frame():
    assert p.watt_parse_frame(b"\x00\x01\x02") is None
    assert p.watt_parse_frame(bytes([p.WATT_HEAD]) + b"\x00" * 20) is None  # no tail


def test_jbd_read_frame():
    frame = p.jbd_build_read_frame(p.JBD_CMD_BASIC_INFO)
    assert frame == bytes([0xDD, 0xA5, 0x03, 0x00, 0xFF, 0xFD, 0x77])


def test_jbd_cell_voltages():
    payload = struct.pack(">HHHH", 3300, 3310, 3295, 3305)
    assert p.jbd_parse_cell_voltages(payload) == [3.3, 3.31, 3.295, 3.305]


def test_bmc_frame_roundtrip():
    handshake = p.bmc_build_frame(p.BMC_CMD_HANDSHAKE)
    assert handshake == bytes([0xAA, 0x00, 0x00, 0x00, 0x00])

    payload = (
        struct.pack("<ii", 13234, -5200)          # 13.23 V, -5.2 A
        + bytes([87, 99])                          # soc, soh
        + struct.pack("<ii", 150000, 314000)       # remaining, full (mAh)
        + struct.pack("<H", 42)                    # cycles
        + bytes([25, 26, 24, 23, 30, 22])          # t1-t4, mos, ambient
    )
    frame = p.bmc_build_frame(p.BMC_CMD_BATTERY_INFO, payload)
    cmd, parsed_payload = p.bmc_parse_frame(frame)
    assert cmd == p.BMC_CMD_BATTERY_INFO
    state = p.bmc_decode_battery_info(parsed_payload)
    assert state.voltage == 13.23
    assert state.current == -5.2
    assert state.soc == 87 and state.soh == 99
    assert state.cycles == 42
    assert state.mos_temperature == 30.0

    # corrupted checksum must be rejected
    bad = bytearray(frame)
    bad[-1] ^= 0xFF
    assert p.bmc_parse_frame(bytes(bad)) is None


def test_bmc_cell_voltages_strip_zero_slots():
    payload = struct.pack("<24H", 3300, 3310, 3295, 3305, *([0] * 20))
    assert p.bmc_decode_cell_voltages(payload) == [3.3, 3.31, 3.295, 3.305]


def test_detect_device_type():
    assert p.detect_device_type([], [p.MANUFACTURER_JBD], None) is p.DeviceType.JBD
    assert p.detect_device_type([p.uuid128("fff0")], [], None) is p.DeviceType.WATT
    assert p.detect_device_type([], [], "WT06-1234") is p.DeviceType.WATT
    assert p.detect_device_type([], [], "Random") is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
