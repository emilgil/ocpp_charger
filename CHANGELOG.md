# Ändringslogg – OCPP Charger

## 2026-06-20: Bug 30 – SOC-estimatets baslinje desyncade vid omstart mitt i en session

**Problem:** Laddning stoppade strax efter 04 med bilen på ~83 % (mål 100 %), endast 1,11 kWh levererat 04–06. Loggen: `Mål nått (SOC 100% >= mål 100%), stoppar` + auto-start undertryckt – HA *trodde* att bilen var full.

**Rotorsak:** SOC-estimatet (Bug 29) = `start_soc + levererad energi`, giltigt bara om energin räknas från när `start_soc` mättes. `_session_start_soc` hölls bara i minnet (ej persisterad) medan `energy_kwh` persisterades. Vid omstart **mitt i en session** nollställdes baslinjen och återfångades till det aktuella (mitt-i-session) SOC-värdet (t.ex. 82 %), medan ~12,3 kWh energi överlevde → den energi som *redan* höjt bilen till 82 % räknades igen → estimat ≈ 100 %, verklighet ~83 % → stopp. **Utlösare:** omstarten för att deploya Bug 29 (inte ett fel i Bug 29 självt).

**Fix:** Persistera `session_start_soc` + `session_total_kwh` i Store och återställ dem i `_load_state()` – **efter** `set_active_vehicle()` (som nollställer dem), annars klobbras återställningen. Återställt icke-None värde hindrar även återfångnings-guarden i `_update_soc_from_ha()` från att skriva över det.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_save_state()` persisterar `session_start_soc` + `session_total_kwh`; `_load_state()` återställer dem efter fordons-återställningen |

**Verifiering:** kompilerar, alla suiter gröna. Deployad live; `[Store] Återställde session-baslinje: start_soc=34.0%` loggas efter fordons-återställningen (överlever `set_active_vehicle`), och baslinjen behålls i `.storage`. Vid själva deploy-omstarten (aktiv session) förseedades baslinjen i `.storage` så den pågående laddningen inte avbröts. Framtida omstarter mitt i laddning är nu säkra.

---

## 2026-06-19: Bug 29 – Laddning stoppade vid SOC-mittpunkten (cirkulärt plan-energi-villkor)

**Problem:** Laddning avbröts strax efter 13:00 och återupptogs inte (Kia eNiro, stannade på ~84 %). Bug 28-fixen höll sessionen genom omräkningen, men `_charging_goal_reached()` stoppade istället på plan-energi-villkoret: `Mål nått (Energi 12.27 kWh >= planens 11.38 kWh)`.

**Rotorsak:** `_charging_goal_reached()` jämförde levererad energi mot `plan.energy_kwh`. Sedan Bug 16 räknas planen om mid-charge och sedan Bug 8 uppskattas aktuell SOC från levererad energi → `plan.energy_kwh` är numera *återstående* energi (≈ TOTAL − levererat). Då blir villkoret `levererat ≥ återstående` = `levererat ≥ TOTAL/2` → stopp vid SOC-mittpunkten (66→100 stannade på 83,6 %). Eftersom samma metod undertrycker auto-start (Bug 23-symmetri) återupptogs aldrig laddningen. CLAUDE.md noterade redan att `_update_charge_plan()` *medvetet* utelämnar samma villkor "(cirkulärt)".

**Fix:** Ny ren stdlib-only-modul `soc_estimate.py` (`estimate_soc()`), använd i **både** `_charging_goal_reached()` och planerarens Bug 8-block (kan inte driva isär). Mål-nått använder nu estimerad SOC ≥ target_soc (rätt, icke-cirkulärt) + target_kwh; det cirkulära plan-energi-villkoret är borttaget.

| Fil | Ändring |
|-----|---------|
| `soc_estimate.py` | Ny modul: `estimate_soc(start_soc, already_charged_kwh, capacity_kwh, efficiency, reported_soc)` |
| `__init__.py` | `_charging_goal_reached()` använder estimerad SOC, tar bort plan-energi-villkoret; planeraren använder samma helper |
| `tests/test_soc_estimate.py` | 7 nya tester (TDD) |

**Verifiering:** 7/7 nya tester gröna (rödde först); alla suiter gröna. Deployad live + omstart utan fel/traceback; den falska `>= planens`-stoppen förekommer inte längre; auto-start undertrycks inte felaktigt (väntar korrekt på billigt fönster 04:45–06:00). Fullt beteendebevis (laddning förbi mittpunkten till target) sker vid nästa fönster.

---

## 2026-06-18: Bug 28 – Omräknad plan avbröt pågående laddning utan att återuppta

**Problem:** När morgondagens priser anlände (~13:00) mitt under en aktiv session räknades planen om (avsiktligt, sedan Bug 16). Om de nya fönstren flyttades bort från nuvarande tidpunkt såg nästa `_update_smart_charging()` `in_window=False`, körde `_guarded_remote_stop()` och **avbröt pågående laddning** – som inte återupptogs förrän nästa fönster (t.ex. 22:00). Window-stopp-grenen dömde alltså en redan pågående session mot den *omräknade* planen.

**Fix:** Planens fönster fryses vid sessionstart i `_session_plan_intervals` (auto-start + manuell/Immediate start). Window-stopp-grenen bedömer en aktiv session mot den frysta listan istället för `plan.is_in_window()`. Listan nollställs endast vid kabelurkoppling (`Available`) – **inte** vid `Preparing` eller Garo 15-min-reset – så greedy-pauser inom samma kabelsession överlever och styr återupptagning. Mål-nått- och SuspendedEV-grenarna ligger ovanför och kan fortfarande stoppa.

**Designbeslut:** `allow_day_charging` (och dess automatiska `_sync_allow_day_charging()`-flip på veckoschema) avbryter medvetet **inte** en aktiv session – den frysta planen vinner. `allow_day_charging` är ett planeringsfilter för framtida fönster, inte ett "stoppa nu"-kommando; den vägen är stopp-knappen (`async_stop_charging`).

| Fil | Ändring |
|-----|---------|
| `__init__.py` | Ny `_session_plan_intervals`; frys vid auto-start + manuell start; window-stopp använder frysta listan; nollställ vid `Available`; debug-rad när frysta planen avvärjer ett stopp |

**Verifiering:** kompilerar, befintliga enhetstester gröna (frysta jämförelsen matchar `is_in_window`-semantiken `iv_start <= t <= iv_end`). Deployad live + omstart utan fel/traceback; inga spuriösa `Outside plan window`-stopp; mål-nått/manual-override-grenarna oförändrade (sågs fira korrekt). Beteendebevis (mid-charge prisuppdatering → ingen abort) loggas av `[SmartCharge] Bug28: behåller aktiv session i fryst planfönster ...` när scenariot inträffar.

---

## 2026-06-17: Bug 27 – `allow_day_charging=True` ignorerades av deadline-beräkningen

**Problem:** `_compute_deadline()` skickade inte med `allow_day_charging` till `compute_deadline()` i `deadline.py`. På vardagar returnerade `compute_deadline()` därför alltid `DEFAULT_CHARGE_DEADLINE_HOUR` (06:00), oavsett switch-läge. Billiga dagtidsslots imorgon (t.ex. 10:00–15:00) filtrerades bort i `plan_cheapest_window()` eftersom de låg efter 06:00 – switchen "Allow Day Charging" hade ingen effekt på planeringshorisonten.

**Fix:** `compute_deadline()` tar nu en `allow_day_charging`-parameter. Prioritet: manuell HH:MM → `allow_day_charging`/helg = slutet av prisdata (+15 min, annars now+48h) → vardag 06:00. `_compute_deadline()` i `__init__.py` skickar med `self.allow_day_charging`. Helg-logiken och `allow_day_charging`-logiken är nu sammanslagna i ett gemensamt villkor.

| Fil | Ändring |
|-----|---------|
| `deadline.py` | `compute_deadline()` får parameter `allow_day_charging` (helg-/dag-logik sammanslagen) |
| `__init__.py` | `_compute_deadline()` skickar `allow_day_charging=self.allow_day_charging` |
| `tests/test_deadline.py` | 4 nya tester (vardag+allow → prishorisont, no-prices→48h, manuell vinner, allow=False→06:00) |

**Verifiering:** 15/15 deadline-tester gröna (TDD: 4 nya tester rödde först, gröna efter fix). Deployad live: med `allow_day_charging=True` extenderas deadline till slutet av prisdata och planeraren valde en **dagtidsplan** `11:45–15:15 @ 26.0 öre/kWh` istället för nattplanen `72.6 öre/kWh` – exakt avsett beteende.

**Undersökt under verifieringen (ingen bugg):** Deadline syntes pendla True↔False under uppstartsfönstret. Spårning visade att det var **användaren som växlade switchen** av/på under testet, inte en intern instabilitet: `allow_day_charging` gick True→False medan `day_charging_manual_override` förblev `True` (syns i `.storage/`), och enda kodvägen som gör det är `set_allow_day_charging(False)` via `switch.async_turn_off` (notis-actions loggar "User chose" – saknas; ingen automation rör entiteten). Steady state är stabilt och konsekvent (switch av → `allow=False` → deadline 06:00). **Åtgärdat:** en debug-rad lades till i `set_allow_day_charging()` (`[DayCharging] set_allow_day_charging(<value>) (was <old>...)`) så att switch-växlingar nu syns i `ocpp_charger_debug.log` – tidigare var de osynliga.

---

## 2026-06-17: Bug 26 – Manuellt val av dagladdning persisterades inte vid omstart

**Problem:** När användaren slog på "Tillåt dagladdning" (`set_allow_day_charging(True)` sätter `allow_day_charging=True` + `_day_charging_manual_override=True`) förlorades valet vid varje HA-omstart/omladdning. Varken `allow_day_charging` eller `_day_charging_manual_override` sparades i `_save_state()`/`_load_state()`, så efter omstart initialiserades `_day_charging_manual_override=False` och `_sync_allow_day_charging()` skrev tillbaka det vardagsberäknade `allow_day_charging=False` varje koordinatorcykel. Symptom i loggen: `[ChargePlanner] Day-charging offer skipped: ...` (grenen `elif not self.allow_day_charging`) gång på gång trots att switchen slagits på.

**Fix:** `_save_state()` persisterar nu `allow_day_charging` + `day_charging_manual_override`. `_load_state()` återställer dem **endast** när `day_charging_manual_override` var satt – annars lämnas overriden av så att `_sync_allow_day_charging()` fortsätter följa vardag/helg-autoschemat. Samma Store-mekanism som redan bevisat fungerar för `charge_mode`/`active_vehicle_name` (läses i samma block, t+10s efter start, före första klobbrande spar-cykeln).

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_save_state()` + `_load_state()` persisterar/återställer `allow_day_charging` och `_day_charging_manual_override` |

