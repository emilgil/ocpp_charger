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

# OBS: *.py tar INTE med services.yaml – kopiera den explicit när den ändrats
scp custom_components/ocpp_charger/services.yaml root@192.168.1.97:/config/custom_components/ocpp_charger/

# Starta om HA
ssh root@192.168.1.97 "ha core restart"

# Följ loggen (HA-loggen har bara WARNING/ERROR från komponenten – allt finns i debugfilen nedan)
ssh root@192.168.1.97 "grep -i ocpp_charger /config/home-assistant.log | tail -30"

# Debug-logg (alla nivåer, ny fil varje dygn, 14 dygn; äldre dygn: ocpp_charger_debug.log.YYYY-MM-DD)
ssh root@192.168.1.97 "tail -f /config/ocpp_charger_debug.log"
```

**Deploy-varning:** `*.py` kopierar bara Python-filer. `services.yaml`, `strings.json`, `sv.json`, `manifest.json` och
`translations/` följer inte med – kopiera dem explicit när de ändrats (annars fungerar nya tjänster men saknar fält i
Developer Tools). Ändringar i `.py` och `services.yaml` kräver full HA-omstart. En ny `.py`-modul som `__init__.py` importerar
följer med globben, men glöms den vid selektiv kopiering laddas hela integrationen inte.

## Filstruktur
```
custom_components/ocpp_charger/
  __init__.py          – OCPPCoordinator, smart charging, kostnad, notiser, auto-start
  ocpp_client.py       – WebSocket OCPP 1.6-server, ChargerState
  config_flow.py       – Setup flow (4 steg) + options flow
  const.py             – Alla konstanter
  sensor.py            – 23 sensorer
  binary_sensor.py     – 4 binära sensorer
  number.py            – 6 number-entiteter
  select.py            – 3 select-entiteter
  switch.py            – 3 switchar
  button.py            – 2 knappar
  vehicle_detection.py – Auto-identifiering av fordon
  current_schedule.py  – Dag/natt-schema
  smart_charge.py      – Prisbeslut (fallback när ingen plan finns) + estimate_completion_time (ETA; tester i tests/test_smart_charge_bug36.py)
  charge_planner.py    – Optimal laddplanering baserat på spotpriser + is_next_day_shift (Bug 40, stdlib-only, testbar; tester i tests/test_bug40.py) + plan_immediate_window/pick_immediate_power_kw/immediate_window_wanted (Bug 42; tester i tests/test_bug42.py)
  charge_windows.py    – Feature 3: bygger laddplanens slots (stdlib-only, testbar)
  deadline.py          – Feature 4/6: parse_hhmm + compute_deadline + helper_state_to_hhmm (stdlib-only, testbar)
  soc_estimate.py      – Bug 29: estimate_soc från start-SOC + levererad energi; golvar mot färsk rapporterad SoC (Bug 38) (stdlib-only, testbar)
  charging_start.py    – Bug 43: serialize_charging_start/restore_charging_start – persisterar _charging_started_at över omstart (stdlib-only, testbar; tester i tests/test_bug43.py)
  cable_flag.py        – Bug 44: restore_cable_was_available – återställer `_cable_was_available` ur Store efter omstart (stdlib-only, testbar; tester i tests/test_bug44.py)
  clear_profile.py     – Feature 8: opt_int/parse_confirm/parse_clear_request/refused_result – vakt och tolkning av clear_charging_profile-indata (stdlib-only, testbar; tester i tests/test_feature8.py)
  logging_setup.py     – Feature 9: loggkonfiguration – HaForwardHandler (WARNING+ till HA-loggen), dygnsroterande fil (14 dygn), valfri syslog UDP; apply_logging/remove_logging (stdlib-only, testbar; tester i tests/test_logging_setup.py, kopplingen i tests/test_feature9_wiring.py)
  price_cap.py         – Feature 5: select_price_cap_slots – slots ≤ pristak (stdlib-only, testbar)
  notifier.py          – Push-notiser
  rest_client.py       – Async HTTP-klient
  manifest.json
  services.yaml
