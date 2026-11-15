# Bug 61 – STOP-knappen: auto-start drar igång laddningen igen, och laddad energi tappas ur SOC-estimatet

**Datum:** 2026-11-15 (rev 2 samma dag: notiser, SOC-estimatet)
**Status:** Specad (väntar på godkännande, ingen kod skriven)
**Beroenden:** Bug 14/23 (auto-start-spärrar), Bug 28/31 (fryst sessionsplan), Bug 33 (ackumulering vid Garo-reset), Bug 47 (manuell start). Bug 62 bygger på den här (STOP häver strömbegränsningen, samma "fönstret är slut"-villkor).

---

## Symptom

1. Tryck på STOP under ett aktivt planfönster i Smart-läge stoppar laddningen, men den startar igen av sig själv så länge planfönstret är öppet.
2. Efter STOP ändras Laddfönster-grafen: fönstret blir längre, trots att inget mål ändrats.
3. Ingen notis skickas, varken om stoppet eller om att laddningen kom igång igen.

## Loggbevis (Graylog 2026-11-15, lokal tid)

### Omstarten

| Tid | Rad |
|---|---|
| 12:34:15.796 | `[OCPP] → CALL action=RemoteStopTransaction {'transactionId': 1794739602}` – inget `[SmartCharge] … stopping` före, alltså knappen |
| 12:34:16.199 | `[Notify] Laddning pausad men plan aktiv till 13:45 – håller inne stopp-notis` |
| 12:34:18.568 | `StatusNotification Finishing` → `[SmartCharge] Transaktion redan aktiv (1794739602), hoppar över auto-start` |
| 12:34:18.687 | `StopTransaction reason=Remote`; `[Session] OCPP tx avslutad: +8.06 kWh (totalt 8.06 kWh denna kabelsession)` |
| 12:34:18.829 | `StatusNotification Preparing` → `[Notify] Skippar Inkopplad – manuellt stopp` |
| 12:34:28.125 | `[SmartCharge] Plan window active (12:30–13:45), starting charge` |
| 12:34:28.306 | `RemoteStartTransaction: result=Accepted` |
| 12:34:28.473 | `[Bug13A] Preparing utan föregående Available – Garo-reset … (sparar 0.000 kWh, _session_total_kwh=0.000)` |
| 12:34:31.529 | `StatusNotification Charging` (16 A) |

Endast ett manuellt stopp finns i loggen mellan 11:40 och 13:50. Omstarten kom vid nästa uppdateringscykel (10 s), inte vid nästa kvart.

### SOC-estimatet och Laddfönster

| Tid | Rad |
|---|---|
| 12:32:15 | `Estimerad SOC: start=63.0% +7.63 kWh → 74.0%` · `Planning: soc=74%→90% energy=11.15 kWh power=11.0 kW` · plan `12:30–13:45` |
| 12:39:36 | `Estimerad SOC: start=63.0% +0.68 kWh → 64.0%` · `Planning: soc=64%→90% energy=18.10 kWh power=11.0 kW` · plan `12:30–14:15` |
| 12:49:46 | `Estimerad SOC: start=63.0% +2.37 kWh → 73.0%` (bilens uppmätta SOC har kommit in) · plan `12:45–14:00` |

De 8,06 kWh som laddats före STOP försvann ur estimatet. Planeraren räknade därför med 7 kWh för mycket och förlängde fönstret med 30 minuter. Planeraren räknade hela tiden med 11,0 kW (16 A), så Override Current-slidern (Bug 62) var inte orsaken.

## Rotorsak

### A. Auto-start saknar spärr för manuellt stopp (`__init__.py`)

- `async_stop_charging()` (rad 1838–1843) sätter `_manual_stop_requested = True` och skickar `RemoteStopTransaction`.
- `_manual_stop_requested` används bara av notislogiken: den förbrukas på första `Preparing` (rad 2133–2136).
- Auto-start-blocket i `_update_smart_charging()` (rad 1453–1490) har spärrar för mål nått (Bug 23), aktiv transaktion (Bug 5) och 300 s sedan senaste RemoteStart, men ingen för manuellt stopp.

