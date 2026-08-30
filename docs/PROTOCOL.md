# WattCycle BLE-protokoll — reverse-engineering-anteckningar

Källa: dekompilering av `WattCycle.apk` (native Android/Kotlin, paket `com.gz.wattcycle`,
Kotlin 2.2.0, byggd med Gradle 8.13). BLE via biblioteket **FastBLE** (`com.clj.fastble`).
Reverse-engineering för interoperabilitet (EU-direktiv 2009/24/EC art. 6).

Alla värden bekräftade genom att läsa dekompilerad Kotlin — **ännu inte** verifierade mot ett
riktigt batteri. Fält markerade **(overifierat)** behöver fältkontroll med `tools/probe.py`.

---

## 1. Apptyp och struktur

- Native Kotlin-app, ingen Xamarin/Flutter/React Native. Ingen `lib/*.so`.
- Stödjer **fem** BMS-protokoll via en gemensam `BaseBleProtocolHandler`:
  - `WattBleProtocolHandler`  ← WattCycles egna paket (primärt mål)
  - `JbdBleProtocolHandler`   ← JBD / Xiaoxiang (välkänt protokoll)
  - `JkBleProtocolHandler`    ← JK-BMS (Jikong)
  - `JdyBleProtocolHandler`   ← JDY-modul
  - `BmcBleProtocolHandler`
- Orkestrering: `service/DeviceLifecycleCoordinator` (connect → notify → poll). Detta är den
  klass HA-coordinatorn ska efterlikna.
- En firmware-fil (`assets/WT06_20004SW10_L_07_m.bin`) för OTA — modellprefix "WT06".

---

## 2. GATT — service/characteristic-UUID:er per enhetstyp

Från `service/BatteryDeviceService$BatteryDeviceType`. Konstruktorordning:
`(namn, namnprefix[], serviceUUID, writeUUID, notifyUUID, authUUID)`.

| Typ  | Namnprefix        | Service | Write | Notify | Auth  |
|------|-------------------|---------|-------|--------|-------|
| WATT | `XDZN`, `WT`      | `fff0`  | `fff2`| `fff1` | `fffa`|
| JBD  | `WT`              | `ff00`  | `ff02`| `ff01` | —     |
| JBD_DG04SA02_4G | `WT` (+ namnlista) | `ff00` | `ff02` | `ff01` | — |
| JDY  | `JDY`             | `ffe0`  | `ffe1`| `ffe1` | —     |
| JK   | `WT`, `60`        | `ffe0`  | `ffe1`| `ffe1` | —     |

Fullständiga 128-bitars UUID:er är standard-basen, t.ex.
`0000fff0-0000-1000-8000-00805f9b34fb`.

Notera: namnprefixet `WT` delas av WATT/JBD/JK — namn räcker **inte** för typidentifiering.
Se avsnitt 6.

---

## 3. WATT-protokollet (primärt) — ramformat

Modbus-liknande ram med fast header/tail och Modbus-CRC16.

### Konstanter (`WattBleProtocolHandler`)
```
FRAME_HEAD_DEFAULT      = 0x7E   (currentFrameHead, default)
FRAME_HEAD_ALTERNATIVE  = 0x1E
FRAME_RESP_HEAD         = 0x7E
FRAME_TAIL              = 0x0D
FUNC_READ               = 0x03
FUNC_WRITE              = 0x06
DEFAULT_DEVICE_ADDRESS  = 0x01
MIN_FRAME_SIZE          = 11
```

### Läsram (buildReadFrame), big-endian
```
[0]      head            0x7E
[1]      infoFlag        0x01 om infoData bifogas, annars 0x00
[2]      device address  0x01
[3]      function        0x03 (read)
[4..5]   register (DP)   u16, t.ex. 0x008C = 140 (analog quantity)
[6..7]   readCount       u16 (ofta 0x0000)
[8..]    infoData        (valfritt, se nedan)
[..]     CRC16           2 byte, little-endian på tråden (se 3.1)
[sista]  tail            0x0D
```
CRC beräknas över byte `[0 .. N)` där `N = 8 (+ infoData.length)` — dvs allt utom CRC och tail.