**Avviker från bug26.md:** Rapportens `deadline_override`-del är **inte** implementerad – den entiteten/variabeln togs bort av Feature 4 (Deadline Override-switch → text-entitet). Att lägga `self.deadline_override` i `_save_state()` enligt rapporten hade kraschat med `AttributeError`. (Den döda konstanten `SWITCH_DEADLINE_OVERRIDE` i `const.py` kvarstår – separat städning.)

**Beteendekonsekvens:** Eftersom `_day_charging_manual_override` aldrig nollställs (förutom genom nytt manuellt val) innebär persistensen att vardag/helg-autoschemat permanent är overridat efter första switch-tryckningen, tills användaren själv ändrar switchen igen. Detta matchar bug26.md:s avsikt: "manuellt val ... bör överleva ... tills användaren aktivt ändrar det."

**Verifiering:** kompilerar, befintliga enhetstester gröna (25/25), deployad till live HA + omstart utan fel/traceback, spar-sidan bekräftad (`allow_day_charging` + `day_charging_manual_override` i `.storage/`). Load-sidan verifierad via strukturell ekvivalens med fungerande restores; `[Store] Återställde allow_day_charging=...` loggas vid nästa riktiga switch-tryck + omstart.

---

## 2026-06-15: Bug 25 – Utan kabel planerades för bilen med lägst SOC istället för active_vehicle

