# OCPP EV-laddare – Utvecklardokumentation

## Arkitektur

Home Assistant fungerar som OCPP 1.6 Central System (WebSocket-server) på port 9000. Laddboxen (Garo) ansluter till HA, inte tvärtom.

```
Garo-laddbox  ──WebSocket/OCPP 1.6──►  HA (port 9000)
                                         │
                                    OCPPCoordinator
                                    ├── OCPPClient (ChargerState)
                                    ├── ChargePlanner
                                    ├── SmartChargeController
                                    ├── CurrentSchedule
                                    └── ChargerNotifier
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

## Deploy-kommandon

```bash
# Kopiera alla Python-filer till HA
scp -r custom_components/ocpp_charger/*.py root@192.168.1.97:/config/custom_components/ocpp_charger/

# Starta om HA
ssh root@192.168.1.97 "ha core restart"

# Följ loggen
ssh root@192.168.1.97 "grep -i ocpp_charger /config/home-assistant.log | grep -v SmartThings | tail -30"

# Debug-logg (roterande fil, mer verbose)
ssh root@192.168.1.97 "tail -f /config/ocpp_charger_debug.log"
```

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

## Laddningsstyrning – prioritetsordning

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
session_start: Optional[datetime]    # timestamp för sessionstart
session_energy_start: Optional[float] # meter-avläsning vid sessionstart
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
allow_day_charging: bool                  # tillåt dagladdning
_force_day_plan: bool                     # tvinga dagsplanering
_alt_plan: ChargePlan | None              # alternativ plan för jämförelse
```

## Garo-specifikt beteende

| Beteende | Hantering |
|----------|-----------|
| Strömgräns via `ChangeConfiguration key=GaroOwnerMaxCurrent` | Fungerar. ChargePointMaxProfile och TxProfile Rejected. |
| Autostart vid inkoppling utan RemoteStartTransaction | Garo startar automatiskt – HA behöver inte skicka RemoteStart |
| Skickar INTE om StartTransaction/StatusNotification vid reconnect | `transaction_id` läses från MeterValues-payload. `TriggerMessage StatusNotification` skickas 10s efter HA-start |
| Per-fas ström (L1/L2/L3), inget totalt faslöst värde | `current_a = mean(L1, L2, L3)` |

## Entiteter

### Sensorer (20 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| ChargerStatusSensor | status | Status |
| SchedulePeriodSensor | schedule_period | Charging Period |
| ChargerPowerSensor | power | Charging Power |
| ChargerCurrentSensor | current | Charging Current |
| ChargerEnergySensor | energy | Session Energy |
| ChargerCostSensor | cost | Session Cost |
| ChargerSOCSensor | soc | Battery Level |
| ChargerElapsedSensor | elapsed_time | Charging Time |
| ChargerETASensor | estimated_completion | Estimated Completion |
| ChargerPriceSensor | current_price | Current Electricity Price |
| ChargerSessionIDSensor | session_id | Session ID |
| ChargerSessionStartSensor | session_start | Session Start |
| ChargerSessionEndSensor | session_end | Estimated Charge Time Remaining |
| PlannedChargeStartSensor | planned_charge_start | Planned Charge Start |
| PlannedChargeEndSensor | planned_charge_end | Planned Charge End |
| EstimatedChargeCostSensor | estimated_charge_cost | Estimated Charge Cost |
| SessionCostSensor | session_cost | Session Cost |
| ChargeGoalAchievableSensor | charge_goal_achievable | Charge Goal Achievable |
| ChargeCapacitySensor | charge_capacity | Chargeable Amount |
| PlannerSavingsSensor | planner_savings | Planner Savings |
| TotalChargingCostSensor | total_charging_cost | Total Charging Cost |

### Binära sensorer (3 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| CableConnectedBinarySensor | cable_connected | Cable Connected |
| ChargingActiveBinarySensor | charging_active | Charging |
| ChargerOnlineBinarySensor | charger_online | Charger Connected |

### Switchar (3 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| AutoVehicleDetectionSwitch | auto_vehicle_detection | Auto Vehicle Detection |
| ScheduleOverrideSwitch | schedule_override | Override Charging Schedule |
| AllowDayChargingSwitch | allow_day_charging | Allow Day Charging |

### Number-entiteter (5 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| MaxCurrentNumber | max_current_limit | Max Charging Current |
| TargetSOCNumber | target_soc | Target Battery Level |
| TargetKWhNumber | target_kwh | Target Energy |
| BatteryCapacityNumber | battery_capacity | Battery Capacity |
| OverrideCurrentNumber | override_current | Override Current |

### Select-entiteter (3 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| ChargeModeSelect | charge_mode | Charging Mode |
| ActiveVehicleSelect | active_vehicle | Active Vehicle (visas om >1 fordon) |
| PlannerAlgorithmSelect | planner_algorithm | Planning Algorithm |

### Knappar (2 st)
| Klass | Unique suffix | Namn |
|-------|---------------|------|
| StartChargingButton | start_charging | Start Charging |
| StopChargingButton | stop_charging | Stop Charging |

## Notiser

Tre händelser, var och en skickas max en gång per session (dedup-guards via session_id):

| Händelse | Trigger |
|----------|---------|
| `on_cable_connected` | `connector_status == Preparing` |
| `on_charging_started` | `charging=True` och `power_w > 100` (faktisk ström flödar) |
| `on_charging_stopped` | `charging=False` efter aktiv laddning |

Notiserna är åtgärdbara: `ocpp_use_day_charging` / `ocpp_use_night_charging`.

## Persistens (Store)

`self._store` (HA Storage) sparar `cable_connected` och `transaction_id` mellan omstarter.

- `_save_state()` anropas i varje `_async_update_data()`-cykel
- `_load_state()` anropas i `_delayed_soc_refresh()` (10s efter HA-start)

## Loggning

- Roterande debug-fil: `/config/ocpp_charger_debug.log` (5 MB × 3 filer via `RotatingFileHandler`)
- HA-log: `home-assistant.log` (filtreras med `grep -i ocpp_charger`)

## OCPP-services

Tillgängliga via **Developer Tools → Actions**:

| Service | Beskrivning |
|---------|-------------|
| `ocpp_charger.get_configuration` | Hämtar Garo-konfiguration, svar på event `ocpp_charger_ocpp_response` |
| `ocpp_charger.change_configuration` | Ändrar Garo-konfiguration |
| `ocpp_charger.rest_call` | Gör REST-anrop via integrationen |

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

## Planeringsalgoritmer

### Greedy (cheapest slots)
Väljer de globalt billigaste intervallen oberoende av ordning. Kan resultera i flera separata laddfönster med pauser mellan.

### Contiguous (cheapest block)
Hittar det billigaste sammanhängande blocket som ger tillräcklig laddtid. Ett enda laddfönster utan avbrott.

Sensorn **Planner Savings** visar skillnaden i kostnad mellan algoritmerna (positivt = Greedy billigare).

## SOC-enheter

Stöder två SOC-enheter:
- `percent` – SOC i procent (0–100%)
- `kwh` – SOC i kWh (konverteras till % baserat på batterikapacitet)

Konfigureras per fordon i setup-flow.
