# Bug 31 – Bug 28:s frysta planfönster överlevde inte omstart

**Datum:** 2026-06-20
**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-20

---

## Symptom

En pågående laddning avbröts kl 17:51 strax efter en omstart (deploy av Bug 30), med
`Outside plan window`, trots att Garo fortsatte ladda genom omstarten:

```
17:48:46  charging=True, power=4246W, soc 72→73%   (laddar fint, billigt @ 5 öre)
17:49:02  charging=False                            ← ha core stop (Bug 30-deploy)
17:49:36  OCPPCoordinator started                   ← omstart
17:49:51  Auto-start check: in_window=False, plan=22:30–00:00
17:49:51  Inferred charging=True from MeterValues (power=4243W)  ← Garo laddade vidare
17:51:45  [SmartCharge] Outside plan window (5 intervals), stopping → RemoteStop
17:51:56  status=Preparing, power=0W
```

---

## Rotorsak

Bug 28 fryser `plan.active_intervals` i `_session_plan_intervals` vid sessionstart, så att
en omräkning mid-charge inte kan flytta fönstret bort under en aktiv session. Window-stopp-
grenen i `_update_smart_charging()` bedömer en aktiv session mot den frysta listan i stället
för mot `plan.is_in_window()`.

Men `_session_plan_intervals` hölls **bara i minnet** och nollställdes vid omstart. Bilen
laddade utanför det omräknade planfönstret (live-planen hade flyttat till `22:30–00:00`);
efter omstarten var den frysta listan `None`, så window-check föll tillbaka på den omräknade
planen → `now` (17:51) utanför fönster → "Outside plan window" → RemoteStop. Exakt den abort
Bug 28 byggdes för att förhindra, åter-exponerad av en omstart mitt i laddning.

Samma klass som Bug 30: in-memory sessionstillstånd som förloras vid omstart. Bug 30 fixade
SOC-baslinjen; detta är den andra biten. (Utlösare här: Bug 30-deployens omstart.)

---

## Önskat beteende

Det frysta planfönstret ska överleva en omstart, så att en omstart mitt i en laddning inte
åter-exponerar Bug 28:s "Outside plan window"-abort.

---

## Fix

Persistera `session_plan_intervals` i Store. Datetimes serialiseras som ISO-strängar
(JSON kan inte lagra datetime-objekt) och parsas tillbaka vid load.

### Berörd fil
`custom_components/ocpp_charger/__init__.py`

### 1. `_save_state()` – serialisera frysta fönster

**Efter** (ny nyckel i `data`-dicten, efter Bug 30-nycklarna):
```python
            "session_plan_intervals": (   # Bug 31: persist Bug 28 frozen plan (was in-memory only)
                [[s.isoformat(), e.isoformat()] for s, e in self._session_plan_intervals]
                if self._session_plan_intervals is not None else None
            ),
```

### 2. `_load_state()` – parsa tillbaka

**Efter** (nytt block, t.ex. efter Bug 30:s session-baslinje-återställning):
```python
            _spi = data.get("session_plan_intervals")
            if _spi:
                try:
                    self._session_plan_intervals = [
                        (datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in _spi
                    ]
                    _LOGGER.info(
                        "[Store] Återställde fryst planfönster (%d intervall)",
                        len(self._session_plan_intervals),
                    )
                except (ValueError, TypeError):
                    self._session_plan_intervals = None
```

`set_active_vehicle()` rör **inte** `_session_plan_intervals`, så ingen ordningskänslighet
som i Bug 30.

**Migrering:** saknad nyckel / `None` → `_session_plan_intervals` förblir `None` →
oförändrat beteende (window-check faller tillbaka på `plan.is_in_window()`).

---

## Verifiering efter implementation

```bash
jq '.data.session_plan_intervals' /config/.storage/ocpp_charger_*
grep 'Återställde fryst planfönster' /config/ocpp_charger_debug.log
```

Utfall: nyckeln round-trippar i `.storage` (`null` när ingen aktiv session, en lista med
ISO-intervall under en aktiv session). Deployades medan bilen var idle → ingen aktiv
laddning avbröts; ren laddning utan fel. Fullt beteendebevis (frysta fönstret återställt
över en omstart mitt i laddning, ingen `Outside plan window`-stopp) loggas av
`Återställde fryst planfönster (N intervall)` nästa gång en laddning spänner över en omstart.

---

## Interaktion med tidigare buggar

| Bug | Påverkan |
|-----|----------|
| Bug 28 (frys planfönster för aktiv session) | Gör Bug 28-skyddet beständigt över omstart (var tidigare in-memory) |
| Bug 16 (omplanering under laddning) | Oförändrad – planen räknas fortfarande om mid-charge; frysta listan styr aktiv session |
| Bug 30 (persistens av SOC-baslinje) | Systerfix – samma klass; tillsammans överlever allt sessionstillstånd en omstart |

---

## Notering

Med Bug 30 + Bug 31 överlever nu allt sessionstillstånd som styr aktiv laddning en omstart
(SOC-baslinje, energi, frysta planfönster). Deploy-omstarter mitt i en laddning är därmed säkra.

---

## Berörda filer

- `__init__.py` – `_save_state()` (serialisering) + `_load_state()` (deserialisering)

Inga andra filer berörs.
