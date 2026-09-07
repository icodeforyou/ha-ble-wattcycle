"""Unit tests for the pure WattCycle protocol logic (no Home Assistant/bleak needed)."""

import struct
from datetime import datetime, timedelta
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


def _jbd_basic_info(protection=0, fet=0x03, balance=0, ntc=(2731 + 200,)):
    """Build a standard JBD 0x03 payload (bytes 0..22 + NTCs)."""
    body = struct.pack(">HhHHH", 1318, 0, 16115, 31400, 3)  # V, A, remaining, total, cycles
    body += struct.pack(">H", 0)  # production date
    body += struct.pack(">HH", balance & 0xFFFF, balance >> 16)  # balance status lo/hi
    body += struct.pack(">H", protection)
    body += bytes([0x21, 51, fet, 4, len(ntc)])  # sw version, RSOC, FET, cells, NTC count
    for raw in ntc:
        body += struct.pack(">H", raw)
    return body


def test_jbd_basic_info_status_fields_idle():
    state = p.jbd_parse_basic_info(_jbd_basic_info())
    assert state.voltage == 13.18
    assert state.total_capacity == 314.0
    assert state.soc == 51
    assert state.cell_count == 4
    assert state.firmware_version == 0x21
    assert state.charge_fet_on is True
    assert state.discharge_fet_on is True
    assert state.protection_status == 0
    assert state.active_protections == []
    assert state.balance_status == 0
    assert state.balancing_cells == []
    assert state.protection_active("cell_overvoltage") is False


def test_jbd_basic_info_cell_ovp_trips_charge_fet():
    # Cell OVP (bit 0) tripped, charge FET off, discharge FET on, cell 1 balancing.
    state = p.jbd_parse_basic_info(_jbd_basic_info(protection=0x0001, fet=0x02, balance=0b0001))
    assert state.charge_fet_on is False
    assert state.discharge_fet_on is True
    assert state.protection_active("cell_overvoltage") is True
    assert state.protection_active("discharge_overcurrent") is False
    assert state.active_protections == ["cell_overvoltage"]
    assert state.balancing_cells == [1]


def test_jbd_basic_info_multiple_protections_and_high_balance_bits():
    prot = (1 << 5) | (1 << 9) | (1 << 12)
    state = p.jbd_parse_basic_info(_jbd_basic_info(protection=prot, balance=(1 << 17) | 0b1000))
    assert state.active_protections == [
        "charge_undertemperature",
        "discharge_overcurrent",
        "mos_software_lock",
    ]
    assert state.balancing_cells == [4, 18]


def test_jbd_basic_info_short_payload_leaves_status_none():
    state = p.jbd_parse_basic_info(_jbd_basic_info()[:12])
    assert state.protection_status is None
    assert state.charge_fet_on is None
    assert state.balance_status is None
    assert state.active_protections == []
    assert state.protection_active("cell_overvoltage") is None


def test_jbd_write_frame_matches_app_restart():
    # WattCycle app: buildWriteFrame(14, [0x81, 0x18]) -> DD 5A 0E 02 81 18 FF 57 77
    assert p.jbd_build_restart_frame() == bytes.fromhex("dd5a0e028118ff5777")


def test_jbd_write_frame_checksum_general():
    # controlMos(charge, on): -(0xFB + 2 + 1 + 0) & 0xFFFF
    frame = p.jbd_build_write_frame(p.JBD_CMD_CONTROL_MOS, bytes([1, 0]))
    assert frame[:4] == bytes([0xDD, 0x5A, 0xFB, 0x02])
    assert frame[4:6] == bytes([1, 0])
    assert struct.unpack(">H", frame[6:8])[0] == (-(0xFB + 2 + 1)) & 0xFFFF
    assert frame[-1] == 0x77


def test_jbd_ack_helpers():
    assert p.JbdAck(0x0E, 0).ok
    assert p.JbdAck(0x0E, 0).error == "ok"
    assert p.JbdAck(0x0E, 0x80).error == "command not supported"
    assert p.JbdAck(0x0E, 0x83).error == "password mismatch"
    assert p.JbdAck(0x0E, 0x7F).error == "status 0x7f"


def test_jbd_basic_info_real_frame_discover_314ah():
    # Captured 2026-09-07 with cell OVP latched (payload of dd03002f...f72d77).
    payload = bytes.fromhex(
        "054000007aa77aa8000334ac00000000000135640204040b630b5c0b5a0b73"
        "0080017aa87aa7000000001900000000"
    )
    s = p.jbd_parse_basic_info(payload)
    assert s.voltage == 13.44
    assert s.current == 0.0
    assert s.remaining_capacity == 313.99
    assert s.total_capacity == 314.0
    assert s.cycles == 3
    assert s.firmware_version == 0x35
    assert s.soc == 100
    assert s.cell_count == 4
    assert s.cell_temperatures == [18.4, 17.7, 17.5, 20.0]
    assert s.protection_status == 1
    assert s.active_protections == ["cell_overvoltage"]
    assert s.charge_fet_on is False
    assert s.discharge_fet_on is True
    assert s.heating_on is False
    assert s.warning_status == 0x8001
    assert s.active_warnings == ["cell_high_voltage"]
    assert s.balance_current == 0.0
    assert s.protocol_version == "2.5"


