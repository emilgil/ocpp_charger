# Bug 60 – Hårdkodade entiteter i const.py (närvaro, hemma-zon, starttimme, sluttid-helper)

**Datum:** 2026-11-14
**Status:** Implementerad lokalt, tester gröna – ej deployad
**Beroenden:** Bug 18 (närvarobaserat dagladdningserbjudande), Feature 6 (sluttid via `input_datetime`), Bug 39 (lyssnare på helpern). Oberoende av Feature 12 (Skoda).

---

## Symptom

Fyra värden som beskriver en enskild installation ligger som konstanter i `const.py`. Att ändra dem kräver kodändring, deploy och omstart av HA:

| Konstant | Rad | Värde | Används i `__init__.py` |
|---|---|---|---|
| `PRESENCE_ENTITIES` | 228 | `device_tracker.skoda_enyaq_position`, `device_tracker.emil_s_s23_ultra`, `device_tracker.e_niro_location` | `_someone_home()` (2029) |
| `PRESENCE_HOME_STATES` | 236 | `{"home", "framsidan"}` | `_someone_home()` (2033) |
| `DAY_OFFER_EARLIEST_HOUR` | 227 | `9` | dagerbjudandet i `_update_charge_plan()` (2722, 2723, 2759) |
| `INPUT_DATETIME_DEADLINE` | 72 | `input_datetime.charger_target_time` | `_deadline_entity_id` (488), Bug 39-lyssnaren (252–262) |

Allt annat installationsspecifikt (prissensor, notistjänst, SOC, plug-sensor, väckning per bil) kommer redan från config flow.

## Beslut (från frågerundan 2026-11-14)

1. Alla fyra värden flyttas till options-flowet.
2. "Hemma" avgörs med en zonväljare, inte en fritextlista.
3. Befintliga installationer **startar tomma**. Inga personliga entitets-id:n finns kvar i koden, och ingen migrering skriver in dagens värden.
4. Tom närvarolista = dagerbjudandet är av.
5. Två nya menyval i options: ett för dagerbjudandet, ett för sluttid-helpern.

## Följd av beslut 3 – läs detta före deploy

Direkt efter deploy, tills inställningarna fyllts i under options:

- **Dagerbjudandet skickas inte** (ingen närvarolista).
- **Manuell sluttid fungerar inte.** `input_datetime.charger_target_time` läses inte, så deadline blir alltid automatisk (vardag 06:00, helg slutet av prisdatan). Helpern nollas inte heller vid urkoppling. Dashboardens sluttidsfält går att ändra men påverkar ingenting.

Åtgärd efter deploy (en gång): options → fyll i båda nya menyvalen → Save and close (integrationen laddas om, laddaren återansluter som vid alla options-ändringar). Deploya därför när ingen laddning pågår.

## Ny konfiguration

Lagras i `entry.data`, som övriga options.

| Nyckel (`const.py`) | Typ | Tomt/standard | Betydelse |
|---|---|---|---|
| `CONF_PRESENCE_ENTITIES = "presence_entities"` | lista av entity_id | `[]` | Vilka som räknas för dagerbjudandet. Tom = erbjudandet av. |
| `CONF_PRESENCE_ZONES = "presence_zones"` | lista av zon-entity_id | `[]` | Extra zoner som räknas som hemma, utöver state `home`. |
| `CONF_DAY_OFFER_EARLIEST_HOUR = "day_offer_earliest_hour"` | heltal 0–23 | `9` | Tidigaste timme för erbjudandet. |
| `CONF_DEADLINE_ENTITY = "deadline_entity"` | entity_id | `""` | `input_datetime`-helper för manuell sluttid. Tom = alltid automatisk deadline. |

`DEFAULT_DAY_OFFER_EARLIEST_HOUR = 9` blir kvar som generisk standard. `PRESENCE_ENTITIES`, `PRESENCE_HOME_STATES`, `DAY_OFFER_EARLIEST_HOUR` och `INPUT_DATETIME_DEADLINE` tas bort ur `const.py`.