**Problem:** När ingen kabel var inkopplad beräknade `_update_charge_plan()` laddplanen (och dashboardens grafer/savings-sensor) för fordonet med **lägst SOC** – oavsett vad användaren valt i `select.*_active_vehicle`. No-cable-grenen itererade alltid över alla fordon och valde lägst SOC, vilket ignorerade `self.active_vehicle`. Dessutom var grenen internt inkonsekvent: `energy_needed` dimensionerades för lägst-SOC-bilen medan `power_kw` (strömtaket på rad 1706) redan använde `active_vehicle`.

**Fix:** No-cable-grenen planerar nu för `self.active_vehicle` (fallback `_vehicles[0]`). SOC läses från fordonets konfigurerade `VEHICLE_SOC_ENTITY`, kapacitet från `VEHICLE_CAPACITY`. Därmed använder både energi och effekt samma fordon. Eftersom `set_active_vehicle()` redan anropar `_update_charge_plan()` med throttle-bypass räknas planen om direkt vid byte i dropdownen.

| Fil | Ändring |
|-----|---------|
| `__init__.py` | No-cable-grenen i `_update_charge_plan()` använder `active_vehicle` istället för lägst-SOC-iteration |

**Verifiering:** kompilerar, befintliga enhetstester gröna (25/25), en `no cable, planning for`-träff med texten `planning for active vehicle`. Beteendetest (live): byt fordon i dropdown utan kabel → loggen visar `planning for active vehicle <namn>` och planen räknas om omedelbart.

