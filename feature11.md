# Feature 11 – Sensor som visar laddmålet i både % och kWh

**Datum:** 2026-10-15
**Status:** 🟡 Deployad 2026-10-15 21:28 (testfall 2–5 kvar). Beroendet "Bug 52" avser egentligen Bug 53 (kWh-mål).
**Beroende:** Bug 52 måste vara deployad först (definierar att `target_kwh > 0` har prioritet).

---

## Bakgrund

`Target Battery Level` (%) och `Target Energy` (kWh) är två separata number-entiteter.
Efter Bug 52 styr kWh-målet när det är större än 0, annars styr procentmålet. Det
framgår inte i UI vad det styrande målet motsvarar i den andra enheten, t.ex. hur
många procent 20 kWh ger, eller hur många kWh 80 % kräver.

Beslut 2026-10-15: entiteterna ska **inte** skriva till varandra. I stället visar en
separat read-only-sensor det aktiva målet i båda enheterna. Ingen entitet ändras av
sig själv och ingen risk för loopar uppstår.

---

## Önskat beteende

Ny sensor: **Charge Goal**, unique suffix `charge_goal`.
Förväntat entity_id: `sensor.ev_charger_garocs_48671aa056e80_charge_goal`.

### Vilket mål som är aktivt (samma regel som Bug 52)

| Villkor | Aktivt mål | Motsvarighet som räknas fram |
|---------|------------|------------------------------|
| `target_kwh > 0` | kWh | slut-SOC i % |
| `target_kwh == 0` | SOC % | kWh för hela sessionen |

### Basvärde (SOC som omräkningen utgår från)

Prioritetsordning:

1. `_session_start_soc`, om den finns (kabel inkopplad, session startad) → `base_source = "session_start"`
2. Aktuell rapporterad SOC för aktivt fordon → `base_source = "current"`
   - Kabel inkopplad: `self.ocpp.state.soc_percent`
   - Ingen kabel: aktivt fordons `VEHICLE_SOC_ENTITY` (samma läsning som Bug 25-grenen i `_update_charge_plan()`)
3. Ingen giltig SOC → `base_source = "missing"`

**Viktigt:** saknad SOC är `None`, aldrig `0.0`. Sensorn får inte ärva planerarens
`soc_percent or 0.0`-fallback. Det var den som gav `soc=0%` i Bug 52-loggen.

Basvärdet från sessionsstart används eftersom `target_kwh` jämförs mot hela
kabelsessionens energi (Bug 52). Då hör båda till samma referenspunkt och
motsvarigheten ligger still under laddningen, i stället för att driva med estimatet.

### Beräkning

Kapacitet = aktivt fordons `VEHICLE_CAPACITY`, fallback `self.battery_capacity_kwh`.
Verkningsgrad = `DEFAULT_CHARGE_EFFICIENCY` (0,92).

**kWh-mål aktivt:**
```
target_soc_equiv = min(100, base_soc + target_kwh × eff / kapacitet × 100)
target_kwh_equiv = target_kwh
```

**SOC-mål aktivt** (target_soc 0 → 80 som fallback, samma som planeraren):
```
target_kwh_equiv = max(0, (target_soc − base_soc) / 100 × kapacitet / eff)
target_soc_equiv = target_soc
```

Saknas `base_soc` blir motsvarigheten `None`.

### State (text)

Svensk decimalform med komma, en decimal för kWh och heltal för procent:

| Fall | State |
|------|-------|
| kWh-mål, SOC känd | `20,0 kWh ≈ 29 %` |
| kWh-mål, SOC saknas | `20,0 kWh (SOC okänd)` |
| SOC-mål, SOC känd | `80 % ≈ 31,3 kWh` |
| SOC-mål, SOC saknas | `80 % (SOC okänd)` |

Med kWh-mål och SOC känd: om `base_soc + target_kwh` skulle ge över 100 % visas `≈ 100 % (max)`.

### Attribut

| Attribut | Typ | Beskrivning |
|----------|-----|-------------|
| `active_goal` | `"kwh"` / `"soc"` | Vilket mål som styr |
| `target_kwh_equiv` | float \| None | Mål i kWh (nätsidan, hela sessionen) |
| `target_soc_equiv` | float \| None | Mål i % slut-SOC |
| `base_soc` | float \| None | SOC som omräkningen utgår från |
| `base_source` | `"session_start"` / `"current"` / `"missing"` | Varifrån basen kommer |
| `delivered_kwh` | float | `_session_total_kwh` + aktiv tx-energi (0 utan kabel) |
| `remaining_kwh` | float \| None | `max(0, target_kwh_equiv − delivered_kwh)` |
| `capacity_kwh` | float | Kapacitet som användes |
| `vehicle` | str | Aktivt fordons namn |

Avrunda floats till 1 decimal (SOC) respektive 2 decimaler (kWh).

---

## Implementation

### Berörda filer

