# Ändringslogg – OCPP Charger

## 2026-06-12: Feature 4 – Manuell deadline via textinmatning (ersätter Deadline Override-switch)

**Vad:** Ny text-entitet `text.ocpp_manual_deadline` där användaren skriver in ett klockslag `HH:MM` som laddningsdeadline. Tomt fält = automatiskt beteende (vardag 06:00, helg ingen fast deadline). Passerad tid idag → imorgon samma tid. Fältet rensas automatiskt vid kabelurkoppling. **Deadline Override-switchen (Feature 1) tas bort.**

**Bakgrundsbug:** Den gamla `deadline_override`-flaggan (bool i minnet) nollställdes av konkurrerande uppdateringscykler. `manual_deadline_str` sparas därför persistent via Store (nyckel `"manual_deadline"`) och skrivs direkt vid varje ändring (`await _save_state()`) samt vid urkoppling (`hass.async_create_task(self._save_state())`).

**Design:** Deadline-logiken bröts ut till en ren, stdlib-only-modul `deadline.py` (`parse_hhmm` + `compute_deadline`), testbar fristående precis som `charge_planner.py`. `_compute_deadline()` blev en tunn wrapper; `text.py` återanvänder `parse_hhmm` för validering (intervallkoll 0–23/0–59, så `24:00` avvisas).

| Fil | Ändring |
|-----|---------|
| `deadline.py` | Ny modul: `parse_hhmm()` + `compute_deadline()` |
| `text.py` | Ny `text`-plattform: `ManualDeadlineText` |
| `__init__.py` | Fält `manual_deadline_str` (Store save/load), tunn `_compute_deadline`, rensa+spara vid Available, `Platform.TEXT`, borttagen `deadline_override` |
| `switch.py` | Borttagen `DeadlineOverrideSwitch` (klass + registrering + import) |
| `tests/test_deadline.py` | 11 fristående enhetstester (`python3 tests/test_deadline.py`) |

**Status:** Implementerad och lokalt verifierad (11 deadline-tester PASS, alla moduler kompilerar). **Ännu ej deployad till live-HA** (deploy pausad på användarens begäran). Vid deploy: den gamla `switch.*_deadline_override`-entiteten blir föräldralös i registret och kan raderas manuellt.

---

## 2026-06-12: Feature 3 – Charge Windows-sensor

**Vad:** Ny diagnostisk sensor `sensor.ocpp_charge_windows` som exponerar `charge_plan` som strukturerade tidsblock (slots). Varje slot loggar planerad energi och viktat pris; när sloten är avklarad fylls faktisk överförd energi i post-hoc via kumulativ kabelsessionenergi. `native_value` = antal slots, attributen innehåller plan-metadata + `slots`-lista. Används för felsökning och bakgrundsvisualisering.

**Design:** Den rena logiken lever i en ny stdlib-only-modul `charge_windows.py` (ingen HA-import → testbar fristående precis som `charge_planner.py`). Koordinatorn anropar två tunna wrappers i `_async_update_data()` efter `_update_charge_plan()`. Rebuild körs bara när planen är ett nytt objekt (identitetsguard), så `calculated_at` speglar verklig omräkning. Energisnapshots nycklas på slot-start-ISO så att de överlever planomräkningar som ändrar slot-ordning.

| Fil | Ändring |
|-----|---------|
| `charge_windows.py` | Ny modul: `build_charge_windows()` + `update_windows_actual()` |
| `__init__.py` | Fält `_charge_windows`/`_charge_windows_meta`/`_charge_windows_energy_at_slot_start`/`_charge_windows_plan_ref`; wrappers `_rebuild_charge_windows()` + `_update_charge_windows_actual()`; anrop i `_async_update_data()` |
| `sensor.py` | Ny klass `ChargeWindowsSensor` + registrering |
| `const.py` | `SENSOR_CHARGE_WINDOWS = "charge_windows"` |
| `tests/test_charge_windows.py` | 8 fristående enhetstester (`python3 tests/test_charge_windows.py`) |

**Deployad och verifierad** 2026-06-12: komponenten laddar utan fel, `[ChargeWindows] Rebuilt 1 slots`-debugrad bekräftar att rebuild körs end-to-end, varje uppdateringscykel `success: True`.

---

## 2026-06-12: Bug 23 – Auto-start och mål-nått-stopp pingpongade när målet nåddes i öppet planfönster