def test_jbd_basic_info_100ma_unit_flag_rescales():
    payload = bytearray(_jbd_basic_info(fet=0x83))  # bit 7 set: 100 mA units
    s = p.jbd_parse_basic_info(bytes(payload))
    assert s.remaining_capacity == 1611.5  # 16115 / 10
    assert s.total_capacity == 3140.0
    assert s.charge_fet_on and s.discharge_fet_on


def test_jbd_extended_protection_bits():
    s = p.jbd_parse_basic_info(_jbd_basic_info(protection=(1 << 13) | (1 << 15)))
    assert s.active_protections == ["charge_mos_broken", "mos_overtemperature"]


REAL_RECORD = bytes.fromhex(
    "012c0006060243170a1740050000a77aa87a01000100580b560b00005d0b280d1e0d01040200"
    "280d1f0d1f0d1e0d100e100e100e100e100e100e100e100e100e100e100e"
)


def test_jbd_record_real_frame_discover_314ah():
    # 0x08 payload captured 2026-09-07 09:57 UTC with cell OVP latched.
    rec = p.jbd_parse_fault_record(REAL_RECORD)
    assert rec is not None
    assert rec.header == bytes.fromhex("012c0006060243170a17")
    assert rec.sequence == 6
    assert rec.voltage == 13.44 and rec.current == 0.0
    assert rec.remaining_capacity == 313.99 and rec.nominal_capacity == 314.0
    assert rec.active_protections == ["cell_overvoltage"]
    assert rec.active_warnings == ["cell_high_voltage"]
    assert rec.temperatures == [17.3, 17.1, None, 17.8]
    assert rec.max_cell_voltage == 3.368 and rec.max_cell_index == 1
    assert rec.min_cell_voltage == 3.358 and rec.min_cell_index == 4
    assert rec.charge_fet_on is False and rec.discharge_fet_on is True
    assert rec.cell_voltages == [3.368, 3.359, 3.359, 3.358]
    assert "cell overvoltage" in rec.summary() and "3.368" in rec.summary()


def test_jbd_record_key_is_header_and_short_payload_rejected():
    a = p.jbd_parse_fault_record(REAL_RECORD)
    b = p.jbd_parse_fault_record(REAL_RECORD[:3] + bytes([5]) + REAL_RECORD[4:])
    assert a.key != b.key and b.sequence == 5
    assert p.jbd_parse_fault_record(REAL_RECORD[:30]) is None


def test_jbd_record_info_and_clock_real_frames():
    # 0x07 reply 2026-09-07: dd 07 00 04 | 00 e2 01 2c | fe ed 77
    info = p.jbd_parse_record_info(bytes.fromhex("00e2012c"))
    assert info == p.JbdRecordInfo(index=226, capacity=300)
    assert p.jbd_parse_record_info(b"\x00") is None
    # 0x06 reply: dd 06 00 06 | 15 01 00 04 02 01 | ff dd 77 — BCD ss mm hh dd MM yy
    clock = p.jbd_parse_clock(bytes.fromhex("150100040201"))
    assert clock.hex == "15 01 00 04 02 01"
    assert clock.bms_datetime == datetime(2000, 2, 4, 0, 1, 15)  # yy ignored
    assert clock.days_running == 34
    assert clock.time_since_restart == timedelta(minutes=1, seconds=15)
    later = p.jbd_parse_clock(bytes.fromhex("351600040201"))
    assert later.elapsed - clock.elapsed == timedelta(minutes=15, seconds=20)
    # After the 12:52 restart: time of day reset, day counter kept, yy flipped 01 -> 00.
    rebooted = p.jbd_parse_clock(bytes.fromhex("090400040200"))
    assert rebooted.days_running == 34
    assert rebooted.time_since_restart == timedelta(minutes=4, seconds=9)
    assert rebooted.elapsed < later.elapsed  # this is how a restart is detected
    assert p.jbd_parse_clock(b"") is None
    assert p.jbd_parse_clock(bytes.fromhex("ffffffffffff")).fields is None


def test_jbd_record_header_datetime():
    rec = p.jbd_parse_fault_record(REAL_RECORD)  # header 01 2c 00 06 06 02 43 17 0a 17
    assert rec.bms_datetime(2) == datetime(2000, 2, 3, 23, 10, 23)
    assert rec.unknown_byte5 == 2
    rolled = p.jbd_parse_fault_record(bytes.fromhex("012c0000060244000012") + REAL_RECORD[10:])
    assert rolled.bms_datetime(2) == datetime(2000, 2, 4, 0, 0, 18)
    # After the restart byte 5 read 00 while the clock still said month 02.
    after = p.jbd_parse_fault_record(bytes.fromhex("012c0000060044000f00") + REAL_RECORD[10:])
    assert after.unknown_byte5 == 0
    assert after.bms_datetime(2) == datetime(2000, 2, 4, 0, 15, 0)
