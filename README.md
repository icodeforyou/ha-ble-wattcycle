# WattCycle BLE Battery — Home Assistant integration

Home Assistant / HACS custom integration for **WattCycle LiFePO4 batteries** (and compatible
JBD/Xiaoxiang-based packs) over Bluetooth Low Energy. Works through **ESPHome Bluetooth proxies**
— no local Bluetooth on the Home Assistant host is required.

> Reverse-engineered for interoperability under EU Directive 2009/24/EC Art. 6. Not affiliated
> with or endorsed by WattCycle. No firmware or app code is redistributed.

## Status

- **Read path (telemetry): confirmed against a WattCycle DISCOVER 12V 314Ah** (which speaks the
  JBD protocol over WattCycle's `fff0` GATT service). Current sign verified: positive = charging.
- **Write path (`send_raw` service): unverified and dangerous.** A BMS can disconnect the
  battery, change protection parameters and balancing. See [docs/TESTPLAN.md](docs/TESTPLAN.md).

## Features

- Bluetooth auto-discovery + config flow (through a proxy or a local adapter).
- A `DataUpdateCoordinator` that keeps one connection open and polls the analog-quantity register.
- Sensors: pack voltage, current, power, SoC, SoH, remaining/total/design capacity, cycles,
  MOSFET/PCB temperature, per-cell voltages, min/max/delta cell voltage.
- Binary sensors: charging / discharging (from current sign, positive = charging), and on JBD
  packs the BMS's own status: charge/discharge MOSFET state, active balancing (with the cell
  list as an attribute), an overall *Protection active* flag and one diagnostic sensor per
  protection (cell/pack over- and undervoltage, charge/discharge over- and under-temperature,
  overcurrent, short circuit, IC error, MOSFET software lock). These tell you *why* a charge
  stopped: charge FET off + cell overvoltage tripped is the BMS protecting itself; charge FET
  on with zero current is the charger finishing.
- Self-heating status and BMS warnings (advisory flags, with the list as an attribute).
- **Restart BMS** button and `restart_bms` service — the app's "Reboot system", for clearing a
  latched protection such as cell overvoltage after a full charge (JBD packs; **unverified
  against hardware until the first field test**, see docs/TESTPLAN.md phase D).
- **BMS clock and snapshot log**: the BMS clock (JBD 0x06, BCD, never set → counts from its
  epoch) gives a *BMS started* sensor and restart detection; the BMS keeps 300 five-minute
  snapshots (0x07 index, 0x08 records, little-endian — unlike the app's parser), read one per
  poll and exposed as *BMS log index*, *Last BMS log record* (record as attributes) and in
  diagnostics.
- Diagnostics that dump the last raw TX/RX frames, the last write acknowledgement, the event
  log and the decoded state.
- `send_raw` service for continued protocol exploration.
- Optional HiLink auth handshake for WATT modules that gate the data path.

## Supported protocols

Detected automatically from the advertisement / GATT profile:

| Type | Service | Notes |
|------|---------|-------|
| WATT | `fff0`  | WattCycle's own Modbus-like protocol (primary) |
| JBD  | `ff00`  | JBD / Xiaoxiang (standard) |

See [docs/PROTOCOL.md](docs/PROTOCOL.md) for the full protocol reference.

## Installation (HACS)

1. Add this repository as a custom repository in HACS (category: Integration).
2. Install **WattCycle BLE Battery** and restart Home Assistant.
3. The battery should be auto-discovered under Settings → Devices & Services. Otherwise add it
   manually. **Close the WattCycle phone app first** — the battery usually accepts only one BLE
   connection at a time.

## Field verification

```bash
pip install bleak
python3 tools/probe.py --scan
python3 tools/probe.py --address <MAC> --once          # one telemetry read
python3 tools/probe.py --address <MAC> --once --auth    # if data stays empty (writes HiLink)
```

Compare the decoded values against the app and an external meter, and confirm the current sign
convention while charging. Then follow [docs/TESTPLAN.md](docs/TESTPLAN.md) before touching any
write command.

## Development

`custom_components/ha_ble_wattcycle/protocol.py` is pure Python with no Home Assistant or bleak
dependency, so it can be unit-tested directly:

```bash
python3 -m pytest tests/
```

## License

MIT — see [LICENSE](LICENSE).