### B. Tidig `return` hoppar över energiackumuleringen (`__init__.py` rad 2133–2136)

```python
        if status == "Preparing" and self._manual_stop_requested:
            _LOGGER.debug("[Notify] Skippar Inkopplad – manuellt stopp")
            self._manual_stop_requested = False
            return
```

Blocket ligger före Bug 13A/33-blocket (rad 2138–2180) som lägger den avslutade transaktionens energi till `_session_total_kwh`. Returen hoppar dessutom över `self._last_connector_status_notify = status` (rad 2347). Nästa pass går därför in i Garo-reset-grenen, men då har den nya `StartTransaction` redan nollat `state.energy_kwh`: `+= 0.000`.

### C. Notiser

- Stopp-notisen hålls inne av Bug 11-villkoret eftersom planen fortfarande är aktiv (rad 2300–2311).
- Start-notisen skickas en gång per kabelsession (`_cable_session_start_notified`, rad 2246), så en återstart ger ingen notis.

## Beslut (frågerunda 2026-11-15)

1. STOP spärrar auto-start **resten av det pågående planfönstret**. Nästa fönster startar automatiskt.
2. START-knappen och urkoppling av kabeln häver spärren.
3. Om planen räknas om och fönstret förlängs gäller spärren tills planen har en lucka vid aktuell tid.
4. En notis skickas när laddningen stoppas manuellt, och en när den startar igen.
5. STOP avslutar sessionen: en aktiv strömbegränsning hävs (specas i Bug 62).

## Fix

### 1. Nya flaggor i `OCPPCoordinator.__init__()` (vid rad 549–550)

```python
self._manual_stop_block: bool = False      # Bug 61: STOP spärrar auto-start tills planfönstret är slut
self._resume_notify_pending: bool = False  # Bug 61: skicka "återupptagen"-notis vid nästa laddstart
```

`_manual_stop_requested` behåller sin roll oförändrad.

### 2. `async_stop_charging()`

```python
# FÖRE
        self._manual_start_requested = False
        self._manual_stop_requested = True
        await self.ocpp.remote_stop_transaction()
        await self.async_refresh()

# EFTER
        self._manual_start_requested = False
        self._manual_stop_requested = True
        self._manual_stop_block = True          # Bug 61
        self._resume_notify_pending = True      # Bug 61
        _LOGGER.info("[Bug61] Manuellt stopp – auto-start spärrad tills planfönstret är slut")
        await self.ocpp.remote_stop_transaction()
        self._notify_manual_stop()              # Bug 61, se punkt 8
        await self._save_state()
        await self.async_refresh()
```

### 3. `_update_smart_charging()` – häv spärren när fönstret är slut

Direkt efter blocket som beräknar `_auto_start_in_window` (efter rad 1420), före kabelkontrollen:

```python
        # Bug 61: spärren gäller bara det planfönster som var öppet vid STOP.
        # Hävs först när en giltig plan finns och nuet ligger utanför alla fönster.
        if (
            self._manual_stop_block
            and not state.charging
            and self.charge_mode == CHARGE_MODE_SMART
            and self.charge_plan and self.charge_plan.feasible and self.charge_plan.active_intervals
            and not _auto_start_in_window
        ):
            _LOGGER.info("[Bug61] Planfönstret är slut – auto-start tillåts igen")
            self._manual_stop_block = False
            self.hass.async_create_task(self._save_state())
```

Saknas plan, eller är planen inte genomförbar, ligger spärren kvar. Annars skulle en tillfälligt tom plan släppa spärren mitt i fönstret.

### 4. `_update_smart_charging()` – spärr i auto-start-blocket

Första villkoret i `if _auto_start_in_window:` (före Bug 23-kontrollen, rad 1454):

```python
            # Bug 61: användaren har tryckt STOP i det här fönstret
            if self._manual_stop_block:
                _LOGGER.debug("[SmartCharge] Auto-start spärrad – manuellt stopp i pågående fönster")
                return
```

