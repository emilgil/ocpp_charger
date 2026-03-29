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
ssh root@192.168.1.97 "grep -i ocpp_charger /config/home-assistant.log | grep -v SmartThings | tail -30"

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
  sensor.py            – 20 sensorer
  binary_sensor.py     – 3 binära sensorer
  number.py            – 5 number-entiteter
  select.py            – 3 select-entiteter
  switch.py            – 3 switchar
  button.py            – 2 knappar
  vehicle_detection.py – Auto-identifiering av fordon
  current_schedule.py  – Dag/natt-schema
  smart_charge.py      – Prisbeslut (fallback när ingen plan finns)
  charge_planner.py    – Optimal laddplanering baserat på spotpriser
  notifier.py          – Push-notiser
  rest_client.py       – Async HTTP-klient
  manifest.json
  services.yaml
```

## Arkitektur – laddningsstyrning (prioritetsordning)
1. **Charge mode = Immediate** → ladda alltid
2. **Charge mode = Smart + feasible plan** → ladda ENDAST inom `plan.start–plan.end`
   - Auto-start: `_update_smart_charging()` skickar RemoteStart när klockan passerar `plan.start`
   - Auto-stop: RemoteStop vid `plan.end`
3. **Charge mode = Smart + ingen plan** → priströskel-fallback (40:e percentilen)
4. **Charge mode = Scheduled** → ladda inom konfigurerad tidsperiod

## Viktiga skyddsmekanismer

### Grace period (90s)
Ingen stop-logik körs inom 90 sekunder efter `StartTransaction`. Förhindrar att en nyss startad session stoppas omedelbart av stop-logiken.

### Plan-frysning under laddning
`_update_charge_plan()` anropas **inte** när `state.charging == True`. Förhindrar att planen räknas om och oscillerar under pågående laddning.

### Plan-frysning efter RemoteStart (5 min)
`_last_remote_start` sätts när auto-start skickas. `_update_charge_plan()` blockeras i 5 minuter därefter för att undvika oscillation i uppstartsfasen.

### Manuell override
`_manual_start_requested = True` sätts i `async_start_charging()`. Stop-logiken respekterar flaggan och avbryter utan att stoppa laddningen. Nollställs när:
- Laddning avslutas naturligt
- Användaren klickar Stopp (`async_stop_charging()`)
- Auto-start tar över (RemoteStart från `_update_smart_charging()`)

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
_manual_start_requested: bool             # manuell override-flagga
_notified_connect_session: str | None     # dedup-guard anslutning
_notified_start_session: str | None       # dedup-guard start
_notified_stop_session: str | None        # dedup-guard stop
target_soc: float                         # 80.0 default
battery_capacity_kwh: float               # 64.0 default
num_phases: int                           # 3
planner_algorithm: str                    # "Greedy (cheapest slots)"
```

## Entiteter

### Sensorer (20 st)
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
| Planned Charge Start | HH:MM lokal tid |
| Planned Charge End | HH:MM lokal tid |
| Estimated Charge Cost | SEK från laddplan |
| Charge Goal Achievable | True/False |
| Chargeable Amount | % av laddmål som kan uppnås |
| Planner Savings | SEK skillnad mellan Greedy och Contiguous |

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

### Number-entiteter (5 st)
| Number | Beskrivning |
|--------|-------------|
| Max Charging Current | Övre strömgräns (A) |
| Target Battery Level | Laddmål i % SOC |
| Target Energy | Laddmål i kWh (0 = obegränsat) |
| Battery Capacity | Batterikapacitet kWh |
| Override Current | Manuell strömgräns vid override |

### Select-entiteter (3 st)
| Select | Beskrivning |
|--------|-------------|
| Charging Mode | Immediate / Smart / Scheduled |
| Active Vehicle | Välj aktivt fordon (visas om >1 fordon) |
| Planning Algorithm | Greedy (cheapest slots) / Contiguous (cheapest block) |

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

## OCPP-services (Developer Tools → Actions)
| Service | Beskrivning |
|---------|-------------|
| `ocpp_charger.get_configuration` | Hämtar Garo-konfiguration, svar på event `ocpp_charger_ocpp_response` |
| `ocpp_charger.change_configuration` | Ändrar Garo-konfiguration |
| `ocpp_charger.rest_call` | Gör REST-anrop via integrationen |

## Persistens (Store)
`self._store` (HA Storage) sparar `cable_connected` och `transaction_id` mellan omstarter.
- `_save_state()` anropas i varje `_async_update_data()`-cykel
- `_load_state()` anropas i `_delayed_soc_refresh()` (10s efter HA-start)

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
