# OCPP EV Charger – Home Assistant Custom Component

## Projektöversikt
Home Assistant custom component som fungerar som OCPP 1.6 Central System (WebSocket-server).
Garo laddbox ansluter till HA, inte tvärtom.

**Charger ID:** `GaroCS-48671AA056E80`
**Charger IP:** `192.168.1.111:39324`
**OCPP WebSocket port:** `9000` (HA lyssnar)
**HA-server:** `192.168.1.97`
**Fordon:** Kia eNiro, 64 kWh, SOC-entitet: `sensor.e_niro_ev_battery_level`
**Elprisentitet:** `sensor.gespot_current_price_se3` (quarterly intervals, attribut: `today_interval_prices`, `tomorrow_interval_prices`)
**Notifikationer:** `notify.mobile_app_sm_s918b`

## Deploy-kommandon
```bash
# Kopiera alla Python-filer till HA
scp -r custom_components/ocpp_charger/*.py root@192.168.1.97:/config/custom_components/ocpp_charger/

# Starta om HA
ssh root@192.168.1.97 "ha core restart"

# Följ loggen
ssh root@192.168.1.97 "grep -i ocpp_charger /config/home-assistant.log | tail -30"

# Debug-logg (mer verbose, roterande fil)
ssh root@192.168.1.97 "tail -f /config/ocpp_charger_debug.log"
```

## Filstruktur
```
custom_components/ocpp_charger/
  __init__.py          – OCPPCoordinator, smart charging, kostnad, notiser, auto-start
  ocpp_client.py       – WebSocket OCPP 1.6-server, ChargerState
  config_flow.py       – Setup flow (4 steg) + options flow
  const.py             – Alla konstanter
  sensor.py            – 23 sensorer
  binary_sensor.py     – 3 binära sensorer
  number.py            – 6 number-entiteter
  select.py            – 3 select-entiteter
  switch.py            – 3 switchar
  button.py            – 2 knappar
  vehicle_detection.py – Auto-identifiering av fordon
  current_schedule.py  – Dag/natt-schema
  smart_charge.py      – Prisbeslut (fallback när ingen plan finns)
  charge_planner.py    – Optimal laddplanering baserat på spotpriser
  charge_windows.py    – Feature 3: bygger laddplanens slots (stdlib-only, testbar)
  deadline.py          – Feature 4/6: parse_hhmm + compute_deadline + helper_state_to_hhmm (stdlib-only, testbar)
  soc_estimate.py      – Bug 29: estimate_soc från start-SOC + levererad energi (stdlib-only, testbar)
  price_cap.py         – Feature 5: select_price_cap_slots – slots ≤ pristak (stdlib-only, testbar)
  notifier.py          – Push-notiser
  rest_client.py       – Async HTTP-klient
  manifest.json
  services.yaml
```

## Arkitektur – laddningsstyrning (prioritetsordning)
1. **Charge mode = Immediate** → ladda alltid
2. **Charge mode = Smart + `price_cap_ore_kwh > 0`** → pristaksläge (Feature 5): planen byggs
   av alla slots ≤ taket via `_update_price_cap_plan()` istället för cheapest-window-planeraren.
   Resten (auto-start/stopp inom `plan.active_intervals`, SoC-stopp) är identiskt.
3. **Charge mode = Smart + feasible plan** → ladda ENDAST inom `plan.start–plan.end`
   - Auto-start: `_update_smart_charging()` skickar RemoteStart när klockan passerar `plan.start`
   - Auto-stop: RemoteStop vid `plan.end`
4. **Charge mode = Smart + ingen plan** → priströskel-fallback (40:e percentilen)
5. **Charge mode = Scheduled** → ladda inom konfigurerad tidsperiod