### 5. Häv spärren vid START och urkoppling

- `async_start_charging()`: `self._manual_stop_block = False` bredvid `self._manual_start_requested = True` (rad 1831).
- `available_edge`-blocket (rad 2102–2108): `self._manual_stop_block = False` och `self._resume_notify_pending = False`.

### 6. Persistens

`_save_state()`: `"manual_stop_block"` och `"resume_notify_pending"`. `_load_state()`: standard `False`. En HA-omstart mitt i ett spärrat fönster ska inte starta laddningen.

### 7. `_check_notify_events()` – ackumulera energin före returen (rotorsak B)

Flytta manuellt-stopp-blocket (rad 2133–2136) till **efter** Bug 13A-blocket (efter rad 2180), före Inkopplad-villkoret, och uppdatera statusspåraren före returen:

```python
        # Bug 13A-blocket (oförändrat) körs först, så att Bug 33-ackumuleringen
        # (_session_total_kwh += state.energy_kwh) sker även efter manuellt stopp.

        if status == "Preparing" and self._manual_stop_requested:
            _LOGGER.debug("[Notify] Skippar Inkopplad – manuellt stopp")
            self._manual_stop_requested = False
            self._last_connector_status_notify = status   # Bug 61: annars ackumuleras energin en gång till nästa pass
            return
```

Bug 41- och Bug 45-grenarna i Bug 13A-blocket gäller därmed även efter manuellt stopp.

### 8. Notiser

**Stopp.** Ny metod `_notify_manual_stop()` i koordinatorn och `on_manual_stop()` i `notifier.py`. Skickas direkt, styrs av `_notify_on_stop`. Den förbrukar **inte** `_cable_session_stop_notified`, så sammanfattningen vid urkoppling skickas som vanligt.

- Titel: `Laddning stoppad`
- Text: `Stoppad manuellt. {energi:.1f} kWh laddat hittills, SOC ca {soc:.0f} %. Nästa planerade start: {HH:MM}.`
  - energi = `_cable_session_energy_kwh` + öppen transaktions energi
  - SOC = `estimate_soc(...)` (samma som planeraren)
  - nästa start = `charge_plan.next_start_after(<slutet av pågående fönster>)`; saknas den: `Ingen ny start planerad.`

**Start igen.** I "Charging started"-blocket (rad 2238–2278), som eget villkor efter den befintliga start-notisen:

```python
        # Bug 61: laddningen har kommit igång igen efter ett manuellt stopp
        if self._resume_notify_pending and is_charging and state.power_w > 100:
            self._resume_notify_pending = False
            if self._notify_on_start:
                self.notifier.on_charging_resumed(
                    soc_pct=<estimate_soc>, current_a=state.current_a, power_kw=state.power_w / 1000,
                    plan_end=plan.end if plan and plan.feasible else None,
                    manual=self._manual_start_requested,
                )
            self.hass.async_create_task(self._save_state())
```

Ny metod `on_charging_resumed()` i `notifier.py`:

- Titel: `Laddning återupptagen`
- Text: `Startad {manuellt | enligt plan}. {effekt:.1f} kW, SOC ca {soc:.0f} %. Planerat slut: {HH:MM}.`

Gäller både START-knappen och auto-start i nästa fönster.

## Rör inte

- Rad 1484 (`_manual_stop_requested = False` vid auto-start).
- Bug 11-villkoret för den ordinarie stopp-notisen och `_send_stop_notification()`.
- `_cable_session_start_notified` och den ordinarie start-notisen.
- Bug 28-logiken för `_session_plan_intervals`.
- Immediate-läget: inget startar om där efter STOP. Notiserna i punkt 8 gäller även där.

## Pre-verify (live-servern, före implementation)

