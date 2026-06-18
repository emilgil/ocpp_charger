# Bug 26 – `allow_day_charging` och `deadline_override` persisteras inte vid omstart

**Status:** ✅ Åtgärdad (delvis enligt rapport) + deployad till live HA 2026-06-17

> **Implementeringsnotis:** Endast `allow_day_charging` + `_day_charging_manual_override`
> persisteras. `deadline_override`-delen av denna rapport implementerades **inte** – den
> entiteten/variabeln togs bort av Feature 4 (Deadline Override-switch ersatt av text-entitet).
> Att lägga `self.deadline_override` i `_save_state()` enligt rapportens "Efter" hade kraschat
> `_save_state()` med `AttributeError` (attributet finns inte). Den döda konstanten
> `SWITCH_DEADLINE_OVERRIDE` i `const.py` kvarstår som separat städning.
>
> **Korrigering av rotorsaksanalysen:** Rapportens steg 5 ("HA:s entitetsregister har cachat
> switch-tillståndet True → switchen visar True i UI") stämmer inte. `AllowDayChargingSwitch`
> är en `CoordinatorEntity` vars `is_on` läser `coordinator.allow_day_charging` live (switch.py:113)
> – den är ingen `RestoreEntity`. Efter omstart visar switchen alltså `False`, inte `True`.
> Den underliggande defekten (manuellt val överlever inte omstart) är dock korrekt och åtgärdad.

## Symptom

Användaren slår på switchen "Tillåt dagladdning" (eller "Deadline Override") — UI visar switchen som på — men laddfönstret förblir oförändrat. Felsökningsloggen visar konsekvent:

```
[ChargePlanner] Day-charging offer skipped: day plan not cheaper (day=85.0 öre/kWh feasible=True, night=85.0 öre/kWh)
```

Denna logggren tillhör `elif not self.allow_day_charging`-grenen i `_update_charge_plan()`, vilket bevisar att `allow_day_charging=False` i koordinatorn trots att switchen visar `True` i UI. Loggraden förekom 519 gånger utan avbrott, vilket bekräftar att `allow_day_charging` aldrig blivit `True` i koordinatorn under den sessionen.

## Rotorsak

Tre in-memory-variabler saknas helt i `_save_state()`/`_load_state()` i `__init__.py`:

| Variabel | Initialiseras till vid omstart | Konsekvens |
|---|---|---|
| `allow_day_charging` | `_compute_allow_day_charging()` → `False` på vardagar | Manuellt val förloras |
| `_day_charging_manual_override` | `False` | `_sync_allow_day_charging()` skriver omedelbart över manuellt `allow_day_charging=True` |
| `deadline_override` | `False` | Deadline Override-switch-läge förloras |

**Exakt felsekvens för `allow_day_charging`:**

1. Användaren slår på switchen → `set_allow_day_charging(True)` sätter `allow_day_charging=True` och `_day_charging_manual_override=True`
2. Koordinatorn planerar korrekt med dagintervall
3. HA startas om eller integrationen laddas om
4. `__init__` initialiserar `_day_charging_manual_override=False` och `allow_day_charging=False` (vardag)
5. HA:s entitetsregister har cachat det gamla switch-tillståndet `True` — switchen *visar* `True` i UI
6. Varje koordinatorcykel anropar `_sync_allow_day_charging()` som ser `manual_override=False` och skriver `allow_day_charging=False`
7. `_update_charge_plan()` kör alltid `elif not self.allow_day_charging`-grenen → nattprisplan

**Exakt felsekvens för `deadline_override`:**

`DeadlineOverrideSwitch.async_turn_on()` skriver direkt på `coordinator.deadline_override = True` utan att anropa någon persisterings-metod. Vid omstart initialiseras `deadline_override=False` (rad 354), switchen visar fel läge.

## Berörda filer

- `custom_components/ocpp_charger/__init__.py` — `_save_state()`, `_load_state()`

## Fix

### `_save_state()` — lägg till tre nycklar i `data`-dict

**Före** (rad 504–507):
```python
            "charge_mode": self.charge_mode,
            "target_soc": self.target_soc,
            "target_kwh": self.target_kwh,
            "active_vehicle_name": self.active_vehicle.get(VEHICLE_NAME) if self.active_vehicle else None,
```

**Efter:**
```python
            "charge_mode": self.charge_mode,
            "target_soc": self.target_soc,
            "target_kwh": self.target_kwh,
            "active_vehicle_name": self.active_vehicle.get(VEHICLE_NAME) if self.active_vehicle else None,
            "allow_day_charging": self.allow_day_charging,
            "day_charging_manual_override": self._day_charging_manual_override,
            "deadline_override": self.deadline_override,
```

### `_load_state()` — återställ de tre variablerna

**Före** (direkt efter `if data.get("target_kwh") is not None:` blocket, rad ~541):
```python
            saved_vehicle = data.get("active_vehicle_name")
```

**Efter:**
```python
            if data.get("day_charging_manual_override"):
                self._day_charging_manual_override = True
                self.allow_day_charging = bool(data.get("allow_day_charging", False))
                _LOGGER.info(
                    "[Store] Återställde allow_day_charging=%s (manuell override)",
                    self.allow_day_charging,
                )
            if data.get("deadline_override") is not None:
                self.deadline_override = bool(data["deadline_override"])
                _LOGGER.info("[Store] Återställde deadline_override=%s", self.deadline_override)

            saved_vehicle = data.get("active_vehicle_name")
```

## Verifiering

```bash
# Efter omstart — kontrollera att värdet laddas korrekt
grep "Återställde allow_day_charging\|Återställde deadline_override" /config/ocpp_charger_debug.log

# Bekräfta att Bug 18-grenen INTE körs när allow=True
grep "Day-charging offer skipped" /config/ocpp_charger_debug.log
```

## Interaktion med övriga buggar/features

- **`_sync_allow_day_charging()`** — fungerar korrekt efter fix eftersom `_day_charging_manual_override=True` återställs innan första koordinatorcykeln
- **Feature 1 (Deadline Override)** — `deadline_override` persisteras nu korrekt; switch visar rätt läge direkt efter omstart
- **Bug 18 (närvaro-erbjudande)** — opåverkad; körs endast när `allow_day_charging=False` och `manual_override=False`
- **`_force_day_plan`** — persisteras medvetet *inte*; det är en session-lokal flagga som sätts via notis-action och ska resettas vid omstart

## Notering om `_day_charging_manual_override` vid reset

`_day_charging_manual_override` ska *inte* nollställas vid kabeldisconnect (kabelsession-reset) — manuellt val av dagladdning bör överleva mellan laddtillfällen tills användaren aktivt ändrar det. Om beteendet önskas annorlunda är det en separat feature.