## Kontrollförändringar → omedelbar omplanering
Dessa setters anropar `_update_charge_plan()` direkt:
- `set_target_soc()`, `set_target_kwh()`
- `set_allow_day_charging()`
- `set_charge_mode()`
- `set_price_cap()` (Feature 5 – async; sparar även Store)
- `set_active_vehicle()` (även `_update_soc_from_ha()` + reset `_session_total_kwh` vid fordonsbyte)

## Viktiga skyddsmekanismer

### Grace period (90s)
Ingen stop-logik körs inom 90 sekunder efter `StartTransaction`. Förhindrar att en nyss startad session stoppas omedelbart av stop-logiken.

### Plan-omräkning under laddning (Bug 16 + Bug 22)
`_update_charge_plan()` körs även under aktiv laddning (sedan Bug 16, för att morgondagens priser ska plockas upp mitt i en session). Slot-filtret i `plan_cheapest_window()` behåller den slot som innehåller `now` – det filtrerar på slot-**slut**, inte slot-start (Bug 22) – så att planen inte skiftar 15 min framåt mid-slot och triggar falsk "Outside plan window"-stopp. Regressionstest: `tests/test_charge_planner_bug22.py`.

### Plan-frysning efter RemoteStart (5 min)
`_last_remote_start` sätts när auto-start skickas. `_update_charge_plan()` blockeras i 5 minuter därefter för att undvika oscillation i uppstartsfasen.

### Manuell override
`_manual_start_requested = True` sätts i `async_start_charging()`. Stop-logiken respekterar flaggan och avbryter utan att stoppa laddningen. Nollställs när:
- Laddning avslutas naturligt
- Användaren klickar Stopp (`async_stop_charging()`)
- Auto-start tar över (RemoteStart från `_update_smart_charging()`)

### Symmetrisk mål-nått-koll (Bug 23)
`_charging_goal_reached() -> (bool, str)` är den gemensamma mål-nått-källan: **estimerad SOC ≥ target_soc** (via `soc_estimate.estimate_soc`, samma som planeraren – Bug 29) eller energi ≥ target_kwh. Den anropas i **både** stopp-grenen och auto-start-grenen i `_update_smart_charging()`. Innan Bug 23 kollade auto-start bara `plan.is_in_window()`, så när målet nåddes i ett öppet planfönster stoppade stopp-logiken medan auto-start startade om → 0 kWh-pingpong var 5:e minut tills fönstret krympte. Nu avstår auto-start (`Auto-start undertryckt – mål redan nått`) eftersom grenarna delar villkor. OBS: `_update_charge_plan()` har en egen, medvetet annorlunda mål-koll (utan plan-energi-villkoret) och delar **inte** hjälpmetoden. **Bug 29:** det tidigare `energi ≥ plan-energi`-villkoret är borttaget – eftersom planen räknas om mid-charge (Bug 16) från en estimerad SOC som själv inkluderar levererad energi (Bug 8) var `plan.energy_kwh` *återstående* energi, så `levererat ≥ plan.energy_kwh` triggade vid SOC-mittpunkten. Estimerad SOC ≥ target är det korrekta, icke-cirkulära kriteriet.

### Sessionsplan-frysning (Bug 28)
`_session_plan_intervals` fryser `plan.active_intervals` vid sessionstart (auto-start + manuell/Immediate). Window-stopp-grenen i `_update_smart_charging()` bedömer en **aktiv** session mot den frysta listan, inte mot `plan.is_in_window()`. Sedan Bug 16 körs `_update_charge_plan()` även mid-charge; utan frysning kunde en omräkning (morgondagens priser ~13:00) flytta fönstren bort från nuvarande tidpunkt → falsk "Outside plan window" → avbruten session som inte återupptas. Listan nollställs **endast** vid `Available` (kabelurkoppling), inte vid `Preparing` eller Garo 15-min-reset, så greedy-pauser inom kabelsessionen överlever. `None` = ingen aktiv frusen session → fallback till `plan.is_in_window()`. Designval: `allow_day_charging`/`_sync_allow_day_charging()` avbryter **inte** aktiv laddning (planeringsfilter, ej stopp-kommando); "stoppa nu" = stopp-knappen. **Bug 31:** listan persisteras i Store (ISO-serialiserade datetimes) och återställs i `_load_state()` – tidigare var den in-memory och en omstart mitt i laddning åter-exponerade "Outside plan window"-aborten.

