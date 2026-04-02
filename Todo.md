# Todo – OCPP Charger bugfixar (2026-03-14)

## Bug 1 – Målnivå stoppar inte laddningen i plan-läge
**Symptom:** Bilen laddade till 88% trots att målnivån var satt till 80%.

**Rotorsak:** `_update_smart_charging()` i plan-läget (`CHARGE_MODE_SMART` med feasible plan) kontrollerar bara om `now` är inom `plan.start–plan.end`. SOC- och kWh-målet ignoreras helt.

### Ändringar

**`__init__.py` – `_update_smart_charging()`**

Lägg till mål-check direkt efter att plan-läget konstaterats aktivt, innan fönsterkontrollen:

```python
if self.charge_mode == CHARGE_MODE_SMART and plan and plan.feasible and plan.start and plan.end:

    # Mål nått → stoppa omedelbart, oavsett planfönster
    soc = state.soc_percent
    soc_reached = soc is not None and self.target_soc > 0 and soc >= self.target_soc
    kwh_reached = self.target_kwh > 0 and state.energy_kwh >= self.target_kwh
    plan_energy_reached = plan.energy_kwh > 0 and state.energy_kwh >= plan.energy_kwh  # fallback om SOC drar ut

    if soc_reached or kwh_reached or plan_energy_reached:
        if state.charging:
            if soc_reached:
                reason = f"SOC {soc:.0f}% >= mål {self.target_soc:.0f}%"
            elif kwh_reached:
                reason = f"Energi {state.energy_kwh:.2f} kWh >= mål {self.target_kwh:.2f} kWh"
            else:
                reason = f"Energi {state.energy_kwh:.2f} kWh >= planens {plan.energy_kwh:.2f} kWh (SOC ej tillgänglig)"
            _LOGGER.info("[SmartCharge] Mål nått (%s), stoppar", reason)
            self.hass.async_create_task(self.ocpp.remote_stop_transaction())
        return

    in_window = plan.start <= now_utc <= plan.end
    # ... resten som idag
```

**`__init__.py` – `_update_charge_plan()`**

Skippa planering om målet redan är nått (förhindrar även onödiga dag/natt-notiser):

```python
soc = self.ocpp.state.soc_percent
soc_reached = soc is not None and self.target_soc > 0 and soc >= self.target_soc
kwh_reached = self.target_kwh > 0 and self.ocpp.state.energy_kwh >= self.target_kwh

if soc_reached or kwh_reached:
    _LOGGER.debug("[ChargePlanner] Mål redan nått, hoppar över planering")
    return
```

---

## Bug 2 – Notis-storm och felaktig sluttid

**Symptom:** Flera "Laddning startad"-notiser per session. Sluttiden i notisen baseras på ETA-beräkning istället för laddplanens sluttid.

### Ändringar

**`__init__.py` – ny bool-flagga i `__init__()`**

```python
self._start_notified_this_connection: bool = False
```

**`__init__.py` – `_check_notify_events()`**

- Nollställ `_start_notified_this_connection = False` när `status == "Available"` och vid ny `"Preparing"`.
- Ersätt villkoret `self._notified_start_session != state.session_id` med `not self._start_notified_this_connection`.
- Sätt `self._start_notified_this_connection = True` när start-notisen skickas.

Skicka med `plan_end` vid anropet till `on_charging_started`:

```python
plan = self.charge_plan
self.notifier.on_charging_started(
    soc_pct=state.soc_percent,
    current_a=state.current_a,
    power_kw=power_kw,
    plan_end=plan.end if plan and plan.feasible else None,
    estimated_end=self.estimated_completion,
)
```

**`notifier.py` – `on_charging_started()`**

Lägg till `plan_end`-parameter och prioritera den över `estimated_end`:

```python
def on_charging_started(
    self,
    soc_pct: float | None,
    current_a: float,
    power_kw: float,
    plan_end: datetime | None,       # från charge_plan.end om feasible
    estimated_end: datetime | None,  # ETA-fallback
) -> None:
    ...
    end_time = plan_end or estimated_end
    if end_time:
        lines.append(f"Beräknat klart: {_fmt_time(end_time)}")
```

---

## Bug 3 – Dag/natt-notis skickas trots att målnivån är nådd, och kan inte avbrytas