**Problem:** När mål-SOC nåddes mitt i ett fortfarande öppet planfönster (natten 2026-06-12 kl ~04:14, Skoda Enyaq, mål 60%) skickade HA sex korta RemoteStart/RemoteStop-cykler à 0 kWh fram till att fönstret krympte (~05:10). Orsak: auto-start-grenen i `_update_smart_charging()` kollade bara `plan.is_in_window()`, aldrig om målet redan var nått, medan stopp-grenen stoppade på mål-nått. De två slogs mot varandra. 300s-guarden begränsade bara frekvensen (var 5:e min) och 15s-stopp-debouncen skyddade bara stopp-sidan. Ofarligt (0 kWh, ingen kostnad) men slitage + loggbrus. Bilen var inte inblandad – dess egen gräns stod på 80%.

**Fix:** Mål-nått-logiken bröts ut till hjälpmetoden `_charging_goal_reached()` som nu anropas i **både** stopp- och auto-start-grenen. Auto-start avstår (`Auto-start undertryckt – mål redan nått`) när målet är nått, så grenarna inte längre kan vara oense.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | Ny `_charging_goal_reached()`; auto-start-gren avstår vid mål nått; stopp-gren refaktorerad att dela samma villkor |

---

## 2026-06-11: Bug 22 – Planen skiftade framåt mid-slot och stoppade pågående laddning

**Problem:** Under nattladdning (plan 02:30–04:45) laddade bilen bara ~5 min per kvart: laddning 02:30–02:35, paus till 02:45, osv – ~30 % effektiv laddtid. När planen räknades om mid-slot (möjligt sedan Bug 16) filtrerade slot-filtret bort den aktiva sloten (slottar vars **start** passerats droppades), `plan.start` hoppade 15 min framåt och "Outside plan window"-logiken stoppade laddningen. 10 minuter senare auto-startade nästa slot och loopen upprepades hela natten.

**Fix:** Slot-filtret i `plan_cheapest_window()` filtrerar nu på slot-**slut** i stället för slot-start – sloten som innehåller `now` behålls i planen.

**Känd accepterad bieffekt:** Den aktiva sloten energiräknas som hel slot (max ~0,7 kWh överskattning, självkorrigerande vid nästa slot-gräns).

| Fil | Ändring |
|-----|---------|
| `charge_planner.py` | Slot-filter: `t_utc < now_utc` → `t_utc + interval_duration <= now_utc` |
| `tests/test_charge_planner_bug22.py` | Nytt fristående regressionstest (6 fall, körs med `python3`, ingen HA-installation krävs) |

---

## 2026-06-06: Feature 2 – Klickbar dashboard-URL i push-notiser

Ny valfri inställning **Dashboard URL** under notis-konfigurationen. När den fylls i öppnas den angivna URL:en när användaren klickar på en push-notis (inte på en action-knapp). Lämnas fältet tomt är beteendet oförändrat.

iOS och Android hanterar fältet olika i HA Companion-appen, så båda nycklarna injiceras alltid i `data`-blocket:
- `data.url` – iOS
- `data.clickAction` – Android

Påverkar samtliga notiser: kabel-inkopplad, start, stopp, frånkopplad och det actionable dag/natt-erbjudandet. URL-värdet kan ändras via Options-flödet utan omstart (uppdateras via `_async_update_listener`).

| Fil | Ändring |
|-----|---------|
| `const.py` | Ny konstant `CONF_NOTIFY_DASHBOARD_URL` |
| `notifier.py` | `ChargerNotifier.__init__` tar emot `dashboard_url`; `_send()`, `on_cable_connected()` och `on_day_charging_chosen()` injicerar `url`/`clickAction` i `data`-blocket |
| `__init__.py` | Skickar `dashboard_url` vid instansiering; `_async_update_listener` uppdaterar fältet live vid options-ändring |
| `config_flow.py` | Nytt textfält i `async_step_initial_notify` (setup) och `async_step_edit_notify` (options) |
| `strings.json`, `sv.json`, `translations/sv.json` | Etikett "Dashboard URL (öppnas vid notisklick)" |

---

## 2026-05-30: Bug 21 – Dag/natt-notis skickas för ofta och kan visa passerade tider

**Problem:** Upprepade actionable notiser ("Dagladdning är billigare") skickades under dagen utan att kabeln var inkopplad (2026-05-30: 10:02, 10:32, 11:49, 12:50, 17:06). 17:06-notisen visade fönster 09:45–16:15 – en tid som redan passerat.

