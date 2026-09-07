# Testplan — försiktig verifiering mot riktigt batteri

Säkerhetsprincip: **läsvägen (telemetri) är säker; skrivvägen är otestad och farlig** tills
motsatsen bevisats. Ett BMS kan stänga av batteriet, ändra skyddsparametrar och balansering.
Verifiera ett kommando i taget. Ha alltid fysisk åtkomst till batteriet/frånskiljare.

Förutsättningar:
- Telefonappen HELT stängd (modulen tar ofta bara en central i taget).
- `pip install bleak`; kör `tools/probe.py` från en maskin med BLE, eller via ESPHome-proxy.

## Fas A — passiv (ingen anslutning)
1. `python3 tools/probe.py --scan` — bekräfta att batteriet syns, notera adress, namn (WT/XDZN?),
   manufacturer-ID och ev. advert-telemetri. Fastställ profil (watt/jbd/jk).

## Fas B — läsväg (säker)
2. `python3 tools/probe.py --address <MAC> --once` — anslut, prenumerera, läs DP 140 en gång.
   - Om notify nekas (GATT status 5) → kör om med `--auth` (skriver HiLink till fffa).
   - Om ingen ram kommer på v<4-varianten → v>=4-varianten skickas automatiskt.
3. Jämför avkodad telemetri mot appens skärm och en extern mätare:
   - [ ] Total spänning (module_voltage_V) mot voltmeter
   - [ ] SoC mot appen
   - [ ] Cellspänningar summerar ≈ totalspänning
   - [ ] Ström: teckenkonvention — ladda batteriet och notera om current_A blir + eller −
   - [ ] Temperaturer rimliga
   - [ ] Cykler mot appen
4. Kör kontinuerligt (`--interval 5`) en stund; kontrollera stabilitet och CRC-status.

## Fas C — skrivväg (FARLIGT, ett kommando i taget)
Gör INTE detta förrän läsvägen är helt verifierad. För varje kommando:
1. Läs och anteckna nuvarande parametervärde först.
2. Skriv ett litet, ofarligt testvärde via `--send-raw` (bygg ramen enligt PROTOCOL.md §3/§4.3).
3. Läs tillbaka och bekräfta ändringen; vänta — statusbilden kan släpa.
4. Återställ till ursprungsvärdet.

Rekommenderad ordning (minst→mest riskabelt):
- [ ] `setSoc` / kalibrering (reversibelt, låg risk)
- [ ] enskild skyddsparameter med känt värde (t.ex. cell-övervoltage recovery)
- [ ] MOSFET-brytare (`setChargeSwitch`/`setDischargeSwitch`) — kan koppla bort batteriet!
- [ ] ALDRIG `restoreSystemDefaults` under drift utan backup på alla parametrar.

Dokumentera varje verifierat skrivkommando (DP, byte-layout, effekt) allteftersom.

## Fas D — BMS-omstart (0x0E), första verkliga skrivkommandot

Förutsättningar: landström inkopplad (laddaren matar då lasten medan BMS:en startar om; utan
landström tappar allt 12 V — inklusive BLE-proxyn — strömmen en kort stund). Telefonappen stängd.

1. Notera före: `binary_sensor.*_skydd_aktivt` (förv. På), `*_laddnings_mosfet` (förv. Av),
   ström (förv. 0.00 A), Sargent leisure-spänning (förv. ~13.7 V float).
2. Tryck `button.*_starta_om_bms` (eller anropa `ha_ble_wattcycle.restart_bms`).
3. Förväntat, i ordning:
   - HA-loggen: "Sending BMS restart … dd5a0e028118ff5777" följt av "BMS acknowledged restart".
     Diagnostik → `last_ack` = `{command: 0x0e, status: ok}`. Rå-svar `dd0e0000000077`.
   - BLE-länken tappas (BMS:en startar om), integrationen återansluter vid nästa poll (~8–40 s).
   - Efter återanslutning: skydd Av, ladd-MOSFET På, och — eftersom laddaren står på 13.7 V mot
     packens ~13.4 V — en liten positiv laddström. Cell 1 kommer att stiga snabbare än de andra.
4. Avvikelser och tolkning:
   - `last_ack` status 0x80 → kommandot finns inte i denna firmware; sluta här.
   - status 0x81/0x83 → BMS:en kräver något vi inte skickar (låst läge / lösenord); sluta här.
   - ack OK men skyddet kvarstår efter återanslutning → omstart nollställer inte OVP-latchen;
     release kräver då urladdning (testa med last i stället).
5. Anteckna utfall i README/PROTOCOL.md och markera 0x0E som verifierat/ej verifierat.

## Fas E — BMS-loggposter och klocka (0x06/0x07/0x08), ren läsväg

Avklarat 2026-09-07: klockan är BCD och går i realtid; posterna är 5-min-snapshots med datum/tid i
huvudet; 0x07 = index/300. Kvar att observera:
1. Efter omstarten (fas D): *BMS startad* ska hoppa fram till omstartstidpunkten och loggen ska
   varna "BMS clock went … back". Klockan bör läsa 2001-01-01 00:00:xx direkt efter.
2. Sensorn *Senaste BMS-loggpost* ska visa en tid inom de senaste 5–10 minuterna.
3. Bekräfta epoken: uptime 34 d ⇒ start ≈ 4 aug 2026 11:13 — stämmer det med när batteriet först
   kopplades in? Om det var tidigare/senare är epoken en annan.
4. 62 °C-anomalin: återkommer poster med orimliga temperaturer och varning 0x51?