### Skrivram (buildWriteFrame), big-endian
```
[0]      head            0x7E
[1]      0x00
[2]      device address  0x01
[3]      function        0x06 (write)
[4..5]   register (DP)   u16
[6..7]   data length     u16
[8..]    data            (payload)
[..]     CRC16           2 byte, little-endian
[sista]  tail            0x0D
```

### Svarsram (parseFrame)
```
[0]      head            0x7E
[1]      version         u8   (lagras; styr protokollvariant, se analog nedan)
[2]      address         u8
[3]      function        u8   (0x86 = fel-svar på skriv → förkastas)
[4..5]   register (DP)   u16
[6..7]   data length     u16
[8..8+L] data            (L byte)
[..]     CRC16           2 byte
[sista]  tail            0x0D
```
Total längd = `L + 11`. **Appen beräknar CRC på svar men verifierar inte** (resultatet
kastas). Vi bör verifiera CRC på inkommande ramar ändå (defensivt).

### 3.1 CRC16 (`util/ModbusCRC`)
Standard **Modbus CRC-16** (polynom 0xA001, init 0xFFFF), tabellbaserad. Appens `crc16()`
returnerar dock `(lo << 8) | hi` och skriver med big-endian `putShort`, vilket på tråden blir
`[lo, hi]` — dvs **precis standard Modbus wire-ordning (low byte first)**.

I Python: `struct.pack("<H", modbus_crc16(payload))`.

---

## 4. WATT — datapunkter (DP-register)

Läs telemetri via `DP_ANALOG_QUANTITY`. Övriga är i huvudsak skrivvägar (skydd/parametrar).

### Läsbara (function 0x03)
| DP (dec / hex) | Namn                     | Modell |
|----------------|--------------------------|--------|
| 140 / 0x8C     | Analog quantity (telemetri) | `AnalogQuantify` |
| 50 / 0x32      | Cell characteristics     | `CellCharacteristics` |
| 1 / 0x01       | Battery temperature      | `TemperatureParameters` |
| 120 / 0x78     | Get password             | `PasswordInfo` |

(även: collection board, protection parameters, warning info, product info — DP-nr ej alla
extraherade; läs `WattBleProtocolHandler.read*`-metoderna vid behov.)

### Analog quantity — läskommando
- `version < 4`: `buildReadFrame(140, readCount=0, infoData=null)`
  → `7E 00 01 03 00 8C 00 00 <crc_lo> <crc_hi> 0D`
- `version >= 4`: infoData = `buildInfoData(addr=1, voltageCount=32, temperatureCount=32)`
  = `00 05 01 00 20 00 20` (u16=5, u8=1, u16=32, u16=32)
  → `7E 01 01 03 00 8C 00 00 00 05 01 00 20 00 20 <crc_lo> <crc_hi> 0D`

Version fås ur svarsramens byte [1]. Börja med v<4-varianten; om svaret är tomt/kort, prova
v>=4-varianten.

**Alternativt ramhuvud 0x1E:** appens `detectProductHeader` provar först huvud `0x7E`; vid
timeout provas samma kommando med huvud `0x1E` ("协议头 0x1E 也无响应" i loggsträngarna), och
det huvud som svarar används sedan för alla förfrågningar (`currentFrameHead`). **Svar börjar
alltid med `0x7E`** oavsett förfrågningshuvud (`parseFrame`/`calculateExpectedLength` kräver
0x7E). Proba därför i ordning: 7E → 7E+info → 1E → 1E+info.

