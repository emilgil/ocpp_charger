# Bug 27 – `allow_day_charging=True` ignoreras av deadline-beräkningen

**Status:** ✅ Åtgärdad (TDD) + deployad till live HA 2026-06-17

> **Verifiering:** 15/15 deadline-tester gröna (4 nya, rödde först). Live: med `allow_day_charging=True`
> extenderas deadline till slutet av prisdata och planeraren valde dagtidsplanen
> `11:45–15:15 @ 26.0 öre/kWh` istället för natt `72.6 öre/kWh` – exakt avsedd effekt.
>
> **Undersökt under verifieringen (ingen bugg):** Deadline syntes pendla True↔False under uppstart.
> Spårning visade att det var **användaren som växlade switchen** under testet, inte intern instabilitet:
> `allow_day_charging` gick True→False medan `day_charging_manual_override` förblev `True` (`.storage/`),
> och enda kodvägen är `set_allow_day_charging(False)` via `switch.async_turn_off` (notis-actions loggar
> "User chose" – saknas; ingen automation rör entiteten). Steady state stabilt (switch av → allow=False →
> deadline 06:00). Åtgärdat: debug-rad tillagd i `set_allow_day_charging()`
> (`[DayCharging] set_allow_day_charging(<value>) ...`) så switch-växlingar nu syns i `ocpp_charger_debug.log`.

## Symptom

Användaren slår på switchen "Allow Day Charging" och förväntar sig att planeringen
ska kunna välja billiga slots under morgondagens dagtid (t.ex. 10:00–14:00).
Planen väljer ändå nattslottar och ignorerar dagtidsfönstret helt.

Loggen bekräftar att deadline alltid är `06:00` oavsett switch-läge:

```
[ChargePlanner] Planning: soc=36%→40% energy=3.35 kWh power=4.1 kW deadline=2026-06-18 06:00
[ChargePlanner] Charge 5.5 kWh in 1 window(s): 05:00–05:30 avg 67.8 öre/kWh
[ChargePlanner] Charge 5.5 kWh in 2 window(s): 23:15–23:30, 05:00–05:15 avg 63.4 öre/kWh
```

Billiga slots imorgon (t.ex. 10:00–14:00 till under 50 öre/kWh) syns aldrig —
de filtreras bort i `plan_cheapest_window()` eftersom de ligger efter 06:00.

## Rotorsak

`_compute_deadline()` i `__init__.py` anropar `compute_deadline()` i `deadline.py`
utan att skicka med `allow_day_charging`. `compute_deadline()` returnerar alltid
`DEFAULT_CHARGE_DEADLINE_HOUR` (06:00) på vardagar oavsett vad switchen visar.

Helg-logiken i `compute_deadline()` (returnera slutet av tillgänglig prisdata)
är exakt det beteende som behövs när `allow_day_charging=True` — men den triggas
aldrig på vardagar.

**Prioritetsordning för deadline (korrekt efter fix):**

| Villkor | Deadline |
|---|---|
| `manual_deadline_str` satt | HH:MM (rullar till imorgon om passerad) |
| `allow_day_charging=True` | Slutet av tillgänglig prisdata (= helg-logik) |
| Vardag, ingen override | `06:00` nästa dag |
| Helg | Slutet av tillgänglig prisdata |

## Berörda filer

- `custom_components/ocpp_charger/deadline.py` — `compute_deadline()`
- `custom_components/ocpp_charger/__init__.py` — `_compute_deadline()`

## Fix

### `deadline.py` — ny parameter `allow_day_charging`

**Före:**
```python
def compute_deadline(
    now_local: datetime,
    local_tz,
    all_prices: list,
    manual_deadline_str: str = "",
    deadline_hour: int = 6,
) -> datetime:
    """Return the charging deadline.

    Priority:
    1. Manual HH:MM (manual_deadline_str) – rolls to tomorrow if already past.
    2. Weekday → deadline_hour:00 (rolls to tomorrow if past).
    3. Weekend → end of last available price interval (+15 min), else now + 48h.
    """
    parsed = parse_hhmm(manual_deadline_str)
    if parsed is not None:
        hour, minute = parsed
        candidate = datetime.combine(
            now_local.date(), dtime(hour, minute), tzinfo=local_tz,
        )
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    is_weekend = now_local.weekday() >= 5  # Sat=5, Sun=6
    if not is_weekend:
        today_deadline = datetime.combine(
            now_local.date(), dtime(deadline_hour, 0), tzinfo=local_tz,
        )
        if today_deadline > now_local:
            return today_deadline
        return datetime.combine(
            now_local.date() + timedelta(days=1), dtime(deadline_hour, 0), tzinfo=local_tz,
        )

    if all_prices:
        last_time = max(_to_utc(iv["time"]) for iv in all_prices)
        return (last_time + timedelta(minutes=15)).astimezone(local_tz)
    return now_local + timedelta(hours=48)
```