## Nyckelkonstanter (const.py)
```python
DEFAULT_CHARGE_DEADLINE_HOUR        = 6      # Laddning klar senast 06:00
DEFAULT_BATTERY_CAPACITY_KWH        = 64.0
DEFAULT_CHARGE_EFFICIENCY           = 0.92   # AC→batteri-verkningsgrad
DEFAULT_VOLTAGE                     = 230    # V per fas
DEFAULT_SCHEDULE_DAY_START          = "06:00"
DEFAULT_SCHEDULE_NIGHT_START        = "22:00"
DEFAULT_SCHEDULE_DAY_CURRENT        = 6      # A
DEFAULT_SCHEDULE_NIGHT_CURRENT      = 16     # A
SCAN_INTERVAL_SECONDS               = 10
SMART_CHARGE_PRICE_THRESHOLD_PERCENTILE = 0.4  # fallback-tröskel
```

## Schema
- **Dag:** 06:00–22:00, 6A (GaroOwnerMaxCurrent=6)
- **Natt:** 22:00–06:00, 16A (GaroOwnerMaxCurrent=16)

## Garo-specifikt beteende
| Beteende | Hantering |
|----------|-----------|
| Strömgräns via `ChangeConfiguration key=GaroOwnerMaxCurrent` | Fungerar. ChargePointMaxProfile och TxProfile Rejected. |
| Autostart vid inkoppling utan RemoteStartTransaction | Garo startar automatiskt – HA behöver inte skicka RemoteStart |
| Skickar INTE om StartTransaction/StatusNotification vid reconnect | `transaction_id` läses från MeterValues-payload. `TriggerMessage StatusNotification` skickas 10s efter HA-start |
| Per-fas ström (L1/L2/L3), inget totalt faslöst värde | `current_a = mean(L1, L2, L3)` |

## ChargerState – viktiga fält (ocpp_client.py)
```python
transaction_id: Optional[int]       # None om ingen aktiv transaktion
session_id: str                      # "recovered-XXXX" om reconnect
cable_connected: bool
charging: bool
power_w: float                       # aktuell effekt i Watt
current_a: float                     # genomsnitt L1/L2/L3
accumulated_cost: float              # SEK, nollställs vid Preparing
accumulated_charging_seconds: int    # aktiv laddtid (pausar vid stop)
_charging_start: Optional[datetime]  # start av nuvarande laddningssegment
```