| Fil | Ändring |
|-----|---------|
| `const.py` | Ny konstant `SENSOR_CHARGE_GOAL = "charge_goal"` |
| `__init__.py` | Ny metod `charge_goal_info() -> dict` på koordinatorn |
| `sensor.py` | Ny klass `ChargeGoalSensor`, registreras i `async_setup_entry` |
| `strings.json` / `translations/*.json` | Endast om sensornamn ligger där i dag (se pre-verify) |

### Pre-verify

```bash
cd /mnt/c/temp/github/claude/ocpp_charger/custom_components/ocpp_charger/
grep -n "SENSOR_PRICE_CAP_STATUS" const.py sensor.py          # mönster för ny konstant + registrering
grep -n "class PriceCapStatusSensor\|class OCPPSensorBase" sensor.py
grep -n "_session_start_soc\b" __init__.py | head -5
grep -n "VEHICLE_SOC_ENTITY\|VEHICLE_CAPACITY" __init__.py | head -10
grep -rn "price_cap_status\|Price Cap Status" strings.json translations/ 2>/dev/null
grep -n "target_kwh > 0" __init__.py   # bekräftar att Bug 52 är på plats
```

Om sista grep inte visar Bug 52:s `target_kwh > 0`-grenar i `_update_charge_plan()` och
`_charging_goal_reached()`: **stoppa**, Bug 52 måste deployas först.

### 1. `const.py`

```python
SENSOR_CHARGE_GOAL = "charge_goal"  # Feature 11
```

### 2. `__init__.py` – `charge_goal_info()`

All logik ligger i koordinatorn så att sensorn bara presenterar. Hjälpmetoden kan
återanvändas av t.ex. notiser senare.

```python
def charge_goal_info(self) -> dict:
    """Feature 11: aktivt laddmål uttryckt i både kWh och SOC %.

    Ren läsning – ändrar inget state. Saknad SOC ger None (aldrig 0.0).
    Aktivt mål följer Bug 52: target_kwh > 0 har prioritet.
    """
    vehicle = self.active_vehicle or {}
    capacity = float(vehicle.get(VEHICLE_CAPACITY, 0) or self.battery_capacity_kwh)
    eff = DEFAULT_CHARGE_EFFICIENCY

    # Basvärde
    base_soc: float | None = None
    base_source = "missing"
    if self._session_start_soc is not None:
        base_soc, base_source = float(self._session_start_soc), "session_start"
    elif self.ocpp.state.cable_connected:
        if self.ocpp.state.soc_percent is not None:
            base_soc, base_source = float(self.ocpp.state.soc_percent), "current"
    else:
        soc_ent = vehicle.get(VEHICLE_SOC_ENTITY, "")
        st = self.hass.states.get(soc_ent) if soc_ent else None
        if st is not None and st.state not in ("unavailable", "unknown", ""):
            try:
                base_soc, base_source = float(st.state), "current"
            except (ValueError, TypeError):
                pass
    if base_soc is not None and not (0.0 <= base_soc <= 100.0):
        base_soc, base_source = None, "missing"

    # Levererat denna kabelsession (samma referens som Bug 52)
    active_tx = self.ocpp.state.energy_kwh if self.ocpp.state.transaction_id is not None else 0.0
    delivered = (self._session_total_kwh + active_tx) if self.ocpp.state.cable_connected else 0.0

    soc_capped = False
    if self.target_kwh > 0:
        active = "kwh"
        kwh_equiv = float(self.target_kwh)
        soc_equiv = None
        if base_soc is not None and capacity > 0:
            raw = base_soc + kwh_equiv * eff / capacity * 100.0
            soc_capped = raw > 100.0
            soc_equiv = min(100.0, raw)
    else:
        active = "soc"
        soc_equiv = float(self.target_soc) if self.target_soc > 0 else 80.0
        kwh_equiv = None
        if base_soc is not None and capacity > 0:
            kwh_equiv = max(0.0, (soc_equiv - base_soc) / 100.0 * capacity / eff)

    remaining = max(0.0, kwh_equiv - delivered) if kwh_equiv is not None else None

    return {
        "active_goal": active,
        "target_kwh_equiv": round(kwh_equiv, 2) if kwh_equiv is not None else None,
        "target_soc_equiv": round(soc_equiv, 1) if soc_equiv is not None else None,
        "soc_capped": soc_capped,
        "base_soc": round(base_soc, 1) if base_soc is not None else None,
        "base_source": base_source,
        "delivered_kwh": round(delivered, 2),
        "remaining_kwh": round(remaining, 2) if remaining is not None else None,
        "capacity_kwh": round(capacity, 1),
        "vehicle": vehicle.get(VEHICLE_NAME, ""),
    }
```

