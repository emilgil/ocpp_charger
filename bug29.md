# Bug 29 – Laddning stoppar vid SOC-mittpunkten (cirkulärt plan-energi-villkor)

**Datum:** 2026-06-19
**Status:** ✅ Åtgärdad (TDD) + deployad till live HA 2026-06-19

## Symptom

Laddning avbröts "strax efter 13:00" och återupptogs inte – bilen stod kvar på
~84 % hela eftermiddagen. (Kia eNiro, start-SOC 66 %, mål 100 %.)

Loggen visar att **Bug 28-fixen fungerade** (frysta planen höll sessionen genom
13:23-omräkningen) men att stoppet istället kom från mål-nått-grenen:

```
[ChargePlanner] Estimerad SOC: start=66.0% +12.27 kWh → 83.6%
[ChargePlanner] Planning: soc=84%→100% energy=11.38 kWh ...
[SmartCharge] Mål nått (Energi 12.27 kWh >= planens 11.38 kWh), stoppar
```

## Rotorsak

`_charging_goal_reached()` jämförde **levererad** energi mot **`plan.energy_kwh`**.
Sedan Bug 16 räknas planen om mid-charge, och sedan Bug 8 uppskattar planen aktuell
SOC från *levererad* energi. Därmed är `plan.energy_kwh` numera **återstående** energi:

```
plan.energy_kwh ≈ TOTAL_behov − levererat
villkor: levererat ≥ plan.energy_kwh  ⇒  levererat ≥ TOTAL_behov − levererat
                                      ⇒  levererat ≥ TOTAL_behov / 2
```

Stoppet triggar alltså vid **halva** energin = **SOC-mittpunkten**. Siffrorna stämmer:
TOTAL = (100−66)/100·64/0,92 ≈ 23,6 kWh → halva ≈ 11,8 kWh; stopp vid 12,27 kWh,
estimerad SOC 83,6 % = mittpunkten 66↔100.

Eftersom samma `_charging_goal_reached()` även undertrycker auto-start (Bug 23-symmetri)
återupptogs aldrig laddningen.

CLAUDE.md noterade redan att `_update_charge_plan()` *medvetet* utelämnar plan-energi-
villkoret "(cirkulärt)" – men det låg kvar i `_charging_goal_reached()`.

## Fix

Ny ren, stdlib-only-modul `soc_estimate.py` med `estimate_soc(start_soc,
already_charged_kwh, capacity_kwh, efficiency, reported_soc)` (testbar fristående;
`tests/test_soc_estimate.py`, 7 tester). Används i **både** `_charging_goal_reached()`
och planerarens Bug 8-block så de inte kan driva isär.

`_charging_goal_reached()` använder nu estimerad SOC ≥ target_soc (rätt, icke-cirkulärt
fullbordandekriterium) och target_kwh-villkoret. Det cirkulära `levererat ≥
plan.energy_kwh`-villkoret är **borttaget**.

| Fil | Ändring |
|-----|---------|
| `soc_estimate.py` | Ny modul: `estimate_soc()` |
| `__init__.py` | `_charging_goal_reached()` använder estimerad SOC, tar bort plan-energi-villkoret; planeraren använder samma helper |
| `tests/test_soc_estimate.py` | 7 tester (full energi→mål, halva→mittpunkt INTE mål, planerar-formel, fallback-fall) |

## Verifiering

```bash
# Den falska stopp-orsaken ska aldrig dyka upp igen
grep "Mål nått (Energi.*planens" /config/ocpp_charger_debug.log   # förväntat: inga nya
```

Förväntat: ingen `>= planens`-stopp; en session laddar förbi mittpunkten och stoppar
först när estimerad SOC når target_soc.