## Ändringar

### 1. `config_flow.py` – options

Två nya menyval i `async_step_init`, mellan "detection" och "notify":

- `"day_offer"` → `☀️ Edit daytime offer settings` → `async_step_edit_day_offer`
- `"deadline"` → `⏰ Edit deadline helper` → `async_step_edit_deadline`

**`edit_day_offer`** (sparas i `self._day_offer_data`):

- `presence_entities`: `EntitySelector`, `multiple=True`, domäner `device_tracker` och `person`.
- `presence_zones`: `EntitySelector`, `multiple=True`, domän `zone`.
- `day_offer_earliest_hour`: `NumberSelector` 0–23, steg 1, BOX, `default=` sparat värde eller 9.

**`edit_deadline`** (sparas i `self._deadline_data`):

- `deadline_entity`: `EntitySelector`, domän `input_datetime`, ett värde.

De tre entitetsfälten får **ingen `default=`** utan `description={"suggested_value": ...}`, och läses med `user_input.get(key, [])` respektive `""`. Annars går ett sparat värde inte att tömma (samma lärdom som `syslog_host` och `plug_entity`).

`_save()` får två rader till (`_day_offer_data`, `_deadline_data`). Initial config flow (de fyra stegen) ändras inte: nya installationer startar tomma och ställer in detta i options.

Validering: `deadline_entity` måste vara tom eller börja med `input_datetime.` (felkod `deadline_entity_invalid`). Att entiteten faktiskt finns kontrolleras inte i flowet.

### 2. `__init__.py` – koordinatorn

Värdena läses ur `entry.data` i `OCPPCoordinator.__init__` (options-sparning laddar redan om entryn, så ingen live-uppdatering behövs):

```
self._presence_entities: list[str]
self._presence_zones: list[str]
self._day_offer_earliest_hour: int      # trasigt värde → 9
self._deadline_entity_id: str           # "" = ingen helper
```

**`_someone_home()`**

- Tom `_presence_entities` → `False`.
- Mängden hemma-states byggs vid varje anrop: `"home"` plus det gemener-satta visningsnamnet (`state.name`) för varje vald zon. Zon som saknas i state machine hoppas över. Namnet läses vid anropet, så en omdöpt zon följer med.
- Jämförelsen är som idag: `st.state.lower() in hemma-states`. Debugraden `[Presence] someone_home=…` behålls och får med vilka states som räknades.

**Dagerbjudandet i `_update_charge_plan()`**

- Ny första skip-orsak: `no presence entities configured`.
- `DAY_OFFER_EARLIEST_HOUR` ersätts med `self._day_offer_earliest_hour` på de tre ställena.
- Övrig logik (Bug 17-jämförelsen, en gång per dag, `_day_charging_dismissed`, kabelvillkoret) oförändrad.

**Sluttid-helpern**

- `_get_manual_deadline_str()`: tom `_deadline_entity_id` → `""` (automatisk).
- `_reset_deadline_helper()`: tom → return. Befintlig guard för saknad entitet kvar.
- Bug 39-lyssnaren i `async_setup_entry` registreras bara när `_deadline_entity_id` är satt.
- En loggrad vid start: `[Bug60] Sluttid-helper: <entity_id>` eller `[Bug60] Ingen sluttid-helper konfigurerad – automatisk deadline`. Motsvarande för närvaro: `[Bug60] Dagerbjudande: N närvaroentiteter, zoner=[…], från HH:00` eller `… av (ingen närvaroentitet)`.

### 3. Texter

`strings.json` och `translations/sv.json`: menytexter, de två nya stegen med fältnamn och hjälptext ("Tomt = dagerbjudandet är av", "Tomt = alltid automatisk sluttid", "State `home` räknas alltid; välj zoner som också ska räknas som hemma"), samt felkoden `deadline_entity_invalid`. `sv.json` i komponentroten ser ut att vara en gammal kopia (saknar `edit_detection`) och lämnas orörd.

