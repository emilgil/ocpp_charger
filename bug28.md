# Bug 28 – Omräknad plan avbryter pågående laddning och återupptar inte

**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-18

> **Implementerat enligt rapport** (radnummer verifierade mot skarp kod först): ny `_session_plan_intervals`,
> frysning vid auto-start (rad 920) + manuell start (`async_start_charging`), window-stopp-grenen (rad 967)
> använder frysta listan, nollställning vid `Available`. Den frysta jämförelsen matchar `is_in_window`-semantiken
> (`iv_start <= t <= iv_end`). Lade dessutom till en debug-rad (`[SmartCharge] Bug28: behåller aktiv session
> i fryst planfönster ...`) som fyrar exakt när fixen avvärjer ett stopp, så scenariot blir verifierbart i loggen.
>
> **Live-verifiering:** integration laddade utan fel/traceback; inga spuriösa `Outside plan window`-stopp;
> mål-nått/manual-override-grenarna oförändrade. Fullt beteendebevis (mid-charge prisuppdatering → ingen abort)
> inväntar det naturliga scenariot – debug-raden bekräftar det då.

## Symptom

När ny prisdata publiceras (~13:00) mitt under en aktiv laddningssession räknas
laddplanen om. Om kommande dygns priser är lägre än innevarande väljer den nya
planen nattfönster och utesluter den nuvarande tidpunkten. Nästa
`_update_smart_charging()`-cykel ser `in_window=False`, kör `_guarded_remote_stop()`
och **avbryter pågående laddning**. Laddningen återupptas inte automatiskt – bilen
står still tills nästa planfönster öppnar (t.ex. 22:00).

Observerat beteende: bilen står ostartad i flera timmar mitt på dagen efter att
morgondagens priser anlänt.

---

## Rotorsak

Detta är en konsekvens av Bug 16-fixen (intentionell): `_update_charge_plan()`
körs nu alltid, även under aktiv laddning, så att nya priser plockas upp mid-charge.
Det är korrekt för *framtida* planering.

Problemet är att stopp-logiken i `_update_smart_charging()` (window-check-grenen,
rad ~967) använder den **omräknade** `self.charge_plan.active_intervals` för att
avgöra om en *redan pågående* session ska stoppas. När omräkningen flyttar fönstren
bort från nuvarande tidpunkt tolkas det som "utanför planfönster" och sessionen
stoppas.

`is_in_window()` kan inte skilja två fall åt:
1. **Legitim greedy-paus** – planen har en avsiktlig lucka (t.ex. 00:00–03:00
   mellan två valda fönster). Här *ska* laddningen pausa.
2. **Omräkningsartefakt** – planens fönster har flyttats av ny prisdata mitt i en
   session. Här ska laddningen *inte* avbrytas.

Pingpong-skyddet (`_last_remote_start`, 5 min) skyddar bara mot upprepade
RemoteStart tätt – det skyddar inte en pågående session från att stoppas.

---

## Önskat beteende (beslutat)

En pågående laddningssession ska få fortsätta enligt den plan som gällde **när
sessionen startade**. Omräkningar under sessionen påverkar bara framtida sessioners
start/stopp, inte den aktiva sessionen.

Endast **mål nått** (`_charging_goal_reached()`) och **SuspendedEV** får avbryta en
aktiv session. "Utanför planfönster"-stopp ska respektera den frysta planen, inte
den omräknade.

Greedy scheduling måste fortsatt fungera: en kabel-session kan bestå av flera
planerade fönster med avsiktliga pauser emellan. Dessa pauser ska respekteras
eftersom de finns i den frysta planen.

---

## Fix: frys planens fönster vid sessionstart

Spara en kopia av `active_intervals` när en session startar. Under sessionen styr
den frysta listan window-besluten i stopp-logiken. Nollställ listan när
kabel-sessionen avslutas (`Available`). Greedy-pauser passerar aldrig `Available`,
så den frysta planen överlever pausen och styr återupptagning korrekt.