## OCPPCoordinator – viktiga fält (__init__.py)
```python
charge_plan: ChargePlan | None
_last_transaction_start: datetime | None  # för 90s grace period
_last_remote_start: datetime | None       # för 5 min plan-frysning
_last_remote_stop: datetime | None        # Fix 8: debounce dubbel RemoteStop (15s)
_manual_start_requested: bool             # manuell override-flagga
_session_plan_intervals: list[tuple] | None  # Bug 28: frysta planfönster för aktiv session (None = ingen)
_notified_connect_session: str | None     # dedup-guard anslutning
_notified_start_session: str | None       # dedup-guard start
_notified_stop_session: str | None        # dedup-guard stop
_cable_session_energy_kwh: float          # ackumulerad energi per kabelsession
_cable_session_cost_sek: float            # ackumulerad kostnad per kabelsession
_cable_session_start_notified: bool       # en start-notis per kabelsession
_cable_session_stop_notified: bool        # en stopp-notis per kabelsession
_cable_session_notified_connect: bool     # Fix 9: en inkopplad-notis per kabelsession
_session_total_kwh: float                 # Fix 7: ackumulerad energi sedan kabel in
_suspended_ev_since: datetime | None      # SuspendedEV-detektion
_cable_connect_time: datetime | None      # Fix 10: tid för kabelinkoppling
_soc_reread_done: bool                    # Fix 10: SOC omläst inom 30 min
_charging_started_at: datetime | None     # Bug 34: fryst faktisk laddstartstid för PlannedChargeStartSensor (None innan start/efter urkoppling)
_day_offer_notified_date: date | None     # Bug 18: en närvarobaserad dagladdningsnotis per kalenderdag
_day_charging_dismissed: bool             # Bug 3/21: användaren tryckt "🚫 Avsluta"
_day_charging_dismissed_until: datetime | None  # Bug 21: nollställs vid lokal midnatt
_deadline_entity_id: str                  # Feature 6: "input_datetime.charger_target_time" (deadline läses därifrån)
price_cap_ore_kwh: float                  # Feature 5: pristak öre/kWh (persisterad via Store), 0 = av
_price_cap_intervals: list[tuple]         # Feature 5: frusna planfönster för pristaksplanen
_price_cap_raw_slots: list[dict]          # Feature 5: [{time, price_kwh, energy_kwh}] för sensorn
target_soc: float                         # 80.0 default
battery_capacity_kwh: float               # 64.0 default
num_phases: int                           # 3
planner_algorithm: str                    # "Greedy (cheapest slots)"
```

## Entiteter

### Sensorer (23 st)
| Sensor | Beskrivning |
|--------|-------------|
| Status | Connector status (Available, Charging, etc.) |
| Charging Power | Effekt i Watt |
| Charging Current | Ström i A (medel L1+L2+L3) |
| Session Energy | kWh sedan StartTransaction |
| Session Cost | Upplupen faktisk kostnad SEK |
| Battery Level | % SOC |
| Charging Time | Aktiv laddtid i minuter |
| Estimated Completion | Timestamp när laddningen är klar |
| Estimated Charge Time Remaining | Återstående tid, format "2 h 15 min" |
| Current Electricity Price | öre/kWh |
| Session ID | Unik per session |
| Session Start | Timestamp |
| Charging Period | Day/Night/Override |
| Planned Charge Start | HH:MM lokal tid. Planerad kommande start innan laddning; fryses till faktisk starttid när laddning börjar (Bug 34) |
| Planned Charge End | HH:MM lokal tid. Speglar `estimated_completion` (ETA), inte `plan.end` (Bug 34) |
| Estimated Charge Cost | SEK från laddplan |
| Charge Goal Achievable | True/False |
| Chargeable Amount | % av laddmål som kan uppnås |
| Planner Savings | SEK skillnad mellan Greedy och Contiguous |
| Total Charging Cost | Kumulativ totalkostnad alla sessioner (SEK) |
| Charge Windows | Diagnostisk: laddplanens slots med planerad + faktisk energi (Feature 3) |
| Price Cap Status | Diagnostisk: antal slots ≤ pristaket + expected_kwh/expected_cost_sek (Feature 5) |

### Binära sensorer (3 st)
| Sensor | Beskrivning |
|--------|-------------|
| Cable Connected | Kabel inkopplad |
| Charging | Aktivt laddande |
| Charger Connected | OCPP WebSocket ansluten |

### Switchar (3 st)
| Switch | Beskrivning |
|--------|-------------|
| Auto Vehicle Detection | Auto-identifiera fordon vid inkoppling |
| Override Charging Schedule | Manuell override av dag/natt-schema |
| Allow Day Charging | Tillåt dagladdning i Smart-läge |

### Manuell deadline (HA-helper, ingen egen entitet)
Feature 6 tog bort `text.*_manual_deadline`. Deadlinen sätts nu i HA-helpern
`input_datetime.charger_target_time` (skapas manuellt; `00:00` = automatisk). Se avsnittet
"Manuell deadline (Feature 4 → Feature 6)".