**Symptom:** Upprepade notiser om att dagladdning är billigare, trots att bilen redan laddats klart eller att användaren inte vill svara.

### Ändringar

**`const.py`** – ny action-konstant:

```python
NOTIFY_ACTION_DISMISS = "ocpp_dismiss_day_charging"
```

**`__init__.py` – ny bool-flagga i `__init__()`**

```python
self._day_charging_dismissed: bool = False
```

**`__init__.py` – `_handle_notification_action()`**

Hantera den nya actionen:

```python
elif action == NOTIFY_ACTION_DISMISS:
    _LOGGER.info("[Notify] User dismissed day/night choice")
    coordinator._day_charging_dismissed = True
    coordinator.set_allow_day_charging(False)
    coordinator._update_charge_plan()
    coordinator.async_set_updated_data(coordinator.ocpp.state)
```

**`__init__.py` – `_update_charge_plan()`**

Skydda `on_day_charging_chosen`-anropet:

```python
if notify and not self._day_charging_dismissed:
    self.notifier.on_day_charging_chosen(...)
```

**`__init__.py` – `_check_notify_events()`**

Återställ flaggan när kabeln kopplas ur (`status == "Available"`):

```python
self._day_charging_dismissed = False
```

**`notifier.py` – `on_day_charging_chosen()`**

Lägg till "Avsluta"-knapp i actions-listan:

```python
{"action": NOTIFY_ACTION_DISMISS, "title": "🚫 Avsluta"}
```

---

## Bug 4 – Gammal SOC i stopp-notisen

**Symptom:** När laddningen avslutas visas gammal SOC i notisen eftersom bilen inte hunnit rapportera uppdaterat värde.

**Rotorsak:** `on_charging_stopped` anropas direkt när `charging` flippar False. HA-entiteten för SOC kan ha fördröjning och hinner inte uppdateras innan notisen skickas.

### Ändringar

**`__init__.py` – `_check_notify_events()`**

Fördröj stopp-notisen med `async_call_later` (t.ex. 15 sekunder) för att ge SOC-entiteten tid att uppdateras:

```python
if (
    self._notify_on_stop
    and not is_charging
    and self._was_charging
    and self._notified_stop_session != state.session_id
    and self._notified_start_session == state.session_id
    ...
):
    self._notified_stop_session = state.session_id
    elapsed = self.elapsed_seconds or 0

    async def _send_stop_notif(_now=None):
        self._update_soc_from_ha()  # uppdatera SOC en gång till
        self.notifier.on_charging_stopped(
            soc_pct=self.ocpp.state.soc_percent,
            energy_kwh=state.energy_kwh,
            actual_cost_sek=state.accumulated_cost,
            duration_minutes=elapsed // 60,
        )

    async_call_later(self.hass, 15, _send_stop_notif)
```

---

## Bug 5 – Laddplan uppdateras inte direkt när target_soc/target_kwh/fordon ändras

**Symptom:** Efter att användaren ändrar målnivå (SOC eller kWh) eller byter aktivt fordon dröjer det upp till 5 minuter innan laddplanen räknas om, på grund av throttle i `_update_charge_plan()`.

**Rotorsak:** `_last_plan_update` nollställs inte vid dessa ändringar, så throttlen (`< 300s sedan senaste omräkning`) blockerar omräkning tills nästa 5-minutersfönster öppnar.

### Ändringar

**`__init__.py` – `set_target_soc()`**

```python
def set_target_soc(self, soc: float) -> None:
    self.target_soc = soc
    self._last_plan_update = None  # tvinga omräkning nästa cykel
    self.async_set_updated_data(self.ocpp.state)
```

**`__init__.py` – `set_target_kwh()`**

```python
def set_target_kwh(self, kwh: float) -> None:
    self.target_kwh = kwh
    self._last_plan_update = None  # tvinga omräkning nästa cykel
    self.async_set_updated_data(self.ocpp.state)
```

**`__init__.py` – `set_active_vehicle()`**

Lägg till nollställning av throttle efter befintlig logik:

```python
self._last_plan_update = None  # tvinga omräkning nästa cykel
```

---

## Bug 6 – Multi-vehicle-logiken väljer fel fordon när kabeln är inkopplad