**Rotorsak:** Tröskeln för plan-shift-notis var 15 min (`> 900` sek), så naturlig drift av plan-starten under dagen triggade nya notiser. Det fanns heller ingen guard mot att visa notiser med plan-start i förflutet, och `_day_charging_dismissed`-flaggan saknade tidsbaserad nollställning.

**Fix:**
- Höjt plan-shift-tröskeln från 900 s till 7200 s (2 h)
- Lade in guard som undertrycker notisen när `plan_start_local <= now_local` (debug-loggar "Dag-notis undertryckt")
- Lade till `_day_charging_dismissed_until` som håller flaggan satt tills nästa lokala midnatt; återställs automatiskt i början av `_update_charge_plan()` när tiden passerats

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_charge_plan()` | Tröskel 900 → 7200 s; ny guard på `plan_start_local <= now_local`; nollställning av dismissed-flagga när `now >= dismissed_until` |
| `__init__.py` | `_handle_notification_action()` | Vid `NOTIFY_ACTION_DISMISS`: sätt `_day_charging_dismissed_until` till nästa lokala midnatt |
| `__init__.py` | `OCPPCoordinator.__init__()` | Nytt fält `_day_charging_dismissed_until: datetime \| None` |
| `__init__.py` | Cable→Available-reset | Nollställ även `_day_charging_dismissed_until` |

---

## 2026-05-17: Bug 20 – Byte till Immediate startar inte laddningen automatiskt

**Problem:** Vid byte till Immediate-läge medan kabeln är inkopplad (`Preparing` / `SuspendedEVSE`) hände ingenting. Användaren behövde trycka Start-knappen manuellt efteråt.

**Fix:** Ny hjälpmetod `async_start_if_ready()` i koordinatorn som anropar `async_start_charging()` bara om chargern är i ett startbart läge (`Preparing`/`SuspendedEVSE`) och inte redan laddar. `async_select_option()` för läget anropar den när användaren väljer Immediate.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | +1 metod `async_start_if_ready()` (nära `async_start_charging`) med guard på `connector_status` + `charging` |
| `select.py` | Importera `CHARGE_MODE_IMMEDIATE`; `async_select_option()` på charge-mode-select anropar `async_start_if_ready()` efter `set_charge_mode()` om option == Immediate |

---

## 2026-05-17: Bug 19 – Goal-reached check stoppade laddningen trots manual override

**Problem:** Vid Immediate-läge / manuell start stoppades laddningen ändå när `plan.energy_kwh` nåddes. Två window-check-grenar i `_update_smart_charging()` respekterade `_manual_start_requested`, men den tredje (goal-reached-blocket) gjorde det inte → upprepade RemoteStop utan notis under dagen.

`plan.energy_kwh` är en planerings-artefakt, inte ett användarsatt mål. När användaren explicit valt Immediate ska den respekteras även här.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_smart_charging()` | Lade till `if self._manual_start_requested: return` i goal-reached-blocket innan `_guarded_remote_stop()` |

---

## 2026-05-16: Feature 1 – Deadline Override Switch (kontextmedveten helg/semester)

Ny switch som flippar veckodag/helg-defaulten för 06:00-deadlinen:

| Dag | Switch AV | Switch PÅ |
|-----|-----------|-----------|
| Mån–fre | 06:00 (normalt) | Ingen deadline (semester) |
| Lör–sön | Ingen deadline (normalt) | 06:00 (tidig avfärd) |

"Ingen deadline" sätter deadline till sista tillgängliga prisintervallets sluttid → planneraren får använda hela prisdatat.

| Fil | Förändring |
|-----|-----------|
| `const.py` | +1 konstant `SWITCH_DEADLINE_OVERRIDE` |
| `switch.py` | +1 klass `DeadlineOverrideSwitch` (ärver `CoordinatorEntity, SwitchEntity`), registrerad i `async_add_entities`. Toggle bypassar `_last_plan_update`-throttle + anropar `_update_charge_plan()` direkt för omedelbar effekt. |
| `__init__.py` | +1 instansvariabel `deadline_override: bool = False`, deadline-blocket i `_update_charge_plan()` ersatt med anrop till ny `_compute_deadline(now_local, local_tz, all_prices)` |

Verifierad i drift (lördag, override OFF): plan utökas från `23:45–06:00` (1 window) till `01:30–17:30` (8 windows) — planneraren använder hela prisdatat istället för att stoppa vid 06:00.

---

## 2026-05-16: Bug 16, 17, 18 – Replanering under laddning, korrekt dag/natt-jämförelse, närvarobaserat erbjudande