CC: kontrollera att `VEHICLE_SOC_ENTITY`, `VEHICLE_CAPACITY`, `VEHICLE_NAME` och
`DEFAULT_CHARGE_EFFICIENCY` redan är importerade i `__init__.py` (de används i
`_update_charge_plan()`). Följ befintlig placering av liknande publika hjälpmetoder.

### 3. `sensor.py` – `ChargeGoalSensor`

```python
class ChargeGoalSensor(OCPPSensorBase):
    """Feature 11: aktivt laddmål i både kWh och SOC %. Read-only."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_CHARGE_GOAL, "Charge Goal")
        self._attr_icon = "mdi:target"
        # Textsensor: ingen unit, ingen state_class (annars förväntar HA numeriskt)

    @staticmethod
    def _fmt_kwh(v: float) -> str:
        return f"{v:.1f}".replace(".", ",") + " kWh"

    @staticmethod
    def _fmt_soc(v: float) -> str:
        return f"{v:.0f} %"

    @property
    def native_value(self) -> str:
        i = self._coord.charge_goal_info()
        if i["active_goal"] == "kwh":
            left = self._fmt_kwh(i["target_kwh_equiv"])
            if i["target_soc_equiv"] is None:
                return f"{left} (SOC okänd)"
            right = self._fmt_soc(i["target_soc_equiv"])
            return f"{left} ≈ {right}" + (" (max)" if i["soc_capped"] else "")
        left = self._fmt_soc(i["target_soc_equiv"])
        if i["target_kwh_equiv"] is None:
            return f"{left} (SOC okänd)"
        return f"{left} ≈ {self._fmt_kwh(i['target_kwh_equiv'])}"

    @property
    def extra_state_attributes(self) -> dict:
        return self._coord.charge_goal_info()
```

Registrera i `async_setup_entry` bredvid övriga sensorer:
```python
ChargeGoalSensor(coordinator, entry),
```

`OCPPSensorBase` antas vara en `CoordinatorEntity` (alla sensorer uppdateras via
koordinatorn i dag). Därmed uppdateras sensorn direkt när `set_target_kwh()` och
`set_target_soc()` anropar `async_set_updated_data()`. Ingen extra lyssnare behövs.

---

## Utanför scope

- **Dashboard:** sensorn och `number.…_target_energy` läggs in i Styrning-kortet i en
  separat ändring av `dashboard.yaml`.
- **Chargeable Amount** räknar fortfarande mot SOC (noterat i Bug 52). Kan senare byggas
  om ovanpå `charge_goal_info()`.
- **Inaktuell SOC:** om fordonets SOC-entitet ligger kvar på ett gammalt värde utan att bli
  `unavailable` kan det inte upptäckas här. `base_source = "current"` visar att värdet inte
  kommer från sessionsstart.

---

## Interaktion

- **Bug 52:** samma prioritetsregel och samma `delivered_kwh`-referens (hela kabelsessionen).
- **Bug 25:** basvärdet utan kabel läses på samma sätt som i planerarens no-cable-gren.
- **Bug 29/38:** `estimate_soc()` används **inte**. Sensorn utgår medvetet från SOC vid
  sessionsstart så att motsvarigheten inte driver under laddning.
- **Bug 30:** `_session_start_soc` persisteras, så `base_source = "session_start"` överlever
  omstart mitt i session.

---

## Deploy

```bash
cd /mnt/c/temp/github/claude/ocpp_charger/custom_components/ocpp_charger/
scp const.py __init__.py sensor.py root@192.168.1.97:/config/custom_components/ocpp_charger/
ssh root@192.168.1.97 "ha core restart"
```

Om `strings.json`/`translations/` ändrades: kopiera även dem.

---

## Post-deploy-verifiering

Kontrollera i Developer Tools → States att `sensor.ev_charger_garocs_48671aa056e80_charge_goal`
finns, och testa följande fall:

| Test | Inställning | Förväntad state |
|------|-------------|-----------------|
| 1 | Kia, SOC saknas, Target Energy 20 | `20,0 kWh (SOC okänd)`, `base_source: missing` |
| 2 | Skoda (77 kWh), SOC 50 %, ingen kabel, Target Energy 20 | `20,0 kWh ≈ 74 %`, `base_source: current` |
| 3 | Skoda, SOC 50 %, Target Energy 0, Target % 80 | `80 % ≈ 25,1 kWh` |
| 4 | Skoda, SOC 90 %, Target Energy 20 | `20,0 kWh ≈ 100 % (max)` |
| 5 | Ändra Target Energy | state ändras direkt, utan att vänta 5 min |

Kontrollräkning test 2: 50 + 20 × 0,92 / 77 × 100 = 73,9 → `74 %`.
Kontrollräkning test 3: (80 − 50) / 100 × 77 / 0,92 = 25,11 → `25,1 kWh`.

Inga `[ChargePlanner]`-rader ska ändras av denna feature:
```bash
grep -E "ChargePlanner\] Planning" /config/ocpp_charger_debug.log | tail -5
```