### Analog quantity — svarets `data`-fält (big-endian), enligt `handleAnalogQuantifyResponse`
```
off  fält
0    cellCount            u8
1..  cellVoltages         cellCount × u16, /1000 → V
n    tempCount            u8
+2   mosTemperature       u16, (raw-2730)/10 → °C
+2   pcbTemperature       u16, (raw-2730)/10 → °C
+2.. cellTemperatures     (tempCount-2) × u16, (raw-2730)/10 → °C
+2   current              2 byte, se 4.1 (signerad) → A
+2   moduleVoltage        u16, /100 → V   (pack-spänning)
+2   remainingCapacity    u16, /10  → Ah
+2   totalCapacity        u16, /10  → Ah
+2   cycleNumber          u16       → cykler
+2   designCapacity       u16, /10  → Ah
+2   soc                  u16       → %
--- om >=18 byte kvar (nyare protokoll): ---
+2   soh                  u16       → %        (loggas, sparas EJ i appens modell)
+4   cumulativeCapacity   u32, /10  → Ah       (loggas, sparas EJ)
+4   remainingTime        i32       → min      (loggas, sparas EJ)
+2   (hoppas över)        u16
+2   (hoppas över)        u16
+2   (hoppas över)        u16
+2   balanceCurrent       2 byte, se 4.1 → A
```

### 4.1 Strömkodning (`parseWattCurrentNegative`)
Två byte `b0 b1`:
```
neg    = b0 & 0x80        # teckenbit
scale  = b0 & 0x40        # om satt: dela med 10
mag    = b1 | ((b0 & 0x3F) << 8)   # 14-bitars magnitud
val    = mag/10 if scale else mag
current = -val if neg else val     # Ampere
```
Det finns en enklare variant `parseWattCurrent` (bitar 0xC0>>6 == 1 eller 3 → /10, alltid
positiv) som används i `CellCharacteristics`. **(overifierat: teckenkonvention laddning vs
urladdning — kontrollera mot verkligt batteri.)**

### 4.2 Skala-hjälpare (skrivväg — för dokumentation, ej testade)
```
kapacitet → u16 = Ah*10
temperatur→ u16 = °C*10 + 2730     (avkodning: (raw-2730)/10)
spänning  → u16 = V*100 (precision 2) eller V*1000 (precision 3)
procent   → u16 = %
tid       → u16 = minuter
```

### 4.3 WATT skrivkommandon (FARLIGA — otestade)
DP-register för skrivning (function 0x06). Behandlas som farliga tills verifierade ett i taget
(se `docs/TESTPLAN.md`). Urval:
```
DP_SET_CHARGE_HIGH_TEMP_PROTECTION = 2      DP_SET_CELL_OVERVOLTAGE_PROTECTION  = 71
DP_SET_CHARGE_HIGH_TEMP_RECOVERY   = 3      DP_SET_CELL_OVERVOLTAGE_RECOVERY    = 72
DP_SET_CHARGE_LOW_TEMP_PROTECTION  = 4      DP_SET_CELL_OVERVOLTAGE_DELAY       = 73
DP_SET_CHARGE_LOW_TEMP_RECOVERY    = 5      DP_SET_CELL_UNDERVOLTAGE_PROTECTION = 74
DP_SET_DISCHARGE_HIGH_TEMP_PROT.   = 6      DP_SET_CELL_UNDERVOLTAGE_RECOVERY   = 75
DP_SET_DISCHARGE_HIGH_TEMP_RECOV.  = 7      DP_SET_CELL_UNDERVOLTAGE_DELAY      = 76
DP_SET_DISCHARGE_LOW_TEMP_PROT.    = 8      DP_SET_PACK_OVERVOLTAGE_PROTECTION  = 77
DP_SET_DISCHARGE_LOW_TEMP_RECOV.   = 9      DP_SET_PACK_UNDERVOLTAGE_PROTECTION = 80
DP_SET_FET_HIGH_TEMP_PROTECTION    = 10     DP_SET_VOLTAGE_BALANCE_PROTECTION   = 83
DP_SET_FET_HIGH_TEMP_RECOVERY      = 11     DP_SET_CHARGE_OVERCURRENT_PROT.     = 84
DP_SET_CELL_BASE_VOLTAGE           = 53     DP_SET_DISCHARGE_OVERCURRENT1_PROT. = 86
DP_SET_BALANCE_START_VOLTAGE       = 54     DP_SET_DISCHARGE_OVERCURRENT2_PROT. = 88
DP_SET_BALANCE_START_VOLTAGE_DIFF  = 55     DP_CHANGE_PASSWORD                  = 122
DP_CALIBRATE_CURRENT               = 57     DP_INPUT_PASSWORD                   = 121
DP_RESET_CURRENT                   = 134    DP_GET_PASSWORD                     = 120
```
Dessutom laddnings-/urladdnings-MOSFET-brytare (`setChargeSwitch`/`setDischargeSwitch`),
`restartSystem`, `restoreSystemDefaults`, `setSoc`, `setDesignCapacity`.

