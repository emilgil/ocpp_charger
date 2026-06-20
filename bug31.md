# Bug 31 – Bug 28:s frysta planfönster överlevde inte omstart

**Datum:** 2026-06-20
**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-20

## Symptom

En pågående laddning avbröts kl 17:51 strax efter en omstart (deploy av Bug 30), trots
att Garo fortsatte ladda genom omstarten:

```
17:49:02  charging=False                 ← ha core stop (Bug 30-deploy)
17:49:36  OCPPCoordinator started
17:49:51  Inferred charging=True from MeterValues (power=4243W)  ← Garo laddade vidare
17:51:45  [SmartCharge] Outside plan window (5 intervals), stopping → RemoteStop
```

## Rotorsak

`_session_plan_intervals` (Bug 28:s frysta planfönster) hölls **bara i minnet** och
nollställdes vid omstart. Bilen laddade utanför det omräknade planfönstret (live-planen
hade flyttat till `22:30–00:00`), och utan den frysta listan föll window-check tillbaka på
den omräknade planen → `now` utanför fönster → "Outside plan window" → RemoteStop. Exakt den
abort Bug 28 var byggd för att förhindra, åter-exponerad av en omstart mitt i laddning.

Samma klass som Bug 30 (in-memory sessionstillstånd förloras vid omstart). Bug 30 fixade
SOC-baslinjen; detta är den andra biten.

## Fix

Persistera `session_plan_intervals` i Store (datetimes serialiseras som ISO-strängar) och
återställ i `_load_state()`. `set_active_vehicle()` rör inte fältet, så ingen ordningskänslighet
som i Bug 30.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_save_state()` serialiserar `_session_plan_intervals` → `[[start_iso, end_iso], ...]`; `_load_state()` parsar tillbaka till `(datetime, datetime)`-tupler |

Migrering: saknad nyckel / `None` → `_session_plan_intervals` förblir `None` (oförändrat
beteende: window-check faller tillbaka på `plan.is_in_window()`).

## Verifiering

```bash
jq '.data.session_plan_intervals' /config/.storage/ocpp_charger_*
grep 'Återställde fryst planfönster' /config/ocpp_charger_debug.log
```

Förväntat: nyckeln finns i Store (`null` när ingen aktiv session, en lista med intervall
under en aktiv session). Vid omstart mitt i laddning loggas `Återställde fryst planfönster
(N intervall)` och ingen `Outside plan window`-stopp följer.

## Notering

Med Bug 30 + Bug 31 överlever nu allt sessionstillstånd som styr aktiv laddning en omstart
(SOC-baslinje, energi, frysta planfönster). Deploy-omstarter mitt i en laddning är därmed säkra.