```

## Arkitektur – laddningsstyrning (prioritetsordning)
1. **Charge mode = Immediate** → ladda alltid. Planen/Charge Windows visar ett enda block
   sessionsstart → beräknad sluttid (Bug 42), inte Smart-planens billigaste luckor; den styr inte start/stopp.
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
- `set_charge_mode()` (rensar Immediate-fönstret vid byte bort från Immediate, Bug 42)
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

### Fordonsbyte + Garo-reset-vakt (Bug 41)
`set_active_vehicle()` nollställer `_session_total_kwh` vid fordonsbyte men rör inte
`self.ocpp.state.energy_kwh` (ägs av `ocpp_client.py`, rensas inte vid byte). Om nästa
`Preparing` efter ett byte klassas som Garo-reset (rad ~1656, ingen genuin `Available`
emellan – t.ex. om kabeln aldrig kopplades ur mellan fordonen) ackumulerade den grenen
tidigare blint in det gamla fordonets kvarvarande `state.energy_kwh` ovanpå den nyss
nollställda `_session_total_kwh` – samma kryssningsbugg Bug 33 löste i genuin-anslutnings-
grenen, fast i den andra grenen. `_vehicle_switch_pending_reset` (satt i
`set_active_vehicle()` vid namnbyte) gör att **vilken gren som helst** som ser nästa
`Preparing` nollställer `_session_total_kwh` istället för att ackumulera, och konsumerar
flaggan. Utan fixen gav ackumulerad stale-energi ett SOC-estimat 20+ procentenheter för
högt → `_charging_goal_reached()` sa "redan klar" → auto-start undertrycktes och
Charge Windows-sensorn visade 0 fönster trots att fordonet låg långt under målet.
Rör varken `_cable_was_available`/`Available`-hanteringen (Bug 13A/38, oförändrad) eller
Bug 33/Fix 7:s legitima ackumulering inom samma bils session.

### Dag-till-nästa-dag-hopp-vakt (Bug 40)
Kompletterar Bug 28 för fasen **innan** en session startat (kabel inkopplad, väntar på fönstret). När morgondagens priser publiceras utökar `compute_deadline()` (helg/`allow_day_charging`-grenen) horisonten ett helt dygn, och `plan_cheapest_window()` kan då skjuta upp dagens redan valda fönster ett dygn för en försumbar besparing – loggen visar bara `%H:%M` så det ser ut som att fönstret försvann. `is_next_day_shift(prev_plan, new_plan, now_local, local_tz, *, cable_connected)` (ren funktion i `charge_planner.py`, testad i `tests/test_bug40.py`) upptäcker hoppet: förra planen börjar **idag**, båda feasible, kabel inkopplad, och nya fönstrets start-dag ligger **efter förra planens *slut*-dag** (slut- inte start-dag → en vardagsnatt som glider "22:00 idag"→"02:00 imorgon" före samma 06:00-deadline räknas inte). Vid hopp håller `_update_charge_plan()` kvar `prev_plan` och skickar `on_next_day_shift_choice`-notisen (knappar 🔌 Ladda idag / ⏳ Vänta till imorgon) **en gång**. `_next_day_shift_hold` (sticky för kalenderdygnet, speglar `_day_charging_dismissed`-mönstret) håller planen utan att spamma tills användaren svarar / hoppet upphör / kabel ur / midnatt. `KEEP_TODAY` låser hållet; `WAIT_TOMORROW` byter in `_next_day_shift_candidate` direkt om ingen laddning pågår, annars sätts `_next_day_shift_accepted` och den nya planen tas i bruk först när den aktiva sessionen avslutats naturligt (mål / kabel ur / prishål) – en notisknapp får aldrig avbryta pågående laddning (Bug 28). Alla tre flaggor nollställs i `Available`-blocket. Pristaksläget (Feature 5) returnerar före vakten och berörs inte.

### Immediate-fönster (Bug 42)
`_update_charge_plan()` anropade tidigare alltid `plan_cheapest_window()`, så i Immediate visade
`Laddfönster`-grafen/`charge_windows`-sensorn Smart-planens billigaste luckor fast bilen laddade direkt
(styrningen ignorerar planen i Immediate – "Immediate läge aktivt"). Nu byggs i stället **ett** block
`[sessionens starttid, nu + återstående energi / effekt]` av `plan_immediate_window()` (ren, stdlib-only,
`tests/test_bug42.py`). Hooken ligger i `_update_charge_plan()` direkt efter `energy_needed`/`power_kw`
och returnerar före dag/natt-notiserna och närvaro-erbjudandet (meningslösa i Immediate).
- **Effekt:** `pick_immediate_power_kw()` – uppmätt `power_w/1000` när `charging` och `power_w >= 1000`
  (skydd mot ramp-up), annars schema/fordonsgräns (`power_kw` från `_update_charge_plan()`).
- **Vänsterkant:** `_charging_started_at` (Bug 34), före laddstart "nu". Klampas till högst `now`.
- **Semantik (Bug 29):** `energy_kwh`/`estimated_cost_sek`/`duration_minutes`/`intervals` gäller
  *återstående* laddning från nu; bara `start`/`active_intervals` sträcker sig bakåt. `duration_minutes` måste
  vara återstående tid eftersom `_update_eta()` återanvänder den. Slot-`time` klipps till `max(slot_start, now)`
  så `build_charge_windows()` (`iv_start <= time < iv_end`) behåller sloten som innehåller `now` (jfr Bug 22).
- **När visas fönstret:** `immediate_window_wanted()` – kabel inkopplad, `connector_status != SuspendedEV`,
  målet inte nått (`_charging_goal_reached()`), `energy_needed > 0` (gäller även `Preparing`/`SuspendedEVSE`).
  Annars `_clear_immediate_plan()` (nollar plan, `_alt_plan` och alla `_charge_windows*`-fält – `_rebuild_charge_windows()`
  rensar aldrig själv för `None`/infeasible plan).
- **Lägesmedvetna tidiga returer i `_update_charge_plan()`:** 300 s-RemoteStart-frysen hoppas över i Immediate
  (planen styr inte start/stopp → ingen pingpong-risk), "mål redan nått" rensar fönstret, throttle 60 s (annars 300 s).
- **`set_charge_mode()`** rensar Immediate-fönstret *före* `_update_charge_plan()` när man lämnar Immediate.
- `_alt_plan = None` i Immediate (Planner Savings skulle annars jämföra mot en gammal Smart-alternativplan).
- **Rör inte** `_update_smart_charging()`/styrlogiken, kortet, Bug 28-frysningen eller pristaksläget (Feature 5).
- **Kända begränsningar:** första planen efter omstart körs före `_load_state()` och kan vara för lång i ~2 min;
  `actual_energy_kwh` fylls inte i för Immediate-block. (`_charging_started_at` persisteras sedan Bug 43 – se nedan.)

### Laddstartstid över omstart (Bug 43)
`_charging_started_at` (Bug 34) var bara in-memory: en HA-omstart mitt i laddning nollade den, så start-grenen i
`_check_notify_events()` skrev över den med "nu" (Laddfönster-blockets vänsterkant och `Planned Charge Start` hoppade
till omstartstiden) och skickade om "Laddning startad"-pushen. Nu persisteras den via de rena hjälparna i
`charging_start.py` (stdlib-only, `tests/test_bug43.py`).
- **Spara/läs:** `_save_state()` skriver `charging_started_at` (`serialize_charging_start`); `_load_state()` återställer
  via `restore_charging_start(raw, now, cable_connected=...)` **före** `set_active_vehicle()` (som kör en plan mitt i
  återställningen, så även den första planen får rätt vänsterkant). Värdet nollas redan vid `Available` (Bug 34) och då sparas `None`.
- **Avvisas (→ som förut, grenen sätter "nu"):** saknat/skräpvärde, naiv tid (tvetydig), kabeln urkopplad vid sparandet,
  tid i framtiden, äldre än 24 h (`CHARGING_START_MAX_AGE`). Kastar aldrig.
- **Start-grenen:** `_charging_started_at` och `_cable_session_start_notified` sätts alltid tillsammans, så ett redan satt
  `_charging_started_at` betyder "start-notisen gick före omstarten": tiden skrivs inte över och `on_charging_started`
  skickas inte igen. Övrig bokföring i grenen (`_cable_session_start_notified`, `_charging_seen_this_session`,
  `_last_transaction_start`, `_last_cost_energy_kwh`) är oförändrad, så stopp-notis, grace period och kostnad beter sig som förut.
- **Kända begränsningar:** första omstarten med den nya koden har inget sparat värde (gamla koden sparade ingen nyckel)
  → beter sig som förut; deploya helst när ingen laddning pågår. Med `notify_on_start` avstängt sätts
  `_charging_started_at` aldrig (Bug 34-beteende, oförändrat).

### Omstartsvägen: Bug 44 + 45 + 46
Incidenten 21/9 (Session Energy nollställdes inte, stod kvar på förra kabelsessionens 51,25 kWh) var tre lager på samma väg – omstart/omladdning
av integrationen. De hänger ihop och ska förstås tillsammans:
1. **Bug 45 (status okänd):** Garo återansluter 22–31 s efter serverstart och skickar inte om sin status. `connector_status` förblev `Unknown`
   i över ett dygn, så ingen `Available` observerades.
2. **Bug 44 (flaggan glömd):** `_cable_was_available` armeras bara av en äkta `Available` och låg bara i minnet → `False` efter varje omladdning →
   riktig inkoppling klassades som Garo-reset, kabelsessionens ackumulatorer nollställdes aldrig.
3. **Bug 46 (falskt fordonsbyte):** en färsk koordinator startar på `vehicles[0]`; `_load_state()` återställde sparat fordon via `set_active_vehicle()` →
   "byte" vid varje omstart → Bug 41-flaggan armerad → den felklassade `Preparing` nollade `_session_total_kwh`.

- **Bug 44:** `_save_state()` skriver `cable_was_available`; `_load_state()` återställer via `restore_cable_was_available(saved, cable_connected=, current=)`
  **efter** att `cable_connected` lästs in. Sparad bool gäller; gammal Store utan nyckeln → `not cable_connected`; en redan satt live-flagga (`current`)
  skrivs inte över. Kan **inte** härledas ur `cable_connected`: den är False även för Faulted/Unavailable med kabeln i (`ocpp_client.py`).
- **Bug 45 Del 1:** `_request_status_refresh(reason)` skickar TriggerMessage(StatusNotification) vid **varje** (åter)anslutning efter att Store lästs: kantdetektor
  (`_charger_was_connected`, `_state_loaded`) i `_on_charger_state_update_async()` + startup-timern `_delayed_soc_refresh` (+10 s, för en laddare som redan är
  ansluten). Före Store-laddningen skickas ingen (`_load_state()` skulle skriva över en färsk status). Retry max `STATUS_TRIGGER_MAX_ATTEMPTS` (3) med
  `STATUS_TRIGGER_RETRY_SECONDS` (10) emellan, så länge status är `Unknown`/`""` och laddaren ansluten. `_state_loaded` sätts i `finally` runt `_load_state()`.
- **Bug 45 Del 2:** i Garo-reset-grenen (`Preparing`, ingen föregående `Available`) hoppas ackumuleringen av `state.energy_kwh` över när
  `_last_connector_status_notify` är `""`/`"Unknown"`: trigger-svaret är första kända status, ingen statusändring har skett, och energin är redan
  inräknad i den återställda `_session_total_kwh` (Bug 30). `elif` ligger **efter** Bug 41-grenen, så Del 2 **förutsätter Bug 46**.
- **Bug 45 Del 3:** `_handle_charger()` (`ocpp_client.py`) städar i `finally` bara om `self._ws is websocket`; annars loggas `Gammal anslutning stängd, nyare aktiv`
  och state rörs inte. Från kodgranskning, aldrig observerad i loggarna.
- **Bug 46:** `set_active_vehicle(vehicle, *, restore=False)`. `_load_state()` anropar med `restore=True` (hoppar över bytesblocket: `_session_total_kwh`-nollning och
  `_vehicle_switch_pending_reset`); loggar `[Bug46] Återställer fordon`. Riktiga byten (notisval, auto-detect, select) är oförändrade – Bug 41 gäller kvar.
- **Samspel:** Bug 44 är skyddsnätet när Garo aldrig svarar på triggern; trigger-svarets `Available` ger ingen falsk stopp-push (Bug 12-vakten i
  `_send_stop_notification` avbryter när kabeln är ur).
- **Tester** (`tests/coordinator_harness.py` = riktig `OCPPCoordinator` med riktig `__init__`, MagicMock-hass; bara Store, hass-tjänster och tid fejkade; körs med
  rot-venv `/mnt/c/temp/github/claude/venv/bin/python`, hoppas över utan HA): `test_bug44.py`, `test_bug45.py`, `test_bug46.py` och `test_restart_path.py`
  (end-to-end: sent ansluten laddare, laddare som aldrig svarar, omstart mitt i en pausad session).
- **Live-verifierat 2026-09-21:** migreringen (`[Bug44] Återställde cable_was_available=False (cable_connected=True)`), `[Bug46]` utan `[Vehicle] Switching`, och
  `[Bug45] TriggerMessage försök 1/3 (orsak=återanslutning)` → `Accepted` → `Preparing` → `[Bug45] Preparing är första kända status efter omstart`.
- **Kända begränsningar / ej live-verifierat:** Bug 44:s huvudfall (kabel ur → omladdning → inkoppling ska nollställa Session Energy); omstart **mitt i
  laddning** (trigger-svaret `Charging` förlitar sig på Bug 43 mot dubbel start-push); Bug 45 Del 3. Är HA nere både när kabeln dras ur och när den kopplas
  in igen är den sparade flaggan False och inkopplingen klassas fortfarande som Garo-reset (kvarvarande begränsning, Bug 44).

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
STATUS_TRIGGER_MAX_ATTEMPTS         = 3      # Bug 45: TriggerMessage-försök per (åter)anslutning
STATUS_TRIGGER_RETRY_SECONDS        = 10     # Bug 45: avstånd mellan försöken
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
| Skickar INTE om StartTransaction/StatusNotification vid reconnect | `transaction_id` läses från MeterValues-payload. `TriggerMessage StatusNotification` skickas vid varje (åter)anslutning efter att Store lästs (Bug 45; Garo återansluter 22–31 s efter serverstart, alltså oftast efter +10 s-timern) |
| Per-fas ström (L1/L2/L3), inget totalt faslöst värde | `current_a = mean(L1, L2, L3)` |
| Laddprofil begränsade transaktionerna till 13 A (`ChargePointMaxProfile`, hittad och rensad 2026-09-19) | Syntes som `limit: 13` i `get_composite_schedule` under en transaktion och `Current.Offered` Outlet = 13 A (Body 16 A); utan transaktion visade boxen 16 A. Rensad med `clear_charging_profile purpose: ChargePointMaxProfile`. Ursprunget är okänt (integrationens fallback loggade ingen användning 14–19 sep). Kommer 13 A tillbaka: sök `ChargePointMaxProfile applied` / `SetChargingProfile` i debugloggen |

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
_vehicle_switch_pending_reset: bool       # Bug 41: nästa Preparing (oavsett gren) ska nollställa _session_total_kwh, inte ackumulera stale state.energy_kwh
_next_day_shift_hold: bool                # Bug 40: håller dagens plan vid dag→nästa-dag-hopp (sticky för kalenderdygnet, nollställs vid Available)
_next_day_shift_candidate: ChargePlan | None  # Bug 40: färskaste billigare senare-dag-planen (för "Vänta till imorgon")
_next_day_shift_accepted: bool            # Bug 40: användaren valde "Vänta till imorgon" under laddning → planen tas i bruk först när sessionen avslutats
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
_cable_was_available: bool                # Bug 13A/38: armeras ENDAST av äkta Available; init False (Bug 38) så omstart mitt i kabelsession inte fyrar falsk genuin-inkoppling vid nästa Preparing; Bug 44: persisteras i Store och återställs i _load_state()
_cable_connect_time: datetime | None      # Fix 10: tid för kabelinkoppling
_state_loaded: bool                       # Bug 45: _load_state() klar (sätts i finally i _delayed_soc_refresh); före det skickas ingen TriggerMessage vid anslutning
_charger_was_connected: bool              # Bug 45: kantdetektor för laddarens WebSocket-anslutning (False→True → _request_status_refresh)
_soc_reread_done: bool                    # Fix 10: SOC omläst inom 30 min
_charging_started_at: datetime | None     # Bug 34: fryst faktisk laddstartstid för PlannedChargeStartSensor (None innan start/efter urkoppling); Bug 43: persisteras i Store och återställs vid omstart
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
| Estimated Completion | Timestamp när laddningen är klar. Under aktiv laddning: fordonets `battery_capacity_kwh` + `DEFAULT_CHARGE_EFFICIENCY` + Bug 29-SOC-estimat (Bug 36) – matchar planerarens `energy_needed`-formel |
| Estimated Charge Time Remaining | Återstående tid, format "2 h 15 min" (samma ETA-källa, Bug 36) |
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
| Price Cap Status | Diagnostisk: antal råslots ≤ pristaket cappat vid återstående SoC-behov (`_capped_raw_slots()`, Bug 35/35b – `state` == `slots_count`) + expected_kwh/expected_cost_sek + `slots`-lista (time/price_ore_kwh/cost_sek) (Feature 5) |

### Binära sensorer (4 st)
| Sensor | Beskrivning |
|--------|-------------|
| Cable Connected | Kabel inkopplad |
| Charging | Aktivt laddande |
| Charger Connected | OCPP WebSocket ansluten |
| Price Cap Active | Feature 7: pristaksläget konfigurerat (`price_cap_ore_kwh > 0`); namn "Price Cap Active" → entity_id `..._price_cap_active`. Indikerar konfiguration, ej om slots kvalificerar just nu |

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

### Dag-till-nästa-dag-hopp-notis (Bug 40)
`on_next_day_shift_choice` skickas när `is_next_day_shift()` upptäcker att en omräknad plan tyst
skjuter upp dagens fönster ett helt dygn (se "Dag-till-nästa-dag-hopp-vakt"). Tag
`ocpp_next_day_shift`, knappar `ocpp_keep_today_plan` (🔌 Ladda idag) / `ocpp_wait_tomorrow_plan`
(⏳ Vänta till imorgon). Skickas **en gång** per hopp (styrs av `_next_day_shift_hold`, inte
session_id-dedup). `dismiss_next_day_shift_notification()` rensar den från telefonen när
användaren svarat, hoppet upphört eller kabeln dragits ur.

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
| `ocpp_charger.get_composite_schedule` | Läser det sammansatta laddschemat boxen tillämpar (OCPP `GetCompositeSchedule`, skrivskyddad). Fält: `connector_id` (0/1), `duration` (s), `charging_rate_unit` (A/W). Svar på event `ocpp_charger_ocpp_response` + debug-logg (Feature 8) |
| `ocpp_charger.clear_charging_profile` | Rensar laddprofiler i boxen (OCPP `ClearChargingProfile`). Filter: `profile_id`, `connector_id`, `purpose`, `stack_level`. Utan filter krävs `confirm_clear_all: true` (bara bool `true` eller strängarna true/yes/on räknas; "false"/"off" avvisas), annars `status: Refused` och inget skickas (Feature 8) |

### Laddprofil-tjänster (Feature 8)
Finns för att hitta och ta bort en laddprofil i boxen som begränsar varje transaktion (bakgrund: `Current.Offered`
Outlet = 13 A trots `GaroOwnerMaxCurrent` = 16 A; boxens logg visade `SC=13.0`). Båda går via `_send_call`, ändrar inte
`ChargerState` och svarar med event `ocpp_charger_ocpp_response` (`action` = `GetCompositeSchedule` / `ClearChargingProfile`);
`get_composite_schedule` loggar dessutom hela svaret i debug-loggen.
- **Läsa:** `charging_schedule.chargingSchedulePeriod[].limit` = 13 (A) bekräftar en profil som sätter 13 A; 16 eller högre →
  profilen är inte källan; `Rejected`/`NotSupported` → använd boxens webbsida (`/admin.html`). Läs UNDER en pågående
  transaktion: utan transaktion visade boxen 16 A även medan profilen fanns.
- **Rensa:** börja smalt (`purpose: TxDefaultProfile`, sedan `ChargePointMaxProfile`, sedan `TxProfile`; `Unknown` = inget
  matchade; ett felformat svar utan `status` visas också som `Unknown` – kolla först `GetCompositeSchedule`-dumpen i
  debug-loggen). Utan filter krävs `confirm_clear_all: true`, annars `status: Refused` och inget skickas. `0` är ett giltigt
  filtervärde (`connector_id: 0` = hela laddpunkten). Bara bool `true` eller strängarna `true`/`yes`/`on` räknas som
  bekräftelse (`"false"`, `"off"`, `1` avvisas). Vakten, `opt_int` och tolkningen av tjänstedata ligger i `clear_profile.py`
  (stdlib-only, `tests/test_feature8.py`); handlern i `__init__.py` delegerar dit, inte `OCPPClient`.
- **Effekt:** rensningen av `ChargePointMaxProfile` slog igenom direkt i den pågående transaktionen (schemat 16 A, laddeffekt
  8,7 → 10,7 kW). `set_charging_limit()` påverkas inte (den använder `GaroOwnerMaxCurrent`; `ChargePointMaxProfile` bara som
  fallback via `_apply_charge_point_max_profile`, profil-id 1).
- **Debugloggen:** sedan Feature 9 sätter koden själv DEBUG vid uppstart, så `/config/ocpp_charger_debug.log` fylls direkt efter
  varje omstart (ingen `logger.set_level` behövs; se "Loggning (Feature 9)").

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
behålls senaste slots (rensas ej) – `calculated_at` visar åldern. Undantag (Bug 42): i Immediate rensas
slots explicit via `_clear_immediate_plan()` när inget fönster ska visas (kabel ur, `SuspendedEV`, mål nått,
byte till annat läge).

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

**Bug 39:** `async_setup_entry` registrerar en `async_track_state_change_event`-lyssnare på
`INPUT_DATETIME_DEADLINE` (samma mönster som `set_price_cap()`) som bypassar plan-throttlen och
kör `_update_charge_plan()` + `async_set_updated_data()` direkt vid ändring. Tidigare lästes
helpern bara on-demand av `_get_manual_deadline_str()`, så en ändring i UI syntes i
Laddfönster-grafen/`charge_windows`-sensorn först vid nästa oberoende omplanering (periodisk
poll, upp till ~60s), asymmetriskt mot pristakets omedelbara uppdatering.

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

**Bug 35c:** `_update_price_cap_plan()` sätter `charge_plan.energy_kwh`/`estimated_cost_sek` till
**SoC-cappade** värden (slots ackumuleras kronologiskt tills återstående batteribehov
`(target_soc−current_soc)/100×capacity/eff` nås) – inte `result.total_kwh` (alla kvalificerande slots).
Tidigare visade `PlannedChargeEnergySensor`/`EstimatedChargeCostSensor` hela marknaden under taket
(t.ex. 38.64 kWh för ett behov på ~21 kWh). `intervals`-listan behåller **alla** slots (det körda
schemat); bara energi/kostnad cappas. Okänd SoC (`None`) eller `current_soc ≥ target_soc` → ingen
cappning (samma semantik som Bug 35b:s `_capped_raw_slots`). Helt-slot-ackumulering → kan överstiga
behovet med ≤1 slot.

## Persistens (Store)
`self._store` (HA Storage) sparar bl.a. `cable_connected`, `transaction_id`, `energy_kwh`,
`price_cap_ore_kwh` (Feature 5),
`allow_day_charging`/`day_charging_manual_override` (Bug 26),
`session_start_soc`/`session_total_kwh` (Bug 30)
`charging_started_at` (Bug 43) och `cable_was_available` (Bug 44) mellan omstarter.
- `_save_state()` anropas i varje `_async_update_data()`-cykel
- `_load_state()` anropas i `_delayed_soc_refresh()` (10s efter HA-start)
- **Bug 30:** `session_start_soc`/`session_total_kwh` återställs **efter** `set_active_vehicle()`
  i `_load_state()` (den nollställer dem). De håller SOC-estimatets baslinje i synk med dess
  energi över en omstart mitt i en laddning – annars dubbelräknas redan levererad energi och
  laddningen stoppar för tidigt ("Mål nått" vid fel SOC).
- **Bug 43:** `charging_started_at` återställs däremot **före** `set_active_vehicle()` i `_load_state()`, så att även
  den plan som körs mitt i återställningen får rätt vänsterkant (se "Laddstartstid över omstart (Bug 43)").
- **Bug 44:** `cable_was_available` återställs efter att `cable_connected` lästs in (migrering för en Store utan nyckeln: `not cable_connected`).
  **Bug 46:** sparat fordon återställs med `set_active_vehicle(match, restore=True)` – inget fordonsbyte. Se "Omstartsvägen: Bug 44 + 45 + 46".

## Loggning (Feature 9)
`logging_setup.py` (stdlib-only) äger all loggkonfiguration. `async_setup_entry()` anropar `apply_logging()` överst (via executor)
och `async_unload_entry()` `remove_logging()` efter `coordinator.async_stop()`. Komponentloggern `custom_components.ocpp_charger`
sätts till DEBUG via `orig_setLevel` (`_set_level()`) med `propagate = False`; tidigare nivå (också via `_set_level()`) och
propagate återställs vid unload.
- **Fil:** `hass.config.path(LOG_FILE_NAME)` = `/config/ocpp_charger_debug.log`. Alla nivåer, ny fil vid midnatt
  (`TimedRotatingFileHandler`, `when="midnight"`), 14 dygn sparas; roterade filer får datumsuffix
  (`ocpp_charger_debug.log.2026-09-18`). Skrivs av en egen tråd (`QueueHandler` → `QueueListener`), aldrig i HA:s event-loop.
- **HA-loggen** (`home-assistant.log`): bara WARNING och ERROR från komponenten – `HaForwardHandler` kringgår med avsikt HA:s
  per-logger-nivåer. Allt (INFO/DEBUG också) om options-valet `log_verbose_ha` ("Skicka allt till Home Assistants logg") är på.
  Filtrera som förut med `grep -i ocpp_charger`.
- **Syslog UDP (valfritt):** options → "📝 Edit logging settings" → värd, port (standard 1514), lägsta nivå (standard DEBUG). Tom värd = av.
  Facility `local0`, tag `ocpp_charger`, ingen avslutande NUL-byte. Värdnamnet slås upp en gång vid start (IP används sedan).
  Okänd värd eller sändningsfel ger EN warning och fäller aldrig setup; upprepade sändningsfel tystas tills sändningen fungerar igen
  (återställningen loggas som INFO i debugfilen).
  `syslog_host` har medvetet ingen `default=` i formuläret utan förifylls med `description={"suggested_value": ...}`: HA-frontend
  utelämnar tomma fält och voluptuous fyller i den gamla värden igen om fältet har `default=`, så en sparad värd kunde aldrig
  rensas (port, nivå och verbose-valet har `default=`).
- **Ändra inställningarna:** options-flowets `_save()` (vid ✅ Save and close) laddar om config entry → unload + setup tillämpar
  de nya värdena (laddaren återansluter, som vid alla options-ändringar).
- **`logger:`-blocket:** behövs inte och kan tas bort – koden sätter DEBUG själv via `orig_setLevel`, som går förbi HA:s
  logger-override (`HassLogger.setLevel()` är annars en no-op för loggers med override: `logger:` i configuration.yaml,
  `logger.set_level`, UI-knappen "Aktivera felsökning"). `logger.set_level` mot komponenten efter start gäller (HA använder samma
  `orig_setLevel`) och påverkar fil och syslog (de får det loggern släpper igenom).
- **Kända begränsningar:** UDP är opålitligt (filen är den pålitliga kopian). Syslog-paketet har ingen TIMESTAMP/HOSTNAME-header
  (Graylog förväntas sätta mottagningstid och avsändar-IP – otestat mot användarens instans); Python 3 lägger **ingen**
  UTF-8-BOM men en avslutande NUL, som är avstängd. Multi-line-poster (tracebacks) blir ett datagram; över ~1 400 byte kan det
  fragmenteras eller trunkeras. Gamla `ocpp_charger_debug.log.1`–`.3` från `RotatingFileHandler` rensas inte och räknas inte in i de
  14 dygnen – radera manuellt. HA:s egna loggrader om integrationen (`homeassistant.setup` m.fl.) berörs inte.
  Inställningar → System → Loggar visar `logging_setup.py:118` som källa för komponentens varningar/fel (bekräftat live 2026-09-20) (system_log tar första
  anropsramen under config-katalogen och det blir `HaForwardHandler.emit`); meddelande, nivå, tid och loggernamn är rätta och
  poster med `exc_info` får rätt källa – ingen ren kodlösning, verifieras live efter deploy. Poster som loggas mellan
  `remove_logging()` och nästa `apply_logging()` vid en omladdning (under en sekund) går varken till filen eller (under WARNING)
  till HA-loggen.

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
