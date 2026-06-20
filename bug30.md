# Bug 30 – SOC-estimatets baslinje desyncade vid omstart mitt i en session

**Datum:** 2026-06-20
**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-20

---

## Symptom

Laddning stoppade strax efter 04:00 med bilen på **~83 %** trots laddmål 100 %, och
endast **1,11 kWh** levererades mellan 04 och 06 (Kia eNiro, 64 kWh). Laddningen
återupptogs inte – HA *trodde* att bilen var full.

Loggen (04:47:30):

```
[ChargePlanner] Estimerad SOC: start=82.0% +12.27 kWh → 99.7%
[ChargePlanner] Planning: soc=84%→100% energy=11.38 kWh ...
[SmartCharge] Mål nått (SOC 100% >= mål 100%), stoppar  → RemoteStop
[SmartCharge] Auto-start undertryckt – mål redan nått (SOC 100% >= mål 100%)
```

Mätaren bekräftar att endast 1,11 kWh flödade: register 3674960 Wh (04:00) →
3676073 Wh (07:05).

---

## Rotorsak

SOC-estimatet (`soc_estimate.estimate_soc`, Bug 29) är `start_soc + levererad energi`.
Det är korrekt **endast** om energin räknas från ögonblicket `start_soc` mättes.

`_session_start_soc` hölls bara i minnet och persisterades **inte**, medan `energy_kwh`
persisterades. Vid en omstart **mitt i en kabelsession** (kvällens omstart för att deploya
Bug 29):

1. `_session_start_soc` nollställdes (init `None`) och återfångades i
   `_update_soc_from_ha()` (`state.charging and _session_start_soc is None`) till det
   *aktuella* mitt-i-sessionen-värdet, **82 %**,
2. medan `energy_kwh` (~12,3 kWh) överlevde omstarten via Store.

Den energi (~12,3 kWh) som **redan hade höjt bilen till 82 %** (från ~65 %) räknades då
en gång till ovanpå 82 %-baslinjen:

```
estimat = 82 % + 12,3 kWh·0,92/64·100 ≈ 99,7 %     (verklighet ~83 %)
```

Efter bara ~0,3 kWh till passerade estimatet 100 % → `_charging_goal_reached()` →
RemoteStop. Samma villkor undertrycker auto-start (Bug 23-symmetri), så laddningen
återupptogs aldrig. Self-clearade först vid urkoppling (`Available`-reset).

**Utlösare:** själva omstarten för att deploya Bug 29. Inte ett fel i Bug 29-logiken –
en separat persistensbugg som vilken omstart som helst mitt i en laddning hade triggat.

---

## Önskat beteende

SOC-baslinjen och dess energi ska förbli i synk över en omstart, så att en omstart mitt
i en laddning inte får estimatet att dubbelräkna levererad energi och stoppa för tidigt.

---

## Fix

Persistera estimatets baslinje tillsammans med dess energi i Store.

### Berörd fil
`custom_components/ocpp_charger/__init__.py`

### 1. `_save_state()` – persistera baslinjen + dess energi

**Före:**
```python
            "cable_session_energy_kwh": self._cable_session_energy_kwh,
            "cable_session_cost_sek": self._cable_session_cost_sek,
            "charge_mode": self.charge_mode,
```

**Efter:**
```python
            "cable_session_energy_kwh": self._cable_session_energy_kwh,
            "cable_session_cost_sek": self._cable_session_cost_sek,
            "session_start_soc": self._session_start_soc,   # Bug 30: SOC estimation baseline
            "session_total_kwh": self._session_total_kwh,   # Bug 30: energy paired with that baseline
            "charge_mode": self.charge_mode,
```

### 2. `_load_state()` – återställ **efter** `set_active_vehicle()`

`set_active_vehicle()` (som anropas för att återställa aktivt fordon) nollställer
`_session_start_soc`/`_session_total_kwh`. Återställningen måste därför ske **efter**
fordons-återställningen, annars klobbras den.

**Efter** (nytt block direkt efter `saved_vehicle`-blocket, före `Laddade state`-loggen):
```python
            if data.get("session_start_soc") is not None:
                self._session_start_soc = data.get("session_start_soc")
                self._session_total_kwh = data.get("session_total_kwh", 0.0)
                _LOGGER.info(
                    "[Store] Återställde session-baslinje: start_soc=%.1f%% total_kwh=%.2f",
                    self._session_start_soc, self._session_total_kwh,
                )
```

Ett återställt icke-`None`-värde gör dessutom att återfångnings-guarden i
`_update_soc_from_ha()` (`state.charging and _session_start_soc is None`) inte skriver
över det.

**Migrering:** gammal sparad state saknar nycklarna → `data.get(...)` ger `None`/`0.0`
→ oförändrat (återfångning) beteende.

---

## Viktig ordning (fångad vid live-verifiering)

Första implementationen placerade återställningen **före** `set_active_vehicle()`-anropet
i `_load_state()`. Live-loggen visade då `Estimerad SOC: start=0.0%` – `set_active_vehicle`
nollställde den återställda baslinjen, och nästa SOC-fångst satte den till 0,0 (Skoda-
entiteten otillgänglig just då). Flytt till **efter** anropet löste det.

---

## Verifiering efter implementation

```bash
grep 'Återställde session-baslinje' /config/ocpp_charger_debug.log
jq '.data.session_start_soc, .data.session_total_kwh' /config/.storage/ocpp_charger_*
grep 'Estimerad SOC' /config/ocpp_charger_debug.log | tail
```

Utfall: `[Store] Återställde session-baslinje: start_soc=34.0% total_kwh=0.00` loggas
**efter** `Återställde aktivt fordon`, baslinjen behålls i `.storage`, ingen fel/traceback.
Vid själva deploy-omstarten (aktiv session) förseedades baslinjen i `.storage`
(start_soc, total_kwh=0.0 via `jq`) för att inte avbryta den pågående laddningen.

---

## Interaktion med tidigare buggar

| Bug | Påverkan |
|-----|----------|
| Bug 8 (estimera SOC från start + levererad energi) | Baslinjen som Bug 8 förlitar sig på överlever nu omstart |
| Bug 29 (estimerad SOC ≥ target som mål-kriterium) | Beror på korrekt baslinje; Bug 30 säkrar den över omstart |
| Bug 23 (auto-start/stopp delar `_charging_goal_reached()`) | Oförändrad – men felaktig baslinje fick *båda* grenarna att tro "mål nått" |
| Bug 26 (persistens av allow_day_charging) | Samma klass (in-memory state över omstart), orelaterat fält |
| Bug 31 (frysta planfönster persisteras) | Systerfix – samma klass; tillsammans överlever allt sessionstillstånd en omstart |

---

## Berörda filer

- `__init__.py` – `_save_state()` (+2 nycklar) och `_load_state()` (återställ efter fordons-återställning)

Inga andra filer berörs.