### Number-entiteter (6 st)
| Number | Beskrivning |
|--------|-------------|
| Max Charging Current | Övre strömgräns (A) |
| Target Battery Level | Laddmål i % SOC |
| Target Energy | Laddmål i kWh (0 = obegränsat) |
| Battery Capacity | Batterikapacitet kWh |
| Override Current | Manuell strömgräns vid override |
| Price Cap | Pristak öre/kWh (0–500, 0 = av). Aktiverar pristaksläge i Smart (Feature 5). Persisterad via Store, rensas vid urkoppling. |

### Select-entiteter (3 st)
| Select | Beskrivning |
|--------|-------------|
| Charging Mode | Immediate / Smart / Scheduled |
| Active Vehicle | Välj aktivt fordon (visas om >1 fordon) |
| Planning Algorithm | Greedy (cheapest slots) / Contiguous (cheapest block) |

Alla tre ärver `CoordinatorEntity, SelectEntity` (Bug 32) så de push-uppdateras vid
`coordinator.async_set_updated_data()` oavsett trigger (notis, automation, egen selector) –
inte bara via egen `async_select_option()`.

### Knappar (2 st)
| Button | Beskrivning |
|--------|-------------|
| Start Charging | Starta laddning manuellt |
| Stop Charging | Stoppa laddning manuellt |

## Notiser
Tre events, var och en skickas max en gång per session (dedup-guards via session_id):
| Händelse | Trigger |
|----------|---------|
| `on_cable_connected` | `connector_status == Preparing` |
| `on_charging_started` | `charging=True` och `power_w > 100` (faktisk ström flödar) |
| `on_charging_stopped` | `charging=False` efter aktiv laddning |

Notiserna är åtgärdbara: `ocpp_use_day_charging` / `ocpp_use_night_charging`.

### Klickbar dashboard-URL (Feature 2)
Valfritt fält `CONF_NOTIFY_DASHBOARD_URL` i notis-konfigurationen. När det är satt injicerar `ChargerNotifier` `data.url` (iOS) + `data.clickAction` (Android) i alla notispayloads så att klick på notisen öppnar dashboarden i HA Companion-appen. Tomt fält → oförändrat beteende. Live-uppdateras via `_async_update_listener` utan omstart.

### Närvarobaserat dagladdningserbjudande (Bug 18)
När `allow_day_charging` är av (vardagars autoschema) men kabeln är inkopplad och någon av `PRESENCE_ENTITIES` (telefon/bilar, se `const.py`) är hemma efter `DAY_OFFER_EARLIEST_HOUR` (09:00), skickas `on_day_charging_chosen` **om** en dag-plan faktiskt blir billigare per kWh än natt-planen. Jämförelsen använder `avg_price_ore_kwh` (inte `estimated_cost_sek`), så en partiell natt-plan (innan morgondagens priser publicerats) suppresserar inte erbjudandet (Bug 17). Max en gång per kalenderdag (`_day_offer_notified_date`). "☀️ Dag"-knappen sätter `_force_day_plan=True`.

## OCPP-services (Developer Tools → Actions)
| Service | Beskrivning |
|---------|-------------|
| `ocpp_charger.get_configuration` | Hämtar Garo-konfiguration, svar på event `ocpp_charger_ocpp_response` |
| `ocpp_charger.change_configuration` | Ändrar Garo-konfiguration |
| `ocpp_charger.rest_call` | Gör REST-anrop via integrationen |

## Charge Windows-sensor (Feature 3)
Diagnostisk sensor `sensor.ocpp_charge_windows` som exponerar `charge_plan` som strukturerade
tidsblock (slots) med planerad energi/pris per slot samt post-hoc faktisk energi när sloten är klar.