**Symptom:** När bil kopplas in räknas laddplanen om baserat på fordonet med lägst SOC (t.ex. Kia eNiro soc=28% → target 60%) istället för det aktiva fordonet som faktiskt laddas. Resulterar i felaktig plan och completion time.

**Bevis från logg (2026-04-01 21:23:06):**
```
[ChargePlanner] Multi-vehicle: planning for Kia eNiro soc=28%
Planning: soc=28%→60% energy=2.48 kWh → plan: 04:00–04:15 (15 min)
```
Den inkopplade bilen var inte Kia eNiro, men multi-vehicle-logiken valde den ändå eftersom den hade lägst SOC.

**Rotorsak:** `_update_charge_plan()` använder alltid "lägst SOC bland alla fordon" vid multi-vehicle, oavsett om `cable_connected == True`. När kabeln är inkopplad borde det aktiva fordonet (`active_vehicle`) användas istället.

### Ändringar

**`__init__.py` – `_update_charge_plan()`**

Ändra multi-vehicle-grenen så att `active_vehicle` prioriteras när kabeln är inkopplad:

```python
if len(self._vehicles) > 1:
    if self.ocpp.state.cable_connected and self.active_vehicle:
        # Kabeln är inkopplad – använd det aktiva fordonet
        current_soc = self.ocpp.state.soc_percent or 0.0
        target_soc = float(self.active_vehicle.get("target_soc", 80.0))
        battery_capacity = float(self.active_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
        _LOGGER.debug("[ChargePlanner] Multi-vehicle: cable connected, planning for active vehicle %s soc=%.0f%%",
            self.active_vehicle.get(VEHICLE_NAME, "?"), current_soc)
    else:
        # Ingen bil inkopplad – välj fordon med lägst SOC för att visa kommande behov
        best_vehicle = None
        lowest_soc = float("inf")
        for v in self._vehicles:
            soc_ent = v.get(VEHICLE_SOC_ENTITY, "")
            soc_st = self.hass.states.get(soc_ent) if soc_ent else None
            try:
                v_soc = float(soc_st.state) if soc_st else float("inf")
            except (ValueError, TypeError):
                v_soc = float("inf")
            if v_soc < lowest_soc:
                lowest_soc = v_soc
                best_vehicle = v
        if best_vehicle:
            current_soc = lowest_soc if lowest_soc != float("inf") else 0.0
            target_soc = float(best_vehicle.get("target_soc", 80.0))
            battery_capacity = float(best_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
            _LOGGER.debug("[ChargePlanner] Multi-vehicle: no cable, planning for %s soc=%.0f%%",
                best_vehicle.get(VEHICLE_NAME, "?"), current_soc)
        else:
            current_soc = self.ocpp.state.soc_percent or 0.0
            target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
            battery_capacity = self.battery_capacity_kwh
```

---

## Bug 7 – Laddplan underskattar energi pga fel strömbegränsning i fallback (EJ BEKRÄFTAD)

**Symptom:** Laddplan beräknas till onormalt kort tid (15 min för ~20 kWh behov). Sett 2026-04-01 21:23 vid kabelinkoppling kl 21:22.

**Hypotes:** `power_kw`-fallbackvärdet i `_update_charge_plan()` beräknas med **aktuell** strömbegränsning via `schedule.current_limit()` (utan argument = just nu). Kl 21:23 är det dagtid (06:00–22:00) → 6A → 4.1 kW. Det planerade intervallet (04:00) är nattetid → borde vara 16A → 11 kW. Om `plan_cheapest_window` faller tillbaka på detta felaktiga värde för något intervall underskattas energin grovt.

```python
# Misstänkt rad i _update_charge_plan():
power_kw = (self.schedule.current_limit() * voltage * self.num_phases) / 1000.0
# Borde kanske vara schedule.current_limit_at(planned_interval_time)
# men det hanteras redan via schedule_fn – måste verifieras i logg
```

**⚠️ KRÄVER VERIFIERING** – kör detta nästa gång planen beter sig konstigt:

```bash
grep -E "ChargePlanner.*Planning|ChargePlanner.*Charge|Schedule.*Period|Schedule.*limit" /config/ocpp_charger_debug.log | grep -A5 "$(date +'%H:%M')"
```

Eller vid kabelinkoppling:
```bash
grep -E "(ChargePlanner|Schedule)" /config/ocpp_charger_debug.log | tail -20
```