---

## 5. Autentisering — HiLink

WATT-enheter har en `authUUID = fffa`. Metoden `sendAuthKey2` skriver ASCII-strängen
**`HiLink`** (`48 69 4C 69 6E 6B`) till auth-characteristicen efter connect + service discovery.
"HiLink" är Huaweis modul-upplåsning → modulen är sannolikt en Huawei/Telink-baserad BLE-modul
som kräver detta för att låsa upp datavägen.

Ingen synlig anropare hittades i den dekompilerade koden (vissa init-metoder gick ej att
dekompilera helt), så det är osäkert om HiLink krävs **innan** notify/läsning eller bara i
vissa lägen. **Strategi för proben/integrationen:** anslut → aktivera notify på `fff1` → prova
läsning; om notify nekas (GATT status 5 / insufficient authentication) eller inga ramar kommer,
skriv `HiLink` till `fffa` och prova igen. Endast WATT har authUUID; övriga typer har ingen.

---

## 6. Enhetsidentifiering vid scanning

`BatteryDeviceService.detectDeviceType`:
1. **Manufacturer-ID** i advertisement (primärt):
   - `0x2000` (8192) = JIABADA → **JBD**
   - `0x0B65` (2917) = JIKONG → **JK**
   - annars → fortsätt till GATT.
2. **GATT-fallback**: matcha service-UUID + write-UUID mot tabellen i avsnitt 2. WATT
   identifieras här (service `fff0` + char `fff2`).

### Advertisement manufacturer-data (live-telemetri utan anslutning!)
`UnifiedBleManager.parseManufacturerData` — AD-struktur typ `0xFF`, längd-byte `0x0F` (15):
```
[+2..+8]  6 byte    id/mac-hex (formateras som sträng)
[+8..+9]  u16       manufacturerId
[+10]     u8        protocolVersion
[+11]     u8        encryptStatus
[+12]     u8        deviceType
[+13]     u8        soc          → %
[+14]     u8        voltage      → (coarse, skala overifierad)
[+15]     u8        current      → (coarse, skala overifierad)
```
(AdvData-konstruktor: `mac, manufacturerId, protocolVersion, encryptStatus, deviceType, soc,
voltage, current, manufacturerData`.)

JK har en egen advert-gren (`id/mac` = 6 byte från `[+6..+12]`, manufacturerId 0x0B65,
signaturbyte `65 0B 88 A0`).

**Konsekvens för HA:** grov SoC/spänning/ström kan läsas **passivt** ur advertisements via
BLE-proxy utan att ansluta — utmärkt som lågfrekvent fallback. Full precision kräver anslutning
+ `DP 140`. Skalfaktorerna för advert-spänning/ström är overifierade.

---

## 7. JBD / Xiaoxiang-protokollet (om enheten är JBD-typ)