**Ren logik i `charge_windows.py`** (stdlib-only, ingen HA-import → testbar fristående som
`charge_planner.py`; tester i `tests/test_charge_windows.py`, körs med `python3 tests/test_charge_windows.py`):
- `build_charge_windows(active_intervals, intervals, existing_slots, now, local_tz)` – bygger slot-dicts;
  bevarar `actual_energy_kwh` mellan omräkningar via slot-start-ISO.
- `update_windows_actual(windows, snapshots, current_cumulative_kwh, now)` – fyller `actual_energy_kwh`
  för avklarade slots (snapshot vid slotstart, delta vid slotslut).

**Koordinator-wrappers** (`__init__.py`): `_rebuild_charge_windows()` och `_update_charge_windows_actual()`
anropas i `_async_update_data()` efter `_update_charge_plan()`. `_rebuild_charge_windows()` anropas
dessutom **inuti** `_update_charge_plan()` (efter `_alt_plan` samt före natt-switch-returen) så att
sensorn synkas direkt vid direkta setter-anrop (algoritmbyte, target_soc m.m.) utan att vänta på
polling-cykeln (Bug 24). Rebuild körs bara när `charge_plan`
är ett nytt objekt (identitetsguard `_charge_windows_plan_ref`) så att `calculated_at` speglar verklig
omräkning, inte varje 10s-cykel. Energikälla: `_cable_session_energy_kwh` (+ aktiv tx-energi).
Snapshots i `_charge_windows_energy_at_slot_start` nycklas på slot-start-ISO.

`native_value` = antal slots; attribut = plan-metadata + `slots`-lista. OBS: vid infeasible/ingen plan
behålls senaste slots (rensas ej) – `calculated_at` visar åldern.

## Manuell deadline (Feature 4 → Feature 6)
Den manuella laddningsdeadlinen sätts via HA-helpern `input_datetime.charger_target_time`
(`has_time=True`, `has_date=False`). **Feature 6** ersatte den tidigare egna `ManualDeadlineText`
(`text.py`, borttagen). `00:00` = "ej satt" → automatiskt beteende (vardag 06:00, helg/dag slutet av
prisdata); valt klockslag används annars, rullar till imorgon om passerat.

**Ren logik i `deadline.py`** (stdlib-only, testbar fristående; `tests/test_deadline.py`, 22 tester):
- `parse_hhmm(value)` – `"H:MM"`/`"HH:MM"` → `(hour, minute)` med intervallkoll (0–23, 0–59), annars `None`.
- `helper_state_to_hhmm(state)` (Feature 6) – `input_datetime`-state (`"HH:MM:SS"`) → `"HH:MM"`;
  `00:00`/None/`unknown`/`unavailable`/ogiltigt → `""` (= automatisk).
- `compute_deadline(now_local, local_tz, all_prices, manual_deadline_str, deadline_hour, allow_day_charging)` –
  prioritet manuell → `allow_day_charging`/helg sista prisintervall +15 min (annars fallback 48h) → vardag
  06:00 (Bug 27).

**Koordinator (`__init__.py`):** `_get_manual_deadline_str()` läser helperns state via `helper_state_to_hhmm`;
`_compute_deadline()` skickar resultatet till `compute_deadline`. Vid kabelurkoppling (status `Available`)
nollar `_reset_deadline_helper()` helpern till `00:00:00` via `input_datetime.set_datetime` – **guardat**
så att ett saknat helper-objekt inte spammar fel. Ingen Store-nyckel längre (HA:s `input_datetime`-lagring
sköter persistensen); gammal `"manual_deadline"`-nyckel i Store ignoreras tyst.

OBS: helpern måste finnas (skapas manuellt i Inställningar → Hjälpare → Tid). Integrationen skapar den
**inte** (HA:s API för programmatisk skapning är instabilt/versionsberoende). Saknas den → automatisk
deadline. Den gamla `text.*_manual_deadline`-entiteten blir föräldralös efter deploy – radera manuellt.