### Berörd fil

`/config/custom_components/ocpp_charger/__init__.py`

---

### 1. Ny instansvariabel i `__init__()`

Placeras bredvid övriga session-flaggor (nära `self._was_charging`, rad ~384):

```python
self._session_plan_intervals: list[tuple[datetime, datetime]] | None = None
```

`None` betyder "ingen aktiv session med fryst plan" → window-check faller tillbaka
på `plan.active_intervals` (oförändrat beteende för auto-start och vila).

---

### 2. Frys planen vid auto-start

I auto-start-blocket (rad ~917–928), direkt efter att `self._last_remote_start`
satts (rad ~920):

```python
            self._last_remote_start = now_utc
            # Bug 28: freeze the plan windows for this session so a later
            # recalculation (new prices mid-charge) can't shift the window
            # out from under the active session.
            self._session_plan_intervals = list(plan.active_intervals)
```

---

### 3. Frys planen vid manuell/Immediate start

I `async_start_charging()` (rad ~1230–1242), direkt efter
`self._manual_start_requested = True` (rad ~1240):

```python
        self._manual_start_requested = True
        # Bug 28: freeze current plan windows for this manually-started session.
        if self.charge_plan and self.charge_plan.active_intervals:
            self._session_plan_intervals = list(self.charge_plan.active_intervals)
```

(Immediate-läge går via `async_start_if_ready()` → `async_start_charging()`, så
ingen separat ändring behövs där.)

---

### 4. Använd den frysta planen i window-stopp-grenen

I `_update_smart_charging()`, window-check-grenen (rad ~967). Idag:

```python
            in_window = plan.is_in_window(now_utc)
            if not in_window and self.ocpp.state.charging:
```

Ändras så att en aktiv session med fryst plan bedöms mot den frysta listan:

```python
            # Bug 28: an active session is gated by the plan frozen at its start,
            # not by a plan that may have been recalculated mid-session.
            if self.ocpp.state.charging and self._session_plan_intervals is not None:
                in_window = any(
                    iv_start <= now_utc <= iv_end
                    for iv_start, iv_end in self._session_plan_intervals
                )
            else:
                in_window = plan.is_in_window(now_utc)
            if not in_window and self.ocpp.state.charging:
```

Mål-nått- och SuspendedEV-grenarna ligger ovanför denna punkt och är oförändrade –
de fortsätter kunna stoppa sessionen.

---

### 5. Nollställ vid kabelurkoppling

I det centraliserade `Available`-blocket (rad ~1409), lägg till bland övriga
nollställningar:

```python
        if status == "Available":
            self._was_charging = False
            self._session_plan_intervals = None  # Bug 28: clear frozen plan on cable disconnect
            ...
```

**Nollställ INTE** vid `Preparing` eller vid Garo 15-min-reset. En greedy-paus inom
en kabel-session passerar aldrig `Available`, så den frysta planen ska överleva
pausen och styra återupptagningen vid nästa fönster i listan.

---

## Verifiering CC ska köra mot skarp kod (192.168.1.97) INNAN implementation

Radnummer ovan är från project-knowledge-kopian och kan ha drivit isär från
deployad kod. Bekräfta först:

```bash
# Startpunkter – ska visa auto-start (_last_remote_start = now_utc) + async_start_charging
grep -n -E "(_last_remote_start = now_utc|_manual_start_requested = True|remote_start_transaction)" \
  /config/custom_components/ocpp_charger/__init__.py

# Window-stopp-grenen – bekräfta is_in_window-anropet och Outside plan window
grep -n -E "(is_in_window|Outside plan window|_guarded_remote_stop)" \
  /config/custom_components/ocpp_charger/__init__.py

# Available-nollställningsblocket
grep -n -E "status == \"Available\"" \
  /config/custom_components/ocpp_charger/__init__.py

# Bekräfta att instansvariabeln inte redan finns
grep -n "_session_plan_intervals" \
  /config/custom_components/ocpp_charger/__init__.py
```