### 4. Dokumentation (vid implementation)

`CLAUDE.md` (Bug 18-avsnittet, Feature 6-avsnittet, koordinatorfälten, nytt Bug 60-avsnitt), `README.md` rad 50 (helpern väljs nu i options), `CHANGELOG.md`.

## Rör inte

- `deadline.py` / `compute_deadline()` och Bug 59-logiken.
- Bug 26/52 (manuell dagladdnings-override), Bug 40, Bug 51:s kabelvillkor för dag-notisen.
- Notisinnehåll och knappar för `on_day_charging_chosen`.
- Per-bil-fälten (`plug_entity`, `wake_action`).

## Tester

`tests/test_bug60.py` (rot-venv, `coordinator_harness.py`):

1. Tom närvarolista → `_someone_home()` False och skip-orsak `no presence entities configured`; ingen notis.
2. Tracker i state `home` → True utan valda zoner.
3. Tracker i state `Framsidan`, zonen vald → True; zonen inte vald → False.
4. Skiftläge: state `framsidan` mot zonnamn `Framsidan` → True.
5. Vald zon saknas i state machine → hoppas över, inget undantag.
6. `person`-entitet i `home` → True.
7. Starttimme 7: erbjudande 07:30 skickas, 06:59 hoppas över (`before 07:00`).
8. Trasigt timvärde i `entry.data` → 9.
9. Tom `deadline_entity` → `_get_manual_deadline_str()` `""`, `_reset_deadline_helper()` gör inget tjänsteanrop, ingen lyssnare registreras.
10. Satt `deadline_entity` med annat namn än det gamla → värdet läses därifrån, nollas vid äkta urkoppling, och en ändring triggar omplanering (Bug 39).
11. `grep` över paketet: inga förekomster av de fyra gamla konstantnamnen eller av `skoda_enyaq_position`, `emil_s_s23_ultra`, `e_niro_location`, `framsidan`, `charger_target_time` i `.py`-filer.

Config flow (`tests/test_bug60_config_flow.py`, mönster från `test_feature10_config_flow.py`): båda stegen sparar; tömda fält sparas som `[]`/`""`; `deadline_entity_invalid` för fel domän; texter finns i `strings.json` och `translations/sv.json`.

Befintliga tester som patchar eller förutsätter helpern/närvaron (bl.a. `test_bug51.py`) ses över: harnessen sätter de nya nycklarna där testet behöver dem.

## Deploy

`*.py` via `scp`, plus **explicit** `strings.json` och `translations/sv.json`. `ha core restart`. Ingen laddning pågående.

## Live-verifiering

1. Direkt efter omstart, före options: debugloggen visar `[Bug60] … av (ingen närvaroentitet)` och `[Bug60] Ingen sluttid-helper konfigurerad`. Inga fel.
2. Options: välj närvaroentiteter, zonen Framsidan, timme 9 och `input_datetime.charger_target_time` → Save and close → loggraderna visar de valda värdena.
3. Ändra helpern i dashboarden → `[Deadline] … ändrad … planerar om` och Laddfönster uppdateras direkt.
4. Töm helper-fältet i options och spara → fältet är tomt när formuläret öppnas igen.
5. Dagerbjudandet med kabel inkopplad en vardag efter vald timme (kräver att dagen är billigare än natten; kan dröja).

## Kända begränsningar

- En `input_datetime` med både datum och tid ger state `YYYY-MM-DD HH:MM:SS`. Hur `helper_state_to_hhmm()` hanterar det är inte kontrollerat i den här specen; helpern ska vara av typen "Tid". Kontrolleras vid implementation.
- Zoner matchas på visningsnamn, eftersom det är vad en tracker rapporterar som state. Två zoner med samma namn går inte att skilja åt.
- En närvaroentitet som tagits bort i HA (t.ex. Skodans tracker när MySkoda avinstalleras) ligger kvar i listan tills den plockas bort i options. Den matchar aldrig och syns som `<missing>` i `[Presence]`-raden.
