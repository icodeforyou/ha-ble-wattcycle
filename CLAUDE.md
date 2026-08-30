# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

A Home Assistant / HACS custom integration for **WattCycle LiFePO4 batteries** (and
JBD/Xiaoxiang-compatible packs) over BLE, working **through ESPHome Bluetooth proxies** (no local
Bluetooth on the HA host). The protocol was reverse-engineered from the WattCycle Android app for
interoperability (EU Directive 2009/24/EC Art. 6). MIT licensed.

## Repository layout

- `custom_components/ha_ble_wattcycle/` — the integration (must stay at repo root for HACS).
  - `protocol.py` — **pure** protocol logic, no HA/bleak imports. Unit-tested. Put all frame
    building/parsing/decoding here so it stays testable in isolation.
  - `coordinator.py` — `WattCycleConnection` (holds one BLE link open, request/response over
    notify, `pair()`, HiLink fallback, retries, raw-frame capture) + `WattCycleCoordinator`.
  - `config_flow.py`, `__init__.py`, `entity.py`, `sensor.py`, `binary_sensor.py`,
    `diagnostics.py`, `manifest.json`, `services.yaml`, `strings.json`, `translations/`.
- `docs/PROTOCOL.md` — full protocol reference (WATT + JBD, UUIDs, DP 140 telemetry, HiLink auth,
  advertisement layout). **Source of truth — update it when protocol understanding changes.**
- `docs/TESTPLAN.md` — cautious read-before-write hardware verification plan.
- `tools/probe.py` — standalone bleak probe for field testing in the vehicle.
- `tests/test_protocol.py` — unit tests for `protocol.py`.
- `.github/workflows/release.yml` — auto-publishes a GitHub release from `manifest.json` version
  on push to `main` (HACS picks it up).

Gitignored (not committed): `WattCycle.apk`, `extracted/`, `decompiled/`, `tools/jadx/`.

## Protocol essentials (see docs/PROTOCOL.md for detail)

- App is native Kotlin (`com.gz.wattcycle`, FastBLE). Supports WATT (own), JBD, JK, JDY, BMC.
- **WATT** (primary): service `fff0`, write `fff2`, notify `fff1`, auth `fffa`.
  Frame `7E [flag] [addr=01] [func 03=read/06=write] reg(2 BE) len(2 BE) data CRC(2) 0D`.
  CRC = standard **Modbus CRC-16** (0xA001, init 0xFFFF), transmitted little-endian.
  Telemetry read via **DP 140 (0x8C)** -> `AnalogQuantify` (see `watt_decode_analog_quantity`).
- **JBD**: standard Xiaoxiang. Service `ff00`/write `ff02`/notify `ff01`,
  frame `DD A5 <cmd> 00 chk(2 BE) 77`, chk = 0x10000 - sum.
- Device-type detection: manufacturer id `0x2000`=JBD, `0x0B65`=JK; WATT via GATT service `fff0`.
- **HiLink auth**: WATT may require writing ASCII `"HiLink"` to `fffa` to unlock data. Off by
  default (`use_hilink_auth`); enable as fallback on GATT status 5 / empty telemetry.

## Commands

```bash
# Unit tests (protocol.py is pure; needs no HA install)
python3 -m pytest tests/ -q

# Field probe in the vehicle (phone app MUST be closed)
pip install bleak
python3 tools/probe.py --scan
python3 tools/probe.py --address <MAC> --once [--auth]
```

## Safety — read path vs write path

- **Read path (telemetry) is safe** and is what the integration relies on.
- **Write path is unverified and dangerous.** A BMS can disconnect the battery, change protection
  parameters and balancing. Only exposed via the `send_raw` service, clearly marked dangerous.
  Never add write entities without following `docs/TESTPLAN.md` (verify one command at a time
  against real hardware first).

## Conventions

- Keep `protocol.py` free of Home Assistant and bleak imports; add tests when changing it.
- Values decoded from the app but not yet confirmed against hardware must be marked
  **(unverified)** in code comments and docs — notably the **current sign convention**
  (charge vs discharge) and advertisement telemetry scales.
- To cut a release: bump `version` in `manifest.json`, commit, push to `main`.

## Current status

Read path decoded and unit-tested; **not yet confirmed against real hardware**. Pending field
verification with `tools/probe.py`: telemetry vs app/meter, current sign, and whether HiLink is
required. Passive advertisement telemetry (SoC/V/A without connecting) is documented but not yet
implemented.