**Rotorsak (om bekräftad):** `power_kw` används som fallback i `plan_cheapest_window` när `schedule_fn` inte ger ett värde. Fixa genom att beräkna fallback-power baserat på nattströmsgränsen, eller säkerställ att `schedule_fn` alltid används per intervall.

---

## Observation – Laddning i flera block trots Contiguous (2026-04-01→02, Kia eNiro)

**Vad hände:** Laddningen delades upp i minst tre separata OCPP-sessioner under natten, trots att Contiguous var valt. Varje session var i sig ett sammanhängande block — Contiguous-algoritmen fungerade korrekt per session. Problemet är att **varje ny session planeras om från scratch** baserat på återstående energibehov, vilket ger ett nytt (kortare) Contiguous-block istället för att hela nattens behov täcks i ett enda block från början.

**Observerade värden:**
- Session 1: `23:30–00:30`, 8.26 kWh laddades
- Plan efter session 1: `soc=28%→70% energy=9.44 kWh → 03:15–04:15`
- SOC fastnade på 28% hela natten (Kia Connect uppdaterar inte SOC under laddning)
- target_soc bytte från 60% → 70% mellan session 1 och 2 — oklart varför

**Öppna frågor som kräver debug-logg från nästa laddnatt:**

1. ✅ **Varför bytte target_soc från 60% → 70%?** Bekräftat: användaren ändrade manuellt till 70% kl 22:03 — korrekt beteende.

2. **Stämmer `already_charged_kwh`-avdraget?** Med 8.26 kWh laddat och target 70%: förväntat `energy_needed ≈ 20.9 kWh`, men loggat var `9.44 kWh`. Antingen är avdraget fel eller target/kapacitet är fel. Debug-raden visar detta.

3. **Nåddes målet?** Bilen laddade till kl 06:00 (deadline) — oklart om målet nåddes eller om laddningen avbröts av deadline.

**Grep för nästa analys:**
```bash
grep -E "ChargePlanner.*DEBUG battery|ChargePlanner.*Planning|already_charged|session_total" /config/ocpp_charger_debug.log | grep "$(date +'%Y-%m-%d')"
```

---

## Bug 8 – Felaktig energiberäkning vid planomräkning när SOC-entitet inte uppdateras

**Symptom:** När laddplanen räknas om mitt i natten (efter ett avslutat block) underskattas återstående energibehov kraftigt. Sett 2026-04-02 00:30: efter 8.26 kWh laddat räknades `energy_needed=9.44 kWh` (förväntat ~20.9 kWh).

**Rotorsak:** Kia Connect (och troligen andra bilintegrationer) uppdaterar inte SOC-entiteten under pågående laddning. `current_soc` läses från HA-entiteten och är alltid startvärdet (28%). Formeln:

```
energy_needed = (target_soc - current_soc) / 100 × capacity / efficiency - already_charged_kwh
```

ger fel resultat eftersom `current_soc` inte reflekterar faktisk SOC efter laddning. `already_charged_kwh` dras av men `current_soc` är oförändrat, vilket skapar inkonsekvens.

**Korrekt approach:** Estimera aktuell SOC utifrån startvärde + laddad energi:

```python
# I _update_charge_plan(), ersätt current_soc-läsning med estimerat värde:
if self._session_start_soc is not None and already_charged_kwh > 0:
    estimated_soc = self._session_start_soc + (
        already_charged_kwh * DEFAULT_CHARGE_EFFICIENCY / battery_capacity * 100.0
    )
    current_soc = min(estimated_soc, target_soc)
    _LOGGER.debug(
        "[ChargePlanner] Estimerad SOC: start=%.1f%% +%.2f kWh → %.1f%%",
        self._session_start_soc, already_charged_kwh, current_soc,
    )
# Sedan beräkna energy_needed utan already_charged_kwh-avdrag (det ingår redan i estimated_soc):
soc_needed = max(0.0, target_soc - current_soc)
energy_needed = (soc_needed / 100.0) * battery_capacity / DEFAULT_CHARGE_EFFICIENCY
```

**OBS:** När denna fix görs ska `already_charged_kwh`-avdraget i `energy_needed`-formeln **tas bort** — annars dubbelräknas den laddade energin.