## Pristaksladdning (Feature 5)
Number-entiteten `number.*_price_cap` (`Price Cap`, 0–500 öre/kWh) aktiverar ett pristaksläge i
Smart-läget. När `price_cap_ore_kwh > 0` bypassar `_update_charge_plan()` den ordinarie
cheapest-window-planeraren och anropar `_update_price_cap_plan()`: ladda **varje** 15-minutersslot
vars spotpris är ≤ taket. SoC-målet gäller fortfarande som övre gräns (`_charging_goal_reached()`).
Tak = 0 → ordinarie Smart-planering oförändrad.

**Ren logik i `price_cap.py`** (stdlib-only, importerar bara `charge_planner`-hjälparna, testbar
fristående; `tests/test_price_cap.py`, 11 tester, `python3 tests/test_price_cap.py`):
- `select_price_cap_slots(prices, cap_ore_kwh, now, deadline, *, power_fn, is_day_fn, allow_day_charging, local_tz)`
  → `PriceCapPlan` (qualifying_slots, merged active_intervals, total_kwh/cost, avg_ore).
  Filtrerar på slot-**slut** mot `now` (Bug 22-semantik) och deadline; exkluderar dagslots när
  `allow_day_charging=False`; droppar slots > taket.

**Koordinator-wrapper** `_update_price_cap_plan()` (`__init__.py`): tunn HA-glue som konverterar
priser till öre (`_to_ore_per_kwh`), bygger `power_fn` (schemamedveten via `schedule.current_limit_at`,
kapad av fordonets maxström) och `is_day_fn` (`schedule.is_day_time`), hämtar deadline via
`_compute_deadline()`, och bygger ett `ChargePlan` (inkl. `intervals` så Charge Windows-sensorn fungerar)
+ anropar `_rebuild_charge_windows()`. Inga slots → `charge_plan = None` (laddning pausad). Pristaks-
grenen ligger efter throttle/goal-reached/RemoteStart-frysningskollarna, så de gäller även här.

`price_cap_ore_kwh` persisteras via Store och nollställs vid kabelurkoppling (`Available`), precis som
Feature 4:s manuella deadline. Auto-start fryser `plan.active_intervals` i `_session_plan_intervals`
(Bug 28) även för pristaksplanen.

## Persistens (Store)
`self._store` (HA Storage) sparar bl.a. `cable_connected`, `transaction_id`, `energy_kwh`,
`price_cap_ore_kwh` (Feature 5),
`allow_day_charging`/`day_charging_manual_override` (Bug 26)
och `session_start_soc`/`session_total_kwh` (Bug 30) mellan omstarter.
- `_save_state()` anropas i varje `_async_update_data()`-cykel
- `_load_state()` anropas i `_delayed_soc_refresh()` (10s efter HA-start)
- **Bug 30:** `session_start_soc`/`session_total_kwh` återställs **efter** `set_active_vehicle()`
  i `_load_state()` (den nollställer dem). De håller SOC-estimatets baslinje i synk med dess
  energi över en omstart mitt i en laddning – annars dubbelräknas redan levererad energi och
  laddningen stoppar för tidigt ("Mål nått" vid fel SOC).

## Loggning
- Roterande debug-fil: `/config/ocpp_charger_debug.log` (5 MB × 3 filer)
- HA-log: `home-assistant.log` (filtreras med `grep -i ocpp_charger`)

## Testinstans
| Parameter | Värde |
|-----------|-------|
| Charger ID | `GaroCS-48671AA056E80` |
| Charger IP | `192.168.1.111:39324` |
| OCPP-port | `9000` |
| HA-server | `192.168.1.97` |
| Fordon | Kia eNiro, 64 kWh |
| SOC-entitet | `sensor.e_niro_ev_battery_level` |
| Prisintervall | `sensor.gespot_current_price_se3` |
| Notiser | `notify.mobile_app_sm_s918b` |