Standard, välkänt JBD-protokoll (`JbdBleProtocolHandler`):
```
FRAME_START = 0xDD    READ_BIT  = 0xA5    CMD_BASIC_INFO   = 0x03
FRAME_END   = 0x77    WRITE_BIT = 0x5A    CMD_CELL_VOLTAGES= 0x04
                                          CMD_DYNAMIC_INFO = 0x01
                                          CMD_SYSTEM_TIME  = 0x06
```
Läsram: `DD A5 <cmd> 00 <chk_hi> <chk_lo> 77`, checksumma = `0x10000 − sum(cmd + len + data)`
(2 byte, big-endian). Basinfo (0x03) och cellspänningar (0x04) enligt vanlig JBD-layout
(totalspänning 10 mV, ström 10 mA signerad, SoC %, temp 0.1K−273.1, skydd 16-bit bitfält).
Se offentlig JBD/Xiaoxiang-dokumentation. Modellklasser: `model/jbd/JbdBasicInfo`,
`JbdCellVoltages`, `JbdDynamicInfo`, `JbdFaultRecord`.

Felkoder JBD: `0x80` cmd finns ej, `0x81` ogiltig operation, `0x82` checksum-fel,
`0x83` lösenord fel.

---

## 8. Anslutningsflöde (från `DeviceLifecycleCoordinator`)

1. Scan → matcha enhetstyp (avsnitt 6).
2. Connect (FastBLE; bygg in retries — HCI 0x3E-storm kan kräva flera försök).
3. Service discovery.
4. `startNotifications(service, notifyUUID)` — WATT: notify på `fff1`.
5. (WATT, vid behov) skriv `HiLink` till `fffa`.
6. Poll: skicka läsram på writeUUID (`fff2`), ta emot svar via notify, avkoda.
   Realtidspollern (`resumeRealtimeDataPoller`) läser `DP 140` upprepat.

## 9. BMC-protokollet (nyare packs — trolig för DISCOVER-serien)

`BmcBleProtocolHandler` finns i appen men är **inte inkopplad** i denna APK-version
(`createProtocolRepository` instansierar den aldrig) — halvintegrerad/kommande kod. Det är det
enda protokollet med **värmestyrning** (`CMD_HEATING_CONTROL = 0x52`), vilket pekar mot de nya
självvärmande packen. Fältenheten EE:C2:37:00:64:8C (WTaHdB1..., service fff0, ingen fffa)
svarade inte på någon WATT-variant — BMC/JBD probas därför också över samma karaktäristik.

Allt **little-endian**. Ram: `AA <cmd> <len> <data...> <chk u16 LE>`, chk = ren byte-summa av
`cmd+len+data`. Min ramlängd 5; total längd = len + 5. Handshake (0x00) förväntas före data.

Kommandon: `00` handshake, `10` tillverkarnamn, `11` packnamn, `20` running status,
`21` batteriinfo, `22` cellspänningar, `23` ström, `50/51` ladd/urladd-FET (skriv), `52` värme
(skriv), `53` clear status, `58` batteriparametrar, `5B/5C/5D` skydds-skrivningar, `66/67/68`
strömkalibrering, `F5` version.

Batteriinfo (0x21), LE: packVoltage i32/1000 → V; current i32/1000 → A (signerad);
soc u8 %; soh u8 %; remainingCapacity i32; fullCapacity i32 (enhet trolig mAh, **overifierat**);
cycleCount u16; därefter 6×u8: temp1–4, mosTemp, ambientTemp (**enkodning overifierad**).
Cellspänningar (0x22): 24 × u16 LE, mV; oanvända slots = 0.

