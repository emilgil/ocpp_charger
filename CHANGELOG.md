# Ändringslogg – OCPP Charger (2026-03-18)

## 2026-03-18: Kabelsession-modell och SuspendedEV-hantering

### Bug 6 – Kabelsession: energi/kostnad nollställs vid varje OCPP-transaktionsstopp

**Problem:** Energi och kostnad nollställdes vid varje planmässigt stopp. Multi-window-planer (tx 1, tx 2) tappade energidata.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nya fält: `_cable_session_energy_kwh`, `_cable_session_cost_sek`, `_cable_session_start_notified`, `_cable_session_stop_notified`, `_cable_session_start_time`. |
| `__init__.py` | `_check_notify_events()` | Nollställer kabelsessions-fält vid `Available → Preparing`. Skickar stopp-notis vid kabelurkoppling. |
| `__init__.py` | `_save_state()` / `_load_state()` | Persistens av `cable_session_energy_kwh` och `cable_session_cost_sek`. |
| `__init__.py` | `_cable_session_elapsed_minutes()` | Ny hjälpmetod. |
| `ocpp_client.py` | `StopTransaction` | Ackumulerar `tx_energy_kwh` och `tx_cost_sek` till coordinatorns kabelsession istället för att nollställa. |
| `sensor.py` | `ChargerEnergySensor` | Visar `cable_session_energy + aktiv tx_energy`. |
| `sensor.py` | `SessionCostSensor` | Visar `cable_session_cost + aktiv tx_cost`. |

### Bug 5 – SuspendedEV avslutar inte laddningen

**Problem:** Vid SuspendedEV (bilen nöjd) hölls transaktionen öppen. Auto-start skickade RemoteStart trots aktiv transaktion.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nytt fält `_suspended_ev_since`. |
| `__init__.py` | `_update_smart_charging()` | SuspendedEV-guard: om SuspendedEV + power<100W i >60s → RemoteStop + stopp-notis. |
| `__init__.py` | `_update_smart_charging()` | Auto-start-guard: hoppar över om `transaction_id is not None`. |

### Bug 4 – Gammal SOC i stopp-notisen (uppdaterad)

**Problem:** Stopp-notisen visar gammal SOC. Uppdaterat: `kia_uvo.force_update` triggas, 60s fördröjning (upp från 15s).

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_send_stop_notification()` | Ny hjälpfunktion. Dedup via `_cable_session_stop_notified`. Triggar `kia_uvo.force_update`, väntar 60s, hämtar färsk SOC. Använder kabelsessions energi/kostnad. |

### Bug 2 – Start-notis per kabelsession (uppdaterad)

**Problem:** En start-notis per OCPP-transaktion istället för per kabelsession.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_check_notify_events()` | Använder `_cable_session_start_notified` som guard. Använder `power_w / 1000` istället för beräknad power. |

---

## 2026-03-14: Ursprungliga bugfixar



## Bug 1 – Målnivå stoppar inte laddningen i plan-läge

