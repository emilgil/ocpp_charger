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
# Kopiera ändrade filer till HA
scp custom_components/ocpp_charger/<fil.py> root@192.168.1.97:/config/custom_components/ocpp_charger/

# Starta om HA
ssh root@192.168.1.97 "ha core restart"

# Följ loggen
ssh root@192.168.1.97 "grep -i ocpp_charger /config/home-assistant.log | grep -v SmartThings | tail -30"
```

## Filstruktur
```
custom_components/ocpp_charger/
  __init__.py          – OCPPCoordinator, smart charging, kostnad, notiser, auto-start
  ocpp_client.py       – WebSocket OCPP 1.6 server, ChargerState
  config_flow.py       – Setup flow (4 steg) + options flow
  const.py             – Alla konstanter
  sensor.py            – 17+ sensorer
  binary_sensor.py     – 3 binära sensorer
  number.py            – 5 number-entiteter
  select.py            – 2 select-entiteter
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
1. **Charge mode = Always** → ladda alltid
2. **Charge mode = Smart + feasible plan** → ladda ENDAST inom `plan.start–plan.end`
   - Auto-start: `_update_smart_charging()` skickar RemoteStart när klockan passerar `plan.start`
   - Auto-stop: RemoteStop vid `plan.end`
3. **Charge mode = Smart + ingen plan** → priströskel-fallback (50:e percentilen × 0.97)
4. **Charge mode = Scheduled** → ladda inom konfigurerad tidsperiod

**Grace period:** Ingen stop-logik körs inom 90s efter StartTransaction (förhindrar att manuell start stoppas direkt).

## Nyckelkonstanter (const.py)
```python
DEFAULT_CHARGE_DEADLINE_HOUR = 6    # Laddning klar senast 06:00
DEFAULT_BATTERY_CAPACITY_KWH = 64.0
DEFAULT_CHARGE_EFFICIENCY = 0.92
DEFAULT_VOLTAGE = 230
```

## Schema
- **Dag:** 06:00–22:00, 6A (GaroOwnerMaxCurrent=6)
- **Natt:** 22:00–06:00, 16A (GaroOwnerMaxCurrent=16)

## Garo-specifikt beteende
- **Strömgräns:** `ChangeConfiguration key=GaroOwnerMaxCurrent value=X` fungerar. ChargePointMaxProfile och TxProfile Rejected.
- **Autostart:** Garo startar laddning automatiskt vid inkoppling utan RemoteStartTransaction
- **Reconnect:** Garo skickar INTE om StartTransaction eller StatusNotification vid OCPP-reconnect
  - Fix: `transaction_id` läses från MeterValues-payload
  - Fix: `TriggerMessage StatusNotification` skickas 10s efter HA-start
- **Per-fas ström:** Garo skickar bara L1/L2/L3, aldrig faslöst totalvärde → `current_a = mean(L1,L2,L3)`

## ChargerState – viktiga fält (ocpp_client.py)
```python
transaction_id: Optional[int]       # None om inte aktiv
session_id: str                      # "recovered-XXXX" om reconnect
cable_connected: bool
charging: bool
accumulated_cost: float              # SEK, nollställs vid Preparing
accumulated_charging_seconds: int    # aktiv laddtid (pausar vid stop)
_charging_start: Optional[datetime]  # start av nuvarande laddningssegment
_current_l1/l2/l3: float            # per-fas ström
```

## OCPPCoordinator – viktiga fält (__init__.py)
```python
charge_plan: ChargePlan | None
_last_transaction_start: datetime    # för grace period
_last_cost_energy_kwh: float         # för inkrementell kostnad
_notified_connect_session: str       # dedup-guard
_notified_start_session: str         # dedup-guard
_notified_stop_session: str          # dedup-guard
target_soc: float                    # 80.0 default
battery_capacity_kwh: float          # 64.0
num_phases: int                      # 3
```

## Sensorlista
| Sensor | Beskrivning |
|--------|-------------|
| Charging Power | Watt |
| Charging Current | A (medel L1+L2+L3) |
| Session Energy | kWh sedan StartTransaction |
| Battery Level | % SOC |
| Charging Time | Aktiv laddtid i minuter (pausar vid stop) |
| Estimated Completion | Timestamp när laddningen är klar |
| Estimated Charge Time Remaining | Återstående tid, format "2 h 15 min" |
| Current Electricity Price | öre/kWh |
| Session ID | Unik per session |
| Session Start | Timestamp |
| Schedule Period | Day/Night |
| Planned Charge Start | HH:MM lokal tid |
| Planned Charge End | HH:MM lokal tid |
| Estimated Charge Cost | SEK från laddplan |
| Session Cost | Upplupen faktisk kostnad SEK |
| Charge Goal Achievable | True/False |
| Chargeable Amount | % av laddmål som kan uppnås |

## Notiser
Fyra events, var och en skickas max 1 gång per session (dedup-guards):
- `on_cable_connected` – vid Preparing-status
- `on_charging_started` – vid charging=True
- `on_charging_stopped` – vid charging=False efter aktiv laddning
- Test-notis finns i Options flow

## OCPP-services (Developer Tools → Actions)
- `ocpp_charger.get_configuration` – hämtar Garo-konfiguration, svar på event `ocpp_charger_ocpp_response`
- `ocpp_charger.change_configuration` – ändrar Garo-konfiguration

## Kända öppna punkter (2026-03-10)
- Auto-start baserat på laddplan: implementerat men ej verifierat i natt än
- Manuell start med grace period: implementerat men ej verifierat
- Imorgondagens priser publiceras ca 13:00 → laddplan uppdateras automatiskt inom 5 min