Justera radnummer och kontext efter faktiskt utfall innan ändringarna görs.

---

## Verifiering efter implementation

```bash
grep -n -E "(Tomorrow prices|plan=|Outside plan window|RemoteStop|_session_plan_intervals)" \
  /config/ocpp_charger_debug.log | grep -A5 "Tomorrow prices"
```

Förväntat: efter "Tomorrow prices arrived" mitt under laddning ska planen
uppdateras (ny `plan=`-rad) men **ingen** `Outside plan window`-stopp och **ingen**
`RemoteStop` ska följa. Laddningen fortsätter oavbrutet i sitt frysta fönster.

Kontroll av greedy-paus: en planerad paus inom samma kabel-session ska fortfarande
stoppa vid pausens början och återuppta vid nästa fönster (frysta listan styr).

---

## Interaktion med tidigare buggar

| Bug | Påverkan |
|-----|----------|
| Bug 16 (replanering under laddning) | Bevaras – planen uppdateras fortfarande mid-charge, men styr nu bara *framtida* sessioner, inte den aktiva |
| Bug 22 (slot-filter bevarar aktiv slot) | Oförändrad – påverkar planeringen, inte den frysta sessionplanen |
| Bug 23 (auto-start/mål-nått pingpong) | Oförändrad – mål-nått-grenen ligger ovanför window-check och använder `_charging_goal_reached()` |
| Bug 19 (manual override mot goal-check) | Oförändrad – `_manual_start_requested` respekteras separat |
| Bug 14 (auto-restart efter SOC-stop) | Oförändrad – `_soc_target_reached` styr auto-start, ortogonal mot frysta planen |
| Bug 13 (Garo 15-min reset) | Viktig: Garo-reset (StopTransaction → Preparing utan Available) får INTE nollställa `_session_plan_intervals`. Endast `Available` nollställer. |

---

## Designbeslut: `allow_day_charging` avbryter INTE en aktiv session

`set_allow_day_charging(False)` (rad ~1329) kringgår throttle och tvingar en
omedelbar omräkning (`_last_plan_update = None; _update_charge_plan()`). Den
omräknade planen filtrerar bort alla dagsslots (rad ~1732) och kan därmed utesluta
nuvarande tidpunkt under en pågående dagladdning.

**Med Bug 28-fixen ska detta INTE avbryta sessionen** – den frysta planen vinner,
precis som vid en prisbaserad omräkning. CC ska medvetet *inte* lägga till något
undantag som låter `allow_day_charging` stoppa aktiv laddning.

Motiv:
- `allow_day_charging` är ett **planeringsfilter** för *framtida* fönsterval, inte
  en stopp-signal. Det har aldrig varit designat som ett "stoppa nu"-kommando.
- Flaggan delas med den automatiska `_sync_allow_day_charging()` (rad ~1360), som
  flippar värdet på veckoschema – t.ex. söndag 18:00 `True → False`. Om flaggan
  vore en stopp-signal skulle ett sådant *automatiskt* byte riva en pågående
  helgladdning. Det är exakt den klass av bugg Bug 28 stänger.
- Användaravsikten "sluta ladda nu" har redan en ren, otvetydig väg:
  `async_stop_charging()` (stopp-knappen), som sätter `_manual_stop_requested`.

Två avsikter, två kontroller: "ladda inte på dagtid framöver" → `allow_day_charging`;
"sluta ladda nu" → stopp-knappen.

---

## Berörda filer

- `__init__.py` – 1 ny instansvariabel + 4 platser (auto-start, manuell start,
  window-check, Available-nollställning)

Inga ändringar i `charge_planner.py`, `ocpp_client.py`, `notifier.py` eller andra
filer.