### Fält-advertisement (verklig enhet, 2026-08-30)
```
raw: 0302f0ff | 1209 57...39 ("WTaHdB12605110139") | 0fff eec23700648c 1012 33 11 03 00 0d 00
mfr-payload: MAC(6) | 0x1012 | 0x33 | 0x11 | 0x03 | soc=0x00 | volt=0x0d(13) | curr=0x00
```
Enligt appens AdvData-layout: deviceType=3 (matchar INTE appens enum 16/17/18/48/56/64/80 —
UNKNOWN), soc=0 (misstänkt; layouten kan avvika för denna adv-revision). Namnet `WTaHdB1...`
ligger mycket nära JBD-DG04SA02-listans `WTeHdBD...` → modulfamiljen är besläktad.

## 10. Känd enhet — BEKRÄFTAD (WattCycle DISCOVER 12V 314Ah, självvärmning)

**Fältverifierad 2026-08-30** (EE:C2:37:00:64:8C, namn `WTaHdB12605110139`, via ESPHome-proxy):
Modulen exponerar WATT-familjens GATT (service `fff0`, write `fff2`, notify `fff1`, ingen `fffa`)
men talar **JBD-protokollet** över dessa karaktäristikor — "JBD bakom fff0-brygga". WATT-ramar
(0x7E/0x1E) och BMC ignoreras; `DD A5 03/04` svarar direkt. Namnmönstret `WTaHdB*`/`WTeHdB*` ⇒
JBD-tråd oavsett GATT-tjänst.

Verifierade värden mot datablad/app (diagnostics-dump):
- 4 celler à 3.295–3.296 V; packspänning 13.18 V = cellsumman ✓
- SoC 51 % (payload-byte 0x33) ✓; kvarvarande 161.15 Ah; **totalt 314.0 Ah** ✓ (datablad)
- 3 cykler (nytt batteri) ✓; 4 NTC:er 18.1–20.0 °C (0x0Bxx → (raw−2731)/10) ✓
- Exempelramar: TX `dd a5 03 00 ff fd 77` → RX `dd 03 00 2f 0526 0000 3ef3 7aa8 0003 ...`
- Ström vid vila = 0.0 A ✓; **teckenkonvention vid laddning fortfarande overifierad**.

Kvar att kartlägga: FET-statusbyte [20] (0x03 = ladd+urladd på), skyddsbitfält [16:18],
extrafält efter NTC:erna (`0080 007aa8...` — trolig utökad JBD-variant), värmestyrning.

### Ursprungliga valideringsmål (datablad)

Från officiella datablad/manualer i `docs/` (tillagda av ägaren):
- 12.8 V nominellt → **4 celler** LiFePO4 (`cell_count` ska bli 4; cellspänningar ~3.2–3.65 V).
- Designkapacitet **314 Ah** (`designCapacity` råvärde 3140 med /10-skala), 250 A BMS
  (max kontinuerlig ladd/urladd 250 A; överströmssteg 260±40 A/10 s, 333±100 A/1–2 s).
- Laddspänning 14.4±0.2 V, ladd-cutoff 14.6 V, urladd-cutoff 10±0.4 V — rimlighetsintervall
  för `moduleVoltage` ≈ 10–14.6 V.
- **Självvärmningsmodul** (aktiveras <−5 °C vid laddning, av >10 °C) — styrning/status ej
  kartlagd i WATT-protokollet ännu (JBD-varianten har `CMD_CONTROL_HEATING = 0xFD`).
- Appmanualen bekräftar: SoC i heltals-%, totalspänning/ström/effekt i realtid (~1 Hz),
  balansström, resttid till full/tom, ladd-/urladdningsbrytare, och att styrning kräver
  inloggat + "bundet" konto medan **läsning fungerar utan bindning** — stödjer att läsvägen
  är öppen och skrivvägen grindad.

Fällor att bygga för (bekräftade relevanta):
- **En central i taget** — telefonappen måste vara helt stängd under test.
- **Bonding/pairing** kan krävas → `client.pair()` proaktivt vid GATT status 5.
- **Anslutning kan kräva flera försök** → retry.
- **Statusbild släpar** → verifiera inte skrivningar för snabbt.
