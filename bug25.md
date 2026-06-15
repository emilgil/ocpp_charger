# Bug 25 – Utan kabel planeras för bilen med lägst SOC istället för active_vehicle

**Datum:** 2026-06-15
**Status:** ✅ Åtgärdad, deployad och verifierad på live HA 2026-06-15

> **Live-verifiering:** Före restart loggade no-cable-grenen `planning for Kia eNiro soc=50%`
> (lägst SOC). Efter deploy: `planning for active vehicle Skoda Enyaq soc=60%`, energy 25.11 kWh
> (60→90% av Skodas 77 kWh) – följer dropdown-valet. (Första planen direkt efter omstart visade
> `soc=0%` eftersom Skodas SOC-sensor inte hunnit återställas; nästa throttle-omräkning (~5 min)
> plockade upp verkliga 60%.)

---

## Symptom

När ingen kabel är inkopplad beräknas laddplanen (och dashboardens grafer) för
**bilen med lägst SOC** – oavsett vad användaren valt i dropdown-entiteten
`select.ev_charger_garocs_48671aa056e80_active_vehicle`.

Konkret exempel (2026-06-15 15:58):
- Dropdown visar: **Skoda Enyaq – 77.0 kWh**
- Loggen säger: `Multi-vehicle: no cable, planning for Kia eNiro soc=52%`
- Grafen och savings-sensorn visar plan för Kia (4.1 kW, 1-fas 6A)

---

## Rotorsak

I `_update_charge_plan()` i `__init__.py`, rad 1615–1638:

```python
else:
    # No cable – pick vehicle with lowest SoC to show upcoming charging need
    best_vehicle = None
    lowest_soc = float("inf")
    for v in self._vehicles:
        ...
        if v_soc < lowest_soc:
            lowest_soc = v_soc
            best_vehicle = v
    if best_vehicle:
        current_soc = lowest_soc ...
        battery_capacity = float(best_vehicle.get(VEHICLE_CAPACITY, ...))
```

Logiken ignorerar `self.active_vehicle` och itererar alltid över alla fordon
för att hitta det med lägst SOC. Det innebär att dashboardens plan aldrig
matchar det valda fordonet när kabeln är urkopplad.

---

## Fix

Ersätt `else`-grenen (rad 1615–1638) så att `active_vehicle` används även utan
kabel. SOC läses från fordonets konfigurerade `VEHICLE_SOC_ENTITY`.

### `__init__.py` – `_update_charge_plan()`, rad 1615–1638

**Före:**
```python
            else:
                # No cable – pick vehicle with lowest SoC to show upcoming charging need
                best_vehicle = None
                lowest_soc = float("inf")
                for v in self._vehicles:
                    soc_ent = v.get(VEHICLE_SOC_ENTITY, "")
                    soc_st = self.hass.states.get(soc_ent) if soc_ent else None
                    try:
                        v_soc = float(soc_st.state) if soc_st else float("inf")
                    except (ValueError, TypeError):
                        v_soc = float("inf")
                    if v_soc < lowest_soc:
                        lowest_soc = v_soc
                        best_vehicle = v
                if best_vehicle:
                    current_soc = lowest_soc if lowest_soc != float("inf") else 0.0
                    target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                    battery_capacity = float(best_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
                    _LOGGER.debug("[ChargePlanner] Multi-vehicle: no cable, planning for %s soc=%.0f%%",
                        best_vehicle.get(VEHICLE_NAME, "?"), current_soc)
                else:
                    current_soc = self.ocpp.state.soc_percent or 0.0
                    target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                    battery_capacity = self.battery_capacity_kwh
```

**Efter:**
```python
            else:
                # No cable – plan for the selected active vehicle (Bug 25)
                vehicle = self.active_vehicle or (self._vehicles[0] if self._vehicles else None)
                if vehicle:
                    soc_ent = vehicle.get(VEHICLE_SOC_ENTITY, "")
                    soc_st = self.hass.states.get(soc_ent) if soc_ent else None
                    try:
                        v_soc = float(soc_st.state) if soc_st else None
                    except (ValueError, TypeError):
                        v_soc = None
                    current_soc = v_soc if v_soc is not None else 0.0
                    target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                    battery_capacity = float(vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
                    _LOGGER.debug("[ChargePlanner] Multi-vehicle: no cable, planning for active vehicle %s soc=%.0f%%",
                        vehicle.get(VEHICLE_NAME, "?"), current_soc)
                else:
                    current_soc = self.ocpp.state.soc_percent or 0.0
                    target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                    battery_capacity = self.battery_capacity_kwh
```

---

## Berörda filer

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `else`-grenen i `_update_charge_plan()` rad 1615–1638 ersätts |

Inga andra filer berörs.

---

## Verifiering

```bash
grep -n "no cable, planning for" \
  /mnt/c/temp/github/claude/ocpp_charger/custom_components/ocpp_charger/__init__.py
```

Förväntat: bara en träff, med texten `planning for active vehicle`.

Manuellt test i HA:
1. Kabel urkopplad
2. Välj **Skoda Enyaq** i dropdown
3. Vänta en polling-cykel (~60s) eller trigga omräkning
4. Kontrollera loggen: `Multi-vehicle: no cable, planning for active vehicle Skoda Enyaq`
5. Kontrollera att `power=` i loggen är ~11 kW (3-fas 16A), inte 4.1 kW (1-fas 6A)
6. Byt till **Kia eNiro** i dropdown → planen ska omedelbart räknas om för Kia

---

## Notering

Den tidigare lägst-SOC-logiken syftade till att visa "nästa bil som behöver
laddas". Det behovet täcks bättre av att användaren aktivt väljer fordon i
dropdown – att systemet tyst override:ar valet är mer förvirrande än hjälpsamt.
