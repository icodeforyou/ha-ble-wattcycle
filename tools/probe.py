#!/usr/bin/env python3
"""Fristående BLE-probe för WattCycle-batterier (endast bleak, ingen Home Assistant).

Testverktyg för bilen. Ansluter till ett WattCycle-BMS via lokal Bluetooth ELLER en ESPHome
BLE-proxy (samma som HA använder), prenumererar på notify, hexdumpar alla ramar åt båda håll
med avkodning, och kan skicka läskommandon.

Protokollet är reverse-engineerat ur WattCycle.apk — se docs/PROTOCOL.md. Läsvägen (telemetri)
betraktas som säker. Skrivvägen är AVSTÄNGD i detta verktyg utom via --send-raw.

Beroenden:
    pip install bleak

Exempel:
    python3 probe.py --scan
    python3 probe.py --address AA:BB:CC:DD:EE:FF
    python3 probe.py --address AA:BB:CC:DD:EE:FF --auth      # skriv HiLink först
    python3 probe.py --address AA:BB:CC:DD:EE:FF --once      # en läsning, avsluta
    python3 probe.py --address AA:BB:CC:DD:EE:FF --send-raw 7E0001030 08C0000....0D

OBS: telefonappen måste vara HELT STÄNGD — modulen accepterar ofta bara en central i taget.
"""
from __future__ import annotations

import argparse
import asyncio
import struct
import sys
from datetime import datetime

try:
    from bleak import BleakClient, BleakScanner
    from bleak.backends.device import BLEDevice
except ImportError:
    sys.exit("Saknar bleak. Kör: pip install bleak")

# ---------------------------------------------------------------------------
# UUID:er (128-bit) per enhetstyp — se docs/PROTOCOL.md §2
# ---------------------------------------------------------------------------
def _uuid(short: str) -> str:
    return f"0000{short}-0000-1000-8000-00805f9b34fb"

PROFILES = {
    "watt": {
        "service": _uuid("fff0"), "write": _uuid("fff2"),
        "notify": _uuid("fff1"), "auth": _uuid("fffa"),
        "name_prefixes": ("XDZN", "WT"),
    },
    "jbd": {
        "service": _uuid("ff00"), "write": _uuid("ff02"),
        "notify": _uuid("ff01"), "auth": None,
        "name_prefixes": ("WT",),
    },
    "jk": {
        "service": _uuid("ffe0"), "write": _uuid("ffe1"),
        "notify": _uuid("ffe1"), "auth": None,
        "name_prefixes": ("WT", "60"),
    },
    "jdy": {
        "service": _uuid("ffe0"), "write": _uuid("ffe1"),
        "notify": _uuid("ffe1"), "auth": None,
        "name_prefixes": ("JDY",),
    },
}

HILINK = b"HiLink"

# WATT-ramkonstanter
W_HEAD = 0x7E
W_TAIL = 0x0D
W_FUNC_READ = 0x03
W_ADDR = 0x01
DP_ANALOG_QUANTITY = 140