**Verifieras med debug-raden** som lagts till i `_update_charge_plan()` — nästa laddnatt syns `battery_capacity/target_soc/current_soc/energy_needed` i loggen.

---

## Bug 9 – SOC låses till "ocpp"-källa och uppdateras inte från HA-entitet

**Symptom:** `sensor.ev_charger_*_battery_level` visar 28% under hela laddnatten trots att `sensor.e_niro_ev_battery_level` uppdaterades till 62% av Kia Connect.

**Rotorsak:** I `_update_soc_from_ha()` finns tidig return-logik som låser SOC-källan:

```python
if state.soc_percent is not None and self._soc_source == "ocpp":
    return  # ← returnerar utan att läsa HA-entiteten
if state.soc_percent is not None and self._soc_source != "ocpp":
    self._soc_source = "ocpp"  # ← låser källan till "ocpp"
    return
```

När `soc_percent` är satt (t.ex. 28% från OCPP vid sessionstart) och källan inte är `"ocpp"`, sätts källan om till `"ocpp"` och returnerar. Därefter matchar alltid första villkoret och HA-entiteten läses aldrig mer. Rad 564 som borde uppdatera från entitet nås aldrig:

```python
if entity_soc is not None and self._soc_source in ("entity", "none"):  # ← nås ej
```

**Fix:** OCPP-prioriteten ska bara gälla när OCPP *aktivt* skickar ett nytt SOC-mätvärde (det sker i OCPP-mätvärdeshanteraren). I `_update_soc_from_ha()` ska HA-entiteten alltid tillåtas uppdatera när den är tillgänglig. Ta bort den tidiga return-logiken och låt entitetsvärdet vinna när det finns:

```python
def _update_soc_from_ha(self) -> None:
    state = self.ocpp.state

    # Läs HA-entitet
    entity_soc: float | None = None
    if self.soc_entity:
        ha_state = self.hass.states.get(self.soc_entity)
        if ha_state and ha_state.state not in ("unavailable", "unknown", ""):
            try:
                val = float(ha_state.state)
                if self.soc_unit == SOC_UNIT_KWH and self.battery_capacity_kwh > 0:
                    val = (val / self.battery_capacity_kwh) * 100.0
                if val is not None and 0.0 <= val <= 100.0:
                    entity_soc = val
            except ValueError:
                pass

    # OCPP rapporterar aktivt SOC → högst prioritet, men uppdatera bara om OCPP-källan är färsk
    # (OCPP sätter _soc_source="ocpp" direkt i MeterValues-hanteraren när nytt värde anländer)
    if self._soc_source == "ocpp":
        return  # OCPP-värde nyss satt, behåll det

    # HA-entitet tillgänglig → använd alltid (även under laddning)
    if entity_soc is not None:
        if state.soc_percent != entity_soc:
            _LOGGER.debug("[SOC] Uppdaterar från HA-entitet: %.1f%% → %.1f%%",
                state.soc_percent or 0.0, entity_soc)
        state.soc_percent = entity_soc
        self._soc_source = "entity"
        # Spara som session-start-SOC om ingen finns
        if state.charging and self._session_start_soc is None:
            self._session_start_soc = entity_soc
        return

    # Fallback: estimera från energimätaren
    if self._session_start_soc is not None and self.battery_capacity_kwh > 0:
        added_kwh = state.energy_kwh * DEFAULT_CHARGE_EFFICIENCY
        estimated = self._session_start_soc + (added_kwh / self.battery_capacity_kwh * 100.0)
        state.soc_percent = min(100.0, round(estimated, 1))
        self._soc_source = "estimated"

    # Idle utan kabel: nollställ session-SOC
    if not state.charging and not state.cable_connected:
        self._session_start_soc = None
```

**OBS:** `_soc_source` måste återställas till `"none"` eller `"entity"` i OCPP-mätvärdeshanteraren *efter* att ett OCPP SOC-värde processats, så att nästa cykel inte fastnar i `"ocpp"`-låsningen om OCPP slutar rapportera SOC.

---

## Tidigare öppna punkter (verifierade i kod 2026-03-14)
- ✅ Auto-start baserat på laddplan – implementerat och korrekt
- ✅ Manuell start med grace period – implementerat och korrekt
- ✅ Imorgondagens priser → laddplan uppdateras automatiskt – implementerat och korrekt