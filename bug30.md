# Bug 30 – SOC-estimatets baslinje desyncar vid omstart mitt i en session

**Datum:** 2026-06-20
**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-20

## Symptom

Laddning stoppade "strax efter 04" med bilen på **83 %** trots mål 100 %, och endast
**1,11 kWh** levererades 04–06. Loggen (04:47:30):

```
[SmartCharge] Mål nått (SOC 100% >= mål 100%), stoppar → RemoteStop
[SmartCharge] Auto-start undertryckt – mål redan nått (SOC 100% >= mål 100%)
```

HA *trodde* att bilen var på 100 % och stoppade + vägrade starta om. Mätaren visar
3674960 Wh (04:00) → 3676073 Wh (07:05) = exakt 1,11 kWh.

## Rotorsak – baslinje och energi desyncade

SOC-estimatet (`soc_estimate.estimate_soc`, Bug 29) = `start_soc + levererad energi`.
Det är bara korrekt om energin räknas **från ögonblicket `start_soc` mättes**.

`_session_start_soc` hölls bara i minnet (persisterades **inte**), medan `energy_kwh`
persisterades. Vid en omstart **mitt i en kabelsession**:
1. `_session_start_soc` nollställdes → återfångades i `_update_soc_from_ha()`
   (`state.charging and _session_start_soc is None`) till det *aktuella* (mitt-i-sessionen)
   SOC-värdet, t.ex. **82 %**,
2. medan `energy_kwh` (~12,3 kWh) överlevde omstarten.

Den energi (~12,3 kWh) som *redan hade höjt bilen till 82 %* (från ~65 %) räknades då
**en gång till** ovanpå 82 %-baslinjen → estimat ≈ 100 %, verklighet ~83 %. Efter bara
~0,3 kWh till passerade estimatet 100 % → RemoteStop, och samma villkor undertryckte
auto-start (Bug 23-symmetri).

**Utlösare:** omstarten kvällen innan för att deploya Bug 29-fixen. Det var inte ett fel
i Bug 29-logiken (den är korrekt) utan en separat persistensbugg som vilken omstart som
helst mitt i en laddning hade triggat. Self-clearade vid urkoppling (`Available`-reset).

## Fix

Persistera estimatets baslinje tillsammans med dess energi i Store, så de inte kan
desynca över en omstart:

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_save_state()` persisterar `session_start_soc` + `session_total_kwh`; `_load_state()` återställer dem **efter** `set_active_vehicle()`. Återställt icke-None värde gör att återfångnings-guarden i `_update_soc_from_ha()` inte skriver över det. |

**Viktig ordning (fångad vid live-verifiering):** `_load_state()` anropar `set_active_vehicle()`
för att återställa aktivt fordon, och den nollställer `_session_start_soc`/`_session_total_kwh`.
Återställningen måste därför ske **efter** det anropet, annars klobbras den (första försöket
placerade den före → estimatet blev `start=0.0%`).

Migrering: gammal sparad state saknar nycklarna → `data.get(...)` ger `None`/`0.0` →
oförändrat beteende (återfångning). Endast framtida omstarter skyddas — vid själva
deploy-omstarten (med en aktiv session) förseedades baslinjen manuellt i `.storage`
(start_soc=34.0, total_kwh=0.0) för att inte avbryta den pågående Skoda-laddningen.

## Verifiering

```bash
# Estimatet ska behålla rätt baslinje över omstart, inte hoppa till ~100%
grep 'Estimerad SOC' /config/ocpp_charger_debug.log | tail
# Nycklarna ska finnas i Store
jq '.data.session_start_soc, .data.session_total_kwh' /config/.storage/ocpp_charger_*
```

Förväntat: efter omstart mitt i en session är `Estimerad SOC: start=...` oförändrad
(ingen falsk 100 %), ingen `Mål nått (SOC 100% ...)`-stopp.