```bash
cd /config/custom_components/ocpp_charger
grep -n "_manual_stop_requested" __init__.py          # förväntat: 550, 1484, 1841, 2133, 2135
grep -n "async def async_stop_charging" __init__.py   # förväntat: 1838
grep -n "if _auto_start_in_window:" __init__.py       # förväntat: 1454
grep -n "Bug13A\] Preparing utan föregående Available" __init__.py   # förväntat: 2176
grep -n "def next_start_after" charge_planner.py      # förväntat: 52
grep -n "12:34:[12]" /config/ocpp_charger_debug.log* | grep -E "RemoteStop|Plan window active|RemoteStart|Bug13A"
```

Avvikande radnummer → stoppa och rapportera.

## Tester (`tests/test_bug61.py`, `coordinator_harness.py`)

Spärren:

1. STOP i öppet fönster → `_manual_stop_block` True; nästa `_update_smart_charging()` med `Preparing`, `transaction_id=None`, i fönster → ingen RemoteStart.
2. Samma läge utan STOP → RemoteStart skickas (regressionsskydd).
3. Spärrad, klockan passerar fönstrets slut (plan giltig, ej i fönster) → flaggan False, loggrad `[Bug61] Planfönstret är slut`.
4. Därefter nytt fönster öppnas → RemoteStart skickas.
5. Spärrad, plan `None` → flaggan ligger kvar.
6. Spärrad, plan räknas om så att fönstret förlängs över nuet → fortsatt spärrad.
7. Spärrad → `async_start_charging()` → flaggan False, RemoteStart skickas.
8. Spärrad → `Available`-kant → båda flaggorna False.
9. `_save_state()`/`_load_state()` bevarar båda flaggorna; Store utan nycklarna → False.

SOC-estimatet:

10. Laddning 8,06 kWh → STOP → `Finishing` → `StopTransaction` → `Preparing`: `_session_total_kwh == 8.06`.
11. Ytterligare ett `_check_notify_events()`-pass i `Preparing` → fortfarande 8,06 (ingen dubbelräkning).
12. Därefter START → `StartTransaction` nollar `state.energy_kwh` → `_charged_kwh` = 8,06 + ny energi.
13. Garo-reset utan manuellt stopp → ackumuleras som förut (Bug 33 oförändrad).
14. Notisbeteendet på `Preparing` efter STOP oförändrat (`Skippar Inkopplad – manuellt stopp`, ingen Inkopplad-notis).

Notiser:

15. STOP → `on_manual_stop` anropas en gång, `_cable_session_stop_notified` oförändrad.
16. `notify_on_stop=False` → ingen stopp-notis.
17. Efter STOP, laddning startar (auto eller START) → `on_charging_resumed` en gång; nästa Garo-reset ger ingen ny.
18. `notify_on_start=False` → ingen notis, flaggan nollas ändå.
19. STOP → urkoppling → ordinarie sammanfattningsnotis skickas, ingen "återupptagen".

## Deploy

`__init__.py` och `notifier.py` via `scp`, `ha core restart`. Ingen laddning pågående.

## Live-verifiering

1. Kabel i, Smart-läge, laddning i aktivt planfönster. Notera `Estimerad SOC`-raden. Tryck STOP.
2. Notis `Laddning stoppad` kommer direkt.
3. Graylog `"Bug61" OR "RemoteStartTransaction" OR "Auto-start spärrad"`: `[Bug61] Manuellt stopp …`, därefter ingen `RemoteStartTransaction` medan fönstret är öppet. Status `Preparing`, 0 W.
4. Nästa `Estimerad SOC`-rad har samma kWh som före STOP (inte nära noll). Laddfönster-grafen ändras inte.
5. Vid fönstrets slut: `[Bug61] Planfönstret är slut – auto-start tillåts igen`.
6. Nästa fönster: `Plan window active …, starting charge` och notis `Laddning återupptagen` (enligt plan).
7. Separat: STOP, sedan START inom samma fönster → laddningen går igång direkt, notis `Laddning återupptagen` (manuellt).

## Öppna punkter

1. Sidofynd 13:45: `Outside plan window (1 intervals), stopping` följt av auto-start 14 s senare. Den frysta sessionsplanen slutade 13:45, den omräknade gick till 14:15. Ingen del av den här specen.