### Bug 18 – Närvarobaserat dagladdningserbjudande (drift-import)
Funktionen var deployad i drift sedan tidigare men aldrig committad till git. Importerad verbatim från `192.168.1.97`.

| Fil | Tillägg |
|-----|---------|
| `const.py` | `DAY_OFFER_EARLIEST_HOUR`, `PRESENCE_ENTITIES`, `PRESENCE_HOME_STATES` (case-insensitive matchning för zonnamn) |
| `__init__.py` | `_someone_home()` med per-tracker debug-logg, `_day_offer_notified_date`-guard, ny `elif`-gren i `_update_charge_plan()` med strukturerad skip-loggning per orsak |

Trigger: kabel inkopplad + `allow_day_charging=False` + någon hemma efter 09:00 + dag-plan billigare än natt-plan. Max en gång per kalenderdag.

### Bug 17 – Dag/natt-jämförelse använde fel mått
**Problem:** Notisen "dag billigare än natt" hölls tillbaka när natt-planen var partiell (morgondagens priser ännu inte publicerade). `estimated_cost_sek` jämförde då 22 kWh natt mot 60 kWh dag — totalkostnaden i SEK blev artificiellt lägre för natt-planen även när medelpriset per kWh var högre.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_charge_plan()` (Bug 3-gren) | Byt jämförelsen till `avg_price_ore_kwh`, uppdatera skip-logg till öre/kWh |
| `__init__.py` | `_update_charge_plan()` (Bug 18-gren) | Samma fix i den närvarobaserade elif-grenen + info-loggen "Hemma efter..." |

### Bug 16 – Plan uppdateras inte under aktiv laddning
**Problem:** `_update_charge_plan()` var gated på `if not charging:` i `_async_update_data()`. När morgondagens priser anländer ~13:00 mitt under en pågående session ignorerades de tills laddningen avslutades. Bilen kunde då ladda i ett dyrt fönster trots billigare timmar i den uppdaterade prisdatan.

Rotorsak: guarden var felplacerad — planeringen skriver bara till `self.charge_plan`. Pingpong-skyddet (RemoteStart/Stop-oscillation) hör hemma i `_update_smart_charging()` där det redan finns (`_last_remote_start`, 5-minuters block).

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_async_update_data()` | Tagit bort `if not charging:`-villkoret runt `_update_charge_plan()` |

---

## 2026-05-15: Bug 15 – Stale fasvärden efter laddningsstopp

### Fix 15 – Per-fas ström nollställs vid varje MeterValues-anrop
**Problem:** Efter RemoteStop visade `Charging Current`-sensorn felaktigt ~12 A trots att `charging=False` och `power=0 W`. Värdet kvarstod tills kabeln drogs ur. Rotorsak: `_current_l1/l2/l3` (instansvariabler i `OCPPChargerClient`) sätts vid varje MeterValues med fasdata men nollställs aldrig. När en periodisk `connectorId=0`-avläsning utan fasström anlände efter stopp, läste fasaggregeringen kvarvarande gamla värden.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `ocpp_client.py` | `_parse_meter_values()` | Nollställer `_current_l1/l2/l3 = None` i början av varje anrop, så stala värden aldrig återanvänds |

---

## 2026-03-30: Fordonsväxling, planering och GitHub-release

### Fordonsval i push-notis
Inkopplad-notisen (`on_cable_connected`) är nu åtgärdbar med en knapp per fordon om fler än ett fordon är konfigurerat. Knappen för aktivt fordon markeras med ✓. Tryck → `set_active_vehicle()` + omplanering direkt.

| Fil | Ändring |
|-----|---------|
| `const.py` | `NOTIFY_ACTION_SELECT_VEHICLE = "ocpp_select_vehicle_"` (prefix + index) |
| `notifier.py` | `on_cable_connected()` tar emot `vehicles`-lista och bygger `actions`-payload |
| `__init__.py` | Anropas med `vehicles=self._vehicles`; action-handler hanterar `ocpp_select_vehicle_X` |

### Aktivt fordon persisteras (Fix-A)
`active_vehicle_name` sparas i HA Storage och återställs vid omstart.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_save_state()` skriver `active_vehicle_name`; `_load_state()` matchar mot `_vehicles` |

### Fix 11 – Vehicle re-detection + session_total_kwh-läcka vid delstopp
**Problem:** Efter RemoteStop (plan slut, kabel kvar) körde `_check_vehicle_auto_detect()` igen eftersom Garo skickar `Preparing`. Det bytte fordon, och `_session_total_kwh` (19,78 kWh) lades ihop med det nya fordonets energibehov → `energy_needed = 0` → ingen ny plan.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_check_vehicle_auto_detect()` | Triggar bara vid `Available → Preparing`, inte `Charging → Preparing` |
| `__init__.py` | `set_active_vehicle()` | Nollställer `_session_total_kwh` vid fordonsbyte |

