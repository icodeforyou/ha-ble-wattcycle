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