**Problem:** Bilen laddade till 88% trots att målnivån var 80%. Smart charging i plan-läge kontrollerade bara om klockan var inom planfönstret, inte om SOC/kWh-målet var nått.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_smart_charging()` | Lagt till SOC/kWh/plan-energi-kontroll **före** fönsterkontrollen. Om målet är nått stoppas laddningen omedelbart med `remote_stop_transaction()`, oavsett planfönster. |
| `__init__.py` | `_update_charge_plan()` | Lagt till early exit om SOC- eller kWh-mål redan är nått – hoppar över planberäkning helt. |

---

## Bug 2 – Notis-storm och felaktig sluttid

**Problem:** Flera "Laddning startad"-notiser per session. Sluttiden baserades på ETA-beräkning istället för laddplanens sluttid.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Ny flagga `_start_notified_this_connection: bool` – förhindrar fler än en start-notis per kabelanslutning. |
| `__init__.py` | `_check_notify_events()` | Villkoret för start-notis ändrat från `_notified_start_session != session_id` till `not _start_notified_this_connection`. Flaggan nollställs vid `Available` och ny `Preparing`. |
| `__init__.py` | `_check_notify_events()` | Skickar med `plan_end` (från `charge_plan`) till `on_charging_started()`. |
| `notifier.py` | `on_charging_started()` | Ny parameter `plan_end`. Prioriteras över `estimated_end` – visar planens sluttid om den finns. |

---

## Bug 3 – Dag/natt-notis skickas trots att målnivån är nådd, och kan inte avbrytas

**Problem:** Upprepade notiser om att dagladdning är billigare, ingen möjlighet att avfärda dem.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `const.py` | – | Ny konstant `NOTIFY_ACTION_DISMISS = "ocpp_dismiss_day_charging"`. |
| `__init__.py` | `__init__()` | Ny flagga `_day_charging_dismissed: bool` – sätts av dismiss-action. |
| `__init__.py` | `_handle_notification_action()` | Hanterar `NOTIFY_ACTION_DISMISS`: sätter `_day_charging_dismissed = True`, stänger av dagladdning, omberäknar plan. |
| `__init__.py` | `_check_notify_events()` | Nollställer `_day_charging_dismissed` vid `Available` (kabel urkopplad). |
| `__init__.py` | `_update_charge_plan()` | Skyddar `on_day_charging_chosen()`-anropet med `not self._day_charging_dismissed`. |
| `notifier.py` | `on_day_charging_chosen()` | Ny "🚫 Avsluta"-knapp i actions-listan. `tag: "ocpp_day_night_choice"` tillagd för att identifiera notisen. |
| `notifier.py` | `dismiss_day_night_notification()` | Ny metod – rensar dag/natt-notisen från telefonen via `clear_notification` + tag. |
| `__init__.py` | `_handle_notification_action()` | Anropar `dismiss_day_night_notification()` vid dismiss-action. |
| `const.py` | – | Borttagen oanvänd konstant `SENSOR_COST` (orphan efter Bug 6). |

---

## Bug 4 – Gammal SOC i stopp-notisen

**Problem:** Stopp-notisen visar gammal SOC eftersom bilen inte hunnit rapportera uppdaterat värde.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_check_notify_events()` | Stopp-notisen fördröjs 15 sekunder med `async_call_later()`. Energi och kostnad sparas vid stopp-ögonblicket, men SOC hämtas färskt via `_update_soc_from_ha()` precis innan notisen skickas. |

---

---

## P2 – ConfigEntryNotReady vid uppstartsfel

**Problem:** Om OCPP-servern inte kan starta (t.ex. port upptagen) misslyckas integrationen tyst utan att HA visar felstatus eller försöker igen.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `async_setup_entry()` | `OSError` från `async_start()` fångas och kastas som `ConfigEntryNotReady`, så HA visar felstatus och försöker igen automatiskt. |

---

## P3 – Kodkvalitet och HA best practices

### P3a – zoneinfo-import flyttad till modulnivå

**Problem:** `import zoneinfo` och `from datetime import timezone` utfördes inuti sensor-properties, vilket är ineffektivt.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `sensor.py` | modulnivå | `import zoneinfo` och `from datetime import timezone` flyttade till toppen av filen. Inline-importer i `PlannedChargeStartSensor`, `PlannedChargeEndSensor` och `ChargerSessionEndSensor` borttagna. |

### P3b – EntityCategory.DIAGNOSTIC

**Problem:** Diagnostiksensorer (Session ID, Session Start, Planner Savings, Charging Period) visades som primära sensorer i HA UI.

| Fil | Sensor | Ändring |
|-----|--------|---------|
| `sensor.py` | `ChargerSessionIDSensor` | `entity_category = EntityCategory.DIAGNOSTIC` |
| `sensor.py` | `ChargerSessionStartSensor` | `entity_category = EntityCategory.DIAGNOSTIC` |
| `sensor.py` | `PlannerSavingsSensor` | `entity_category = EntityCategory.DIAGNOSTIC` |
| `sensor.py` | `SchedulePeriodSensor` | `entity_category = EntityCategory.DIAGNOSTIC` |