### Fix 12 – Inaktuellt energy_kwh från föregående session
**Problem:** `state.energy_kwh` sparas i HA Storage. Efter omstart var värdet kvar från föregående session (19,78 kWh) och subtraherades från energibehovet → `energy_needed = 0` → plan `08:20–08:20`.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `_update_charge_plan()` | `active_tx_energy = energy_kwh if transaction_id is not None else 0.0` |

### Fix 13 – Kontrollförändringar triggade inte omedelbar omplanering
**Problem:** `set_target_soc`, `set_target_kwh`, `set_allow_day_charging`, `set_charge_mode` och `set_active_vehicle` saknade `_update_charge_plan()`-anrop. Ändringen fick effekt först vid nästa 10s-cykel.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | Alla fem setter-metoder | Lade till `_update_charge_plan()` + `async_set_updated_data()` |
| `__init__.py` | `set_active_vehicle()` | Lade även till `_update_soc_from_ha()` innan omplanering |

### Per-fordon max laddström
Nytt fält `max_current_a` (default 0 = använd schemat) i fordonskonfigurationen. Planeraren använder `min(schema_ström, fordonets_max)` om värdet är > 0.

| Fil | Ändring |
|-----|---------|
| `const.py` | `VEHICLE_MAX_CURRENT_A = "max_current_a"` |
| `config_flow.py` | Nytt NumberSelector-fält i `_vehicle_schema()` och alla tre dict-byggen |
| `__init__.py` | `effective_current = min(schedule_current, vehicle_max_a)` i `_update_charge_plan()` |

### Dashboard
`dashboard.yaml` skapad med 6 sektioner: status, fordon/SOC, aktiv session, styrning, schema, laddplan (conditional Smart-läge).

---

## 2026-03-20: Multi-session stabilitet (Fix 7–10)

### Fix 7 – Planeraren räknar om från noll efter varje delstopp

**Problem:** Nattladdningen skapade 5–6 separata laddningssessioner istället för en sammanhängande. Planeraren räknade om planen efter varje 30-min delstopp eftersom `state.energy_kwh` nollställdes vid varje ny OCPP-transaktion.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nytt fält `_session_total_kwh: float`. |
| `__init__.py` | `_check_notify_events()` | Nollställs vid Available, ackumulerar föregående delsessions energi vid Preparing. |
| `__init__.py` | `_update_charge_plan()` | `already_charged_kwh = _session_total_kwh + state.energy_kwh` subtraheras från `energy_needed`. |

### Fix 8 – Dubbel RemoteStop inom sekunder

**Problem:** Två update-cykler triggade ibland `remote_stop_transaction()` inom 1–2 sekunder av varandra.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nytt fält `_last_remote_stop: datetime | None`. |
| `__init__.py` | `_update_smart_charging()` | Guard: hoppar över RemoteStop om < 15s sedan senaste. |

### Fix 9 – Upprepad "Inkopplad"-notis under natt-cykeln

**Problem:** "Inkopplad"-notisen skickades vid varje OCPP-delsession (Preparing) under natten, inte bara en gång per kabelinkoppling.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nytt fält `_cable_session_notified_connect: bool`. |
| `__init__.py` | `_check_notify_events()` | Guard mot bool-flagga istället för session_id-jämförelse. Nollställs vid Available. |

### Fix 10 – Periodisk SOC-omläsning de första 30 minuterna efter inkoppling

**Problem:** Bilappen uppdaterade SOC med fördröjning efter körning. Planeraren beräknade `energy_needed` från gammal SOC som råkade vara i HA vid inkoppling.

| Fil | Funktion | Ändring |
|-----|----------|---------|
| `__init__.py` | `__init__()` | Nya fält `_cable_connect_time`, `_soc_at_connect`, `_soc_reread_done`. |
| `__init__.py` | `_check_soc_reread()` | Ny metod. Körs varje 10s i upp till 30 min efter Preparing. Vid ΔSoC ≥ 5 pp → omberäknar planen. |
| `__init__.py` | `_async_update_data()` | Anropar `_check_soc_reread()` efter SOC-uppdatering. |

---

## 2026-03-18: Kabelsession-modell och SuspendedEV-hantering

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