# ---------------------------------------------------------------------------
# Modbus CRC-16 (poly 0xA001, init 0xFFFF). WATT sänder CRC little-endian på tråden.
# ---------------------------------------------------------------------------
def modbus_crc16(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            if crc & 1:
                crc = (crc >> 1) ^ 0xA001
            else:
                crc >>= 1
    return crc & 0xFFFF


def watt_build_read_frame(register: int, read_count: int = 0, info_data: bytes | None = None,
                          head: int = W_HEAD) -> bytes:
    body = bytearray()
    body.append(head)
    body.append(0x01 if info_data else 0x00)
    body.append(W_ADDR)
    body.append(W_FUNC_READ)
    body += struct.pack(">H", register)
    body += struct.pack(">H", read_count)
    if info_data:
        body += info_data
    crc = modbus_crc16(bytes(body))
    body += struct.pack("<H", crc)  # low byte first (se PROTOCOL.md §3.1)
    body.append(W_TAIL)
    return bytes(body)


def watt_build_info_data(addr: int = 1, voltage_count: int = 32, temperature_count: int = 32) -> bytes:
    return struct.pack(">HBHH", 5, addr, voltage_count, temperature_count)


def watt_analog_read_frames() -> list[tuple[str, bytes]]:
    """Alla varianter i prob-ordning: huvud 0x7E/0x1E, med/utan infoData.

    Appens detectProductHeader provar 0x7E först och faller tillbaka till 0x1E.
    Svaren börjar alltid med 0x7E oavsett.
    """
    info = watt_build_info_data()
    return [
        ("analog 7E", watt_build_read_frame(DP_ANALOG_QUANTITY)),
        ("analog 7E+info", watt_build_read_frame(DP_ANALOG_QUANTITY, info_data=info)),
        ("analog 1E", watt_build_read_frame(DP_ANALOG_QUANTITY, head=0x1E)),
        ("analog 1E+info", watt_build_read_frame(DP_ANALOG_QUANTITY, info_data=info, head=0x1E)),
    ]


def jbd_build_read_frame(cmd: int) -> bytes:
    # DD A5 <cmd> 00 <chk_hi> <chk_lo> 77 ; chk = 0x10000 - sum(cmd+len+data), len=0
    chk = (0x10000 - ((cmd + 0) & 0xFFFF)) & 0xFFFF
    return bytes([0xDD, 0xA5, cmd, 0x00]) + struct.pack(">H", chk) + bytes([0x77])


# ---------------------------------------------------------------------------
# Avkodning
# ---------------------------------------------------------------------------
def parse_watt_current_negative(b0: int, b1: int) -> float:
    neg = b0 & 0x80
    scale = b0 & 0x40
    mag = b1 | ((b0 & 0x3F) << 8)
    val = mag / 10.0 if scale else float(mag)
    return -val if neg else val


def decode_watt_frame(data: bytes) -> dict | None:
    """Tolka en komplett WATT-svarsram (7E ... 0D)."""
    if len(data) < 11 or data[0] != W_HEAD or data[-1] != W_TAIL:
        return None
    version = data[1]
    address = data[2]
    func = data[3]
    register = struct.unpack(">H", data[4:6])[0]
    length = struct.unpack(">H", data[6:8])[0]
    payload = data[8:8 + length]
    crc_wire = struct.unpack("<H", data[8 + length:8 + length + 2])[0] if len(data) >= 8 + length + 3 else None
    crc_calc = modbus_crc16(data[0:8 + length])
    return {
        "version": version, "address": address, "func": func, "register": register,
        "length": length, "payload": payload, "crc_wire": crc_wire, "crc_calc": crc_calc,
        "crc_ok": crc_wire == crc_calc if crc_wire is not None else None,
    }


def decode_analog_quantity(payload: bytes) -> dict:
    """Avkoda DP 140-payload enligt handleAnalogQuantifyResponse (PROTOCOL.md §4)."""
    o = 0
    def u8():
        nonlocal o; v = payload[o]; o += 1; return v
    def u16():
        nonlocal o; v = struct.unpack(">H", payload[o:o+2])[0]; o += 2; return v
    def u32():
        nonlocal o; v = struct.unpack(">I", payload[o:o+4])[0]; o += 4; return v
    def i32():
        nonlocal o; v = struct.unpack(">i", payload[o:o+4])[0]; o += 4; return v
    def current2():
        nonlocal o; v = parse_watt_current_negative(payload[o], payload[o+1]); o += 2; return v

    out: dict = {}
    cell_count = u8()
    out["cell_count"] = cell_count
    out["cell_voltages_V"] = [round(u16() / 1000.0, 3) for _ in range(cell_count)]
    temp_count = u8()
    out["temperature_count"] = temp_count
    out["mos_temperature_C"] = round((u16() - 2730) / 10.0, 1)
    out["pcb_temperature_C"] = round((u16() - 2730) / 10.0, 1)
    out["cell_temperatures_C"] = [round((u16() - 2730) / 10.0, 1) for _ in range(max(temp_count - 2, 0))]
    out["current_A"] = round(current2(), 2)
    out["module_voltage_V"] = round(u16() / 100.0, 2)
    out["remaining_capacity_Ah"] = round(u16() / 10.0, 1)
    out["total_capacity_Ah"] = round(u16() / 10.0, 1)
    out["cycle_number"] = u16()
    out["design_capacity_Ah"] = round(u16() / 10.0, 1)
    out["soc_pct"] = u16()
    remaining = len(payload) - o
    if remaining >= 18:
        out["soh_pct"] = u16()
        out["cumulative_capacity_Ah"] = round(u32() / 10.0, 1)
        out["remaining_time_min"] = i32()
        u16(); u16(); u16()  # reserverade
        out["balance_current_A"] = round(current2(), 2)
    return out


# ---------------------------------------------------------------------------
# Hjälp: hexdump
# ---------------------------------------------------------------------------
def hexdump(data: bytes) -> str:
    return " ".join(f"{b:02x}" for b in data)


def ts() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# ---------------------------------------------------------------------------
# Scan
# ---------------------------------------------------------------------------
async def do_scan(timeout: float) -> None:
    print(f"Skannar {timeout:.0f}s efter BLE-enheter...\n")
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for _addr, (dev, adv) in sorted(devices.items(), key=lambda kv: -(kv[1][1].rssi or -999)):
        name = dev.name or adv.local_name or "?"
        mfg = adv.manufacturer_data or {}
        mfg_str = ", ".join(f"0x{cid:04x}:{hexdump(bytes(v))}" for cid, v in mfg.items())
        guess = guess_profile(name, adv)
        print(f"{dev.address}  rssi={adv.rssi:>4}  name={name!r}  {('['+guess+']') if guess else ''}")
        if mfg_str:
            print(f"    mfg: {mfg_str}")
            for cid, v in mfg.items():
                decoded = decode_advert(cid, bytes(v))
                if decoded:
                    print(f"    advert-telemetri: {decoded}")
        if adv.service_uuids:
            print(f"    services: {', '.join(adv.service_uuids)}")


def guess_profile(name: str | None, adv) -> str | None:
    for cid in (adv.manufacturer_data or {}):
        if cid == 0x2000:
            return "jbd"
        if cid == 0x0B65:
            return "jk"
    svc = {s.lower() for s in (adv.service_uuids or [])}
    for prof, cfg in PROFILES.items():
        if cfg["service"].lower() in svc:
            return prof
    if name:
        for prof, cfg in PROFILES.items():
            if any(name.startswith(p) for p in cfg["name_prefixes"]):
                return prof
    return None


def decode_advert(cid: int, data: bytes) -> dict | None:
    """Grov telemetri ur manufacturer-data (PROTOCOL.md §6). Skalor overifierade."""
    # bleak ger payload EFTER manufacturerId. App-layouten räknar från AD-strukturens start;
    # här tolkar vi payload direkt: [protoVer, encrypt, deviceType, soc, voltage, current, ...]
    if len(data) < 6:
        return None
    return {
        "manufacturer_id": f"0x{cid:04x}",
        "raw": hexdump(data),
        "note": "byte-offset kan behöva justeras mot verklig advert; se PROTOCOL.md §6",
    }


# ---------------------------------------------------------------------------
# Connect + poll
# ---------------------------------------------------------------------------
class Probe:
    def __init__(self, profile: str, do_auth: bool):
        self.cfg = PROFILES[profile]
        self.profile = profile
        self.do_auth = do_auth
        self._rx = bytearray()

    def _on_notify(self, _char, data: bytearray) -> None:
        print(f"[{ts()}] RX  {hexdump(data)}")
        if self.profile == "watt":
            self._accumulate_watt(bytes(data))

    def _accumulate_watt(self, data: bytes) -> None:
        # Ramar kan komma i flera notify-paket. Ackumulera och plocka kompletta 7E..0D-ramar.
        self._rx += data
        while True:
            start = self._rx.find(bytes([W_HEAD]))
            if start < 0:
                self._rx.clear(); return
            if start > 0:
                del self._rx[:start]
            if len(self._rx) < 8:
                return
            length = struct.unpack(">H", self._rx[6:8])[0]
            total = length + 11
            if len(self._rx) < total:
                return
            frame = bytes(self._rx[:total])
            del self._rx[:total]
            self._handle_watt_frame(frame)

    def _handle_watt_frame(self, frame: bytes) -> None:
        info = decode_watt_frame(frame)
        if not info:
            print("    (kunde inte tolka ram)")
            return
        crc_note = {True: "CRC ok", False: "CRC FEL", None: "CRC ?"}[info["crc_ok"]]
        print(f"    ram: ver={info['version']} addr={info['address']} func=0x{info['func']:02x} "
              f"reg={info['register']} len={info['length']} {crc_note}")
        if info["register"] == DP_ANALOG_QUANTITY and info["func"] == W_FUNC_READ:
            try:
                decoded = decode_analog_quantity(info["payload"])
                print("    === TELEMETRI (DP 140) ===")
                for k, v in decoded.items():
                    print(f"      {k}: {v}")
            except Exception as e:  # noqa: BLE001
                print(f"    avkodningsfel: {e}")

    async def run(self, device: BLEDevice | str, once: bool, interval: float,
                  send_raw: bytes | None, retries: int) -> None:
        last_err = None
        for attempt in range(1, retries + 1):
            try:
                await self._run_once(device, once, interval, send_raw)
                return
            except Exception as e:  # noqa: BLE001
                last_err = e
                print(f"[försök {attempt}/{retries}] fel: {e}")
                await asyncio.sleep(2.0)
        print(f"Gav upp efter {retries} försök: {last_err}")

    async def _run_once(self, device, once, interval, send_raw) -> None:
        print(f"Ansluter till {device} (profil={self.profile})...")
        async with BleakClient(device, timeout=20.0) as client:
            print(f"Ansluten: {client.is_connected}")
            # Proaktiv pairing — hanterar GATT status 5 (insufficient authentication).
            try:
                await asyncio.wait_for(client.pair(), timeout=15.0)
                print("Pairing/bonding ok (eller redan bondad)")
            except Exception as e:  # noqa: BLE001
                print(f"pair() ej tillämpligt/misslyckades (fortsätter): {e}")

            for svc in client.services:
                chars = ", ".join(c.uuid for c in svc.characteristics)
                print(f"  service {svc.uuid}: {chars}")

            if self.do_auth and self.cfg["auth"]:
                try:
                    await client.write_gatt_char(self.cfg["auth"], HILINK, response=True)
                    print(f"Skrev HiLink till auth-char {self.cfg['auth']}")
                except Exception as e:  # noqa: BLE001
                    print(f"HiLink-skrivning misslyckades: {e}")

            await client.start_notify(self.cfg["notify"], self._on_notify)
            print(f"Prenumererar på notify {self.cfg['notify']}\n")

            if send_raw is not None:
                print(f"[{ts()}] TX  {hexdump(send_raw)}  (--send-raw)")
                await client.write_gatt_char(self.cfg["write"], send_raw, response=True)
                await asyncio.sleep(3.0)
                return

            while True:
                if self.profile == "watt":
                    frames = watt_analog_read_frames()
                elif self.profile == "jbd":
                    frames = [("jbd basic", jbd_build_read_frame(0x03)),
                              ("jbd cells", jbd_build_read_frame(0x04))]
                else:
                    frames = [("jbd basic", jbd_build_read_frame(0x03))]
                for label, fr in frames:
                    print(f"[{ts()}] TX  {hexdump(fr)}  ({label})")
                    await client.write_gatt_char(self.cfg["write"], fr, response=True)
                    await asyncio.sleep(1.5)
                if once:
                    await asyncio.sleep(2.0)
                    return
                await asyncio.sleep(interval)


async def main() -> None:
    ap = argparse.ArgumentParser(description="WattCycle BLE-probe (bleak)")
    ap.add_argument("--scan", action="store_true", help="skanna och lista enheter")
    ap.add_argument("--address", help="MAC/adress att ansluta till")
    ap.add_argument("--profile", choices=list(PROFILES), default="watt",
                    help="protokollprofil (default: watt)")
    ap.add_argument("--auth", action="store_true", help="skriv HiLink till auth-char efter connect")
    ap.add_argument("--once", action="store_true", help="en läsomgång, avsluta sedan")
    ap.add_argument("--interval", type=float, default=5.0, help="pollintervall i sekunder")
    ap.add_argument("--retries", type=int, default=5, help="anslutningsförsök")
    ap.add_argument("--timeout", type=float, default=10.0, help="scan-timeout")
    ap.add_argument("--send-raw", help="skicka rå hexram (t.ex. 7e0001...0d) och lyssna")
    args = ap.parse_args()

    if args.scan or not args.address:
        await do_scan(args.timeout)
        if not args.address:
            return

    send_raw = bytes.fromhex(args.send_raw.replace(" ", "")) if args.send_raw else None
    probe = Probe(args.profile, args.auth)
    await probe.run(args.address, args.once, args.interval, send_raw, args.retries)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\nAvbruten.")