---

## 2026-06-12: Bug 24 – Charge Windows-sensorn uppdaterades inte vid manuell planändring

**Problem:** När användaren bytte planeringsalgoritm (Greedy/Contiguous) – eller ändrade något annat som anropar `_update_charge_plan()` direkt (target_soc, charge_mode m.m.) – uppdaterades inte `sensor.*_charge_windows` (`calculated_at`/`slots`) förrän nästa polling-cykel (~5 min). Orsak: `_rebuild_charge_windows()` anropades bara från `_async_update_data()`, inte i den direkta setter-kodvägen.

**Fix:** `_rebuild_charge_windows()` anropas nu även inuti `_update_charge_plan()` på två ställen: direkt efter att `charge_plan`/`_alt_plan` beräknats (täcker huvudvägen + närvarobaserad dag-gren) och före natt-switch-returen (täcker omtilldelningen till `night_plan`). Objektidentitetsguarden gör det per-cykel-anropet i `_async_update_data()` till en no-op, så ingen dubbel-rebuild i steady state. Anropet i `_async_update_data()` behålls (hanterar post-hoc `actual_energy`).

| Fil | Ändring |
|-----|---------|
| `__init__.py` | `_rebuild_charge_windows()`-anrop tillagda i `_update_charge_plan()` (efter `_alt_plan` + före natt-switch-return) |

**Verifiering:** kompilerar, befintliga enhetstester gröna, 4 `_rebuild_charge_windows`-referenser. Beteendetest (live): byt algoritm → `calculated_at` uppdateras inom sekunder utan att vänta på polling-cykeln.

---

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

**Status:** Implementerad, lokalt verifierad (11 deadline-tester PASS, alla moduler kompilerar) och **deployad + verifierad på live-HA 2026-06-12**: komponenten laddar utan fel, deadline-logiken körs korrekt (vardag → nästa 06:00), och text-entiteten `text.garage_ev_charger_garocs_48671aa056e80_manual_deadline` är registrerad och aktiv. Den gamla `switch.*_deadline_override`-entiteten ligger kvar föräldralös i registret och kan raderas manuellt via Inställningar → Enheter & tjänster → Entiteter.

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