**Efter:**
```python
def compute_deadline(
    now_local: datetime,
    local_tz,
    all_prices: list,
    manual_deadline_str: str = "",
    deadline_hour: int = 6,
    allow_day_charging: bool = False,
) -> datetime:
    """Return the charging deadline.

    Priority:
    1. Manual HH:MM (manual_deadline_str) – rolls to tomorrow if already past.
    2. allow_day_charging=True → end of last available price interval (+15 min),
       else now + 48h. Same logic as weekend – planner can use full price horizon.
    3. Weekday → deadline_hour:00 (rolls to tomorrow if past).
    4. Weekend → end of last available price interval (+15 min), else now + 48h.
    """
    parsed = parse_hhmm(manual_deadline_str)
    if parsed is not None:
        hour, minute = parsed
        candidate = datetime.combine(
            now_local.date(), dtime(hour, minute), tzinfo=local_tz,
        )
        if candidate <= now_local:
            candidate += timedelta(days=1)
        return candidate

    is_weekend = now_local.weekday() >= 5  # Sat=5, Sun=6
    if allow_day_charging or is_weekend:
        if all_prices:
            last_time = max(_to_utc(iv["time"]) for iv in all_prices)
            return (last_time + timedelta(minutes=15)).astimezone(local_tz)
        return now_local + timedelta(hours=48)

    today_deadline = datetime.combine(
        now_local.date(), dtime(deadline_hour, 0), tzinfo=local_tz,
    )
    if today_deadline > now_local:
        return today_deadline
    return datetime.combine(
        now_local.date() + timedelta(days=1), dtime(deadline_hour, 0), tzinfo=local_tz,
    )
```

### `__init__.py` — skicka med `allow_day_charging` till `compute_deadline`

**Före** (rad ~1377–1383):
```python
        return compute_deadline(
            now_local,
            local_tz,
            all_prices,
            manual_deadline_str=self.manual_deadline_str,
            deadline_hour=DEFAULT_CHARGE_DEADLINE_HOUR,
        )
```

**Efter:**
```python
        return compute_deadline(
            now_local,
            local_tz,
            all_prices,
            manual_deadline_str=self.manual_deadline_str,
            deadline_hour=DEFAULT_CHARGE_DEADLINE_HOUR,
            allow_day_charging=self.allow_day_charging,
        )
```

## Verifiering

```bash
# Bekräfta att deadline utökas när allow_day_charging=True
grep "ChargePlanner.*Planning.*deadline" /config/ocpp_charger_debug.log | tail -5

# Bekräfta att dagslottar nu dyker upp i planen
grep "ChargePlanner.*Charge.*window" /config/ocpp_charger_debug.log | tail -10
```

Förväntat utfall med `allow_day_charging=True` på en vardag:
- `deadline` ska vara slutet av prisdata (t.ex. `2026-06-18 23:45` eller liknande)
- Planen ska kunna inkludera slots under 06:00–22:00 nästa dag om de är billigare

## Interaktion med övriga buggar/features

- **Feature 4 (manual_deadline_str)** — opåverkad; manuell deadline har högst prioritet
  och returnerar tidigt innan `allow_day_charging`-villkoret nås
- **Feature 1 (deadline_override / helg-switch)** — opåverkad; helg-logiken
  är nu sammanfogad med `allow_day_charging`-logiken i ett gemensamt villkor
- **Bug 26 (allow_day_charging persistens)** — förutsätts vara fixad; utan Bug 26-fix
  återställs `allow_day_charging=False` vid omstart och denna fix har ingen effekt
- **`_sync_allow_day_charging()`** — opåverkad; styr om dagtidsslots filtreras
  bort i `filtered_prices`, inte deadline