### P3c – manifest.json

| Ändring | Före | Efter |
|---------|------|-------|
| Version | `1.0.0` | `1.1.0` |
| websockets | `>=11.0` | `>=11.0` (övre gräns reverterad – HA stöder ej kommaseparerade constraints) |

---

## Bug 5 – Estimated Charge Time Remaining visar absurt värde

**Problem:** Sensorn visar t.ex. "8 h 12 min" trots att planen är 00:45–01:00 (15 min). `_update_eta()` beräknade ETA från `power_w` som kunde vara ~0 (väntar på planfönstret) eller baserat på dagschema (6A). `charging`-flaggan är opålitlig vid reconnect/Unknown-status – `power_w` är alltid korrekt.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_eta()` | Använder `power_w < 100` som primärt idle-villkor istället för `charging`-flaggan. Vid idle + feasible plan → `plan.end`. Vid idle utan plan → `None`. Vid aktiv laddning (≥100W) → beräkna från faktisk `power_w`. |
| `__init__.py` | `_update_eta()` | Nytt fält `estimated_remaining_minutes`. Vid idle = `plan.duration_minutes` (faktisk aktiv laddtid). Vid laddning = `eta - now`. Förhindrar att sensorn visar "7h" när planen har 30 min aktiv laddning. |
| `sensor.py` | `ChargerSessionEndSensor` | Använder `estimated_remaining_minutes` direkt istället för `eta - now()`. |
| `__init__.py` | `elapsed_seconds` | Returnerar `None` när `transaction_id is None` (ingen aktiv session). Förhindrar stale Charging Time efter sessionsslut. |

---

## Bug 6 – Dubblerad session_cost-sensor

**Problem:** Två sensorer med samma namn "Session Cost" registrerades: `ChargerCostSensor` (unique_id `cost`) och `SessionCostSensor` (unique_id `session_cost`). Båda visade `accumulated_cost`, vilket skapade förvirring i HA.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `sensor.py` | `ChargerCostSensor` | Klass borttagen – `SessionCostSensor` är bättre (returnerar `None` utanför aktiv session). |
| `sensor.py` | `async_setup_entry()` | `ChargerCostSensor(coordinator, entry)` borttagen från entitetslistan. |
| `sensor.py` | import | `SENSOR_COST` borttagen från const-importen. |

---

## Ny sensor – Total Charging Cost

**Syfte:** Kumulativ totalkostnad över alla laddningssessioner. `SensorStateClass.TOTAL` gör att HA integrerar värdet i energidashboarden och långtidsstatistiken automatiskt.

**Ändringar:**

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `const.py` | – | Ny konstant `SENSOR_TOTAL_COST = "total_charging_cost"`. |
| `ocpp_client.py` | `ChargerState` | Nytt fält `total_cost: float = 0.0`. |
| `ocpp_client.py` | `StopTransaction`-hanterare | `total_cost += accumulated_cost` vid sessionsslut. |
| `sensor.py` | `TotalChargingCostSensor` | Ny sensorklass med `SensorStateClass.TOTAL`, enhet SEK. |
| `sensor.py` | `async_setup_entry()` | `TotalChargingCostSensor` tillagd i entitetslistan. |
| `__init__.py` | `_save_state()` | `total_cost` sparas till HA storage. |
| `__init__.py` | `_load_state()` | `total_cost` laddas från HA storage vid omstart. |

---

## Sammanfattning av ändrade filer

| Fil | Ändringar | Kategori |
|-----|-----------|----------|
| `__init__.py` | 13 ändringar | Bug 1-5, P2, Total Cost |
| `notifier.py` | 3 ändringar | Bug 2, 3 |
| `const.py` | 2 ändringar | Bug 3, Total Cost |
| `sensor.py` | 10 ändringar | Bug 6, P3a, P3b, Total Cost |
| `ocpp_client.py` | 2 ändringar | Total Cost |
| `manifest.json` | 1 ändring | P3c |
