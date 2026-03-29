# Todo – OCPP Charger bugfixar (2026-03-18)

## Övergripande sessionmodell (grund för Bug 2, 4, 5, 6)

En **kabelsession** sträcker sig från att kabeln kopplas in (`Preparing`) till att den
kopplas ur (`Available`). Inom en kabelsession kan det finnas flera OCPP-transaktioner
(ett per planfönster). Integrationen ska hantera detta korrekt:

```
Kabel in (Preparing)       → nollställ session_energy/cost, reset notis-flaggor
  OCPP tx 1 start          → skicka "Startad"-notis (en gång per kabelsession)
  OCPP tx 1 stopp          → ackumulera energi, ingen notis
  OCPP tx 2 start          → ingen ny "Startad"-notis
  OCPP tx 2 stopp          → ackumulera energi, ingen notis
  SuspendedEV / mål nått   → RemoteStop, force_update, 60s senare "Stoppad"-notis
Kabel ur (Available)       → session avslutas (notis om ej redan skickad)
```

---

## Bug 1 – Målnivå stoppar inte laddningen i plan-läge

**Symptom:** Bilen laddade till 88% trots att målnivån var satt till 80%.

**Rotorsak:** `_update_smart_charging()` kontrollerar bara om `now` är inom planfönstret.
SOC- och kWh-målet ignoreras helt.

### Ändringar

**`__init__.py` – `_update_smart_charging()`**

Lägg till mål-check direkt i början av plan-blocket, innan fönsterkontrollen:

```python
if self.charge_mode == CHARGE_MODE_SMART and plan and plan.feasible:

    soc = state.soc_percent
    soc_reached = soc is not None and self.target_soc > 0 and soc >= self.target_soc
    kwh_reached = self.target_kwh > 0 and state.energy_kwh >= self.target_kwh
    plan_energy_reached = (
        plan.energy_kwh > 0 and state.energy_kwh >= plan.energy_kwh
    )

    if soc_reached or kwh_reached or plan_energy_reached:
        if state.charging:
            if soc_reached:
                reason = f"SOC {soc:.0f}% >= mål {self.target_soc:.0f}%"
            elif kwh_reached:
                reason = f"Energi {state.energy_kwh:.2f} kWh >= mål {self.target_kwh:.2f} kWh"
            else:
                reason = f"Energi {state.energy_kwh:.2f} kWh >= planens {plan.energy_kwh:.2f} kWh"
            _LOGGER.info("[SmartCharge] Mål nått (%s), stoppar", reason)
            self.hass.async_create_task(self.ocpp.remote_stop_transaction())
        return

    in_window = plan.start <= now_utc <= plan.end
    # ... resten som idag
```

**`__init__.py` – `_update_charge_plan()`**

Skippa planering om målet redan är nått:

```python
soc = self.ocpp.state.soc_percent
if (soc is not None and self.target_soc > 0 and soc >= self.target_soc) or \
   (self.target_kwh > 0 and self.ocpp.state.energy_kwh >= self.target_kwh):
    _LOGGER.debug("[ChargePlanner] Mål redan nått, hoppar över planering")
    return
```

---

## Bug 2 – "Startad"-notis skickas vid varje OCPP-transaktion

**Symptom:** En ny "Startad"-notis per planfönster (tx 1, tx 2 osv) trots att det är
samma kabelsession. Sluttiden baseras på ETA-beräkning istället för planens sluttid.

**Rotorsak:** Notis-flaggan är kopplad till OCPP session_id, inte kabelsessionen.
Löses som en del av Bug 6 (kabelsession-modellen). Se den buggen för flaggorna.

### Ändringar

**`__init__.py` – `_check_notify_events()`**

Ersätt befintlig start-notis-logik med check mot `_cable_session_start_notified`:

```python
if (
    not self._cable_session_start_notified
    and is_charging
    and state.power_w > 100
):
    self._cable_session_start_notified = True
    plan = self.charge_plan
    self.notifier.on_charging_started(
        soc_pct=state.soc_percent,
        current_a=state.current_a,
        power_kw=state.power_w / 1000,
        plan_end=plan.end if plan and plan.feasible else None,
        estimated_end=self.estimated_completion,
    )
```

**`notifier.py` – `on_charging_started()`**

Lägg till `plan_end`-parameter, prioritera den över `estimated_end`:

```python
def on_charging_started(
    self,
    soc_pct: float | None,
    current_a: float,
    power_kw: float,
    plan_end: datetime | None,
    estimated_end: datetime | None,
) -> None:
    ...
    end_time = plan_end or estimated_end
    if end_time:
        lines.append(f"Beräknat klart: {_fmt_time(end_time)}")
```

---

## Bug 3 – Dag/natt-notis skickas trots att målnivån är nådd, och kan inte avbrytas

**Symptom:** Upprepade notiser om att dagladdning är billigare trots att bilen redan
laddats klart, eller när användaren inte vill svara.

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

```python
elif action == NOTIFY_ACTION_DISMISS:
    _LOGGER.info("[Notify] User dismissed day/night choice")
    coordinator._day_charging_dismissed = True
    coordinator.set_allow_day_charging(False)
    coordinator._update_charge_plan()
    coordinator.async_set_updated_data(coordinator.ocpp.state)
```

**`__init__.py` – `_update_charge_plan()`**

```python
if notify and not self._day_charging_dismissed:
    self.notifier.on_day_charging_chosen(...)
```

**`__init__.py` – `_check_notify_events()`**

Återställ vid kabelurkoppling:

```python
if state.status == "Available":
    self._day_charging_dismissed = False
```

**`notifier.py` – `on_day_charging_chosen()`**

```python
{"action": NOTIFY_ACTION_DISMISS, "title": "🚫 Avsluta"}
```

---

## Bug 4 – Gammal SOC i stopp-notisen

**Symptom:** Stopp-notisen visar gammal SOC eftersom bilen inte hunnit synka med
Kia UVO-molnet.

**Rotorsak:** Notisen skickas direkt när laddningen avslutas. Bilen behöver ca 60
sekunder för att synka tillståndet via molnet.

### Ändringar

**`__init__.py` – stopp-notis-hjälpfunktion**

Skapa en intern hjälpfunktion `_send_stop_notification()` som används av både
SuspendedEV-fallet (Bug 5) och kabelurkoppling (Bug 6). Den ska alltid:

1. Trigga biluppdatering omedelbart via `kia_uvo.force_update`.
2. Vänta 60 sekunder via `async_call_later` innan notisen skickas.
3. Hämta uppdaterad SOC via `_update_soc_from_ha()` precis innan notisen skickas.
4. Använda `_cable_session_energy_kwh` och `_cable_session_cost_sek` som värden.

```python
def _send_stop_notification(self) -> None:
    if self._cable_session_stop_notified:
        return
    self._cable_session_stop_notified = True

    self.hass.async_create_task(
        self.hass.services.async_call("kia_uvo", "force_update", {})
    )

    energy_kwh = self._cable_session_energy_kwh
    cost_sek = self._cable_session_cost_sek

    async def _delayed(_now=None):
        self._update_soc_from_ha()
        self.notifier.on_charging_stopped(
            soc_pct=self.ocpp.state.soc_percent,
            energy_kwh=energy_kwh,
            actual_cost_sek=cost_sek,
            duration_minutes=self._cable_session_elapsed_minutes(),
        )

    async_call_later(self.hass, 60, _delayed)
```

---

## Bug 5 – SuspendedEV avslutar inte laddningen

**Symptom:** När bilen når sin interna AC-laddningsgräns (t.ex. 90%) övergår
laddboxen till `SuspendedEV` med power=0W. Integrationen håller transaktionen öppen
och försöker sedan starta en ny (som rejected), vilket kapar sessionen och ger
felaktig energimätning.

**Rotorsak:** Två brister:
1. `SuspendedEV` hanteras inte som avslutskriterium.
2. Auto-start-checken ser `charging=False` och skickar `RemoteStartTransaction` trots
   att en transaktion redan är aktiv.

### Ändringar

**`__init__.py` – `__init__()`**

```python
self._suspended_ev_since: datetime | None = None
```

**`__init__.py` – `_update_smart_charging()`**

Lägg till SuspendedEV-guard i början av funktionen, efter mål-checken (Bug 1):

```python
if state.status == "SuspendedEV" and state.power_w < 100:
    if self._suspended_ev_since is None:
        self._suspended_ev_since = now
    elif (now - self._suspended_ev_since).total_seconds() >= 60:
        if state.charging:
            _LOGGER.info("[SmartCharge] SuspendedEV i >60s – bilen nöjd, avslutar")
            self.hass.async_create_task(self.ocpp.remote_stop_transaction())
            self._send_stop_notification()
        return
else:
    self._suspended_ev_since = None
```

**`__init__.py` – auto-start-check**

Lägg till guard mot aktiv transaktion:

```python
if self.ocpp.state.transaction_id is not None:
    _LOGGER.debug(
        "[SmartCharge] Transaktion redan aktiv (%s), hoppar över auto-start",
        self.ocpp.state.transaction_id,
    )
    return
```

---

## Bug 6 – session_energy/cost nollställs vid varje OCPP-transaktionsstopp

**Symptom:** Energi och kostnad nollställs vid varje planmässigt stopp. Energi
levererad i transaktion 2 och 3 registrerades aldrig. Stopp-notis skickades
felaktigt efter varje OCPP-transaktion.

**Rotorsak:** Integrationen likställer "session" med OCPP-transaktion. Korrekt
definition: session = kabel in → kabel ur.

### Ändringar

**`__init__.py` – `__init__()`**

```python
self._cable_session_energy_kwh: float = 0.0
self._cable_session_cost_sek: float = 0.0
self._cable_session_start_notified: bool = False
self._cable_session_stop_notified: bool = False
self._cable_session_start_time: datetime | None = None
```

**`__init__.py` – `_check_notify_events()` / statusövergång**

Nollställ vid övergång `Available → Preparing`:

```python
if prev_status == "Available" and state.status == "Preparing":
    self._cable_session_energy_kwh = 0.0
    self._cable_session_cost_sek = 0.0
    self._cable_session_start_notified = False
    self._cable_session_stop_notified = False
    self._cable_session_start_time = now
    _LOGGER.debug("[Session] Ny kabelsession – nollställer ackumulatorer")
```

Skicka stopp-notis vid kabelurkoppling om inte redan skickad:

```python
if state.status == "Available" and self._cable_session_energy_kwh > 0:
    self._send_stop_notification()
```

**`ocpp_client.py` – `handle_stop_transaction()`**

Ackumulera energi från laddboxens mätarvärden (tillförlitligaste källan) i stället
för att nollställa:

```python
tx_energy_kwh = (meter_stop - meter_start) / 1000.0
tx_cost_sek = <beräkna från pris och energi som idag>

coordinator._cable_session_energy_kwh += tx_energy_kwh
coordinator._cable_session_cost_sek += tx_cost_sek

_LOGGER.info(
    "[Session] OCPP tx avslutad: +%.2f kWh (totalt %.2f kWh denna kabelsession)",
    tx_energy_kwh,
    coordinator._cable_session_energy_kwh,
)
# Ta INTE bort/nollställ session_energy här längre
```

**`__init__.py` – `_cable_session_elapsed_minutes()`**

Lägg till hjälpmetod:

```python
def _cable_session_elapsed_minutes(self) -> int:
    if self._cable_session_start_time is None:
        return 0
    return int((dt_util.utcnow() - self._cable_session_start_time).total_seconds() / 60)
```

---

## Fältmappning – energi/kostnad (underlag från CC-genomgång 2026-03-18)

Baserat på genomgång av alla energi/kostnads-fält definieras här exakt vad som
ska förändras och vad som ska vara oförändrat.

### Fält som BEHÅLLS oförändrade

| Fält | Var | Motivering |
|------|-----|------------|
| `total_energy_kwh` | `ChargerState` | Laddboxens totala mätarställning – berörs inte |
| `total_cost` | `ChargerState` | Kumulativ kostnad alla sessioner – berörs inte |
| `session_energy_start` | `ChargerState` | Behövs för OCPP-transaktionsberäkning i `ocpp_client.py` |
| `plan.energy_kwh`, `plan.estimated_cost_sek` | `ChargePlan` | Planeringsdata – berörs inte |

### Fält som ÄNDRAR SEMANTIK

**`energy_kwh` i `ChargerState`**

Idag: nollställs vid `StartTransaction`, representerar innevarande OCPP-transaktion.
Efter: fortsätter representera innevarande OCPP-transaktion (oförändrat internt), men
används INTE längre direkt i notiser eller sensorer som "sessionsenergi". Sensorer och
notiser ska i stället läsa `coordinator._cable_session_energy_kwh`.

Berörs:
- `sensor.py` `SessionEnergySensor` → ändra till `coordinator._cable_session_energy_kwh`
- `__init__.py` `_handle_charging_stopped` → ändra till `_cable_session_energy_kwh`
- `__init__.py` `_fire_session_event()` → ändra till `_cable_session_energy_kwh`
- `__init__.py` notiser rad 844, 902 → ändra till `_cable_session_energy_kwh`
- `_update_cost()`, `_estimate_completion()`, `_update_smart_charging()`, `_check_target_reached()` → **behåll** `state.energy_kwh` (dessa jobbar mot pågående OCPP-transaktion, korrekt)

**`accumulated_cost` i `ChargerState`**

Idag: nollställs vid `StartTransaction`, representerar innevarande OCPP-transaktion.
Efter: fortsätter nollställas per OCPP-transaktion (används i `_update_cost()` för
löpande kostnadsberäkning), men används INTE längre i notiser eller sensorer.

Berörs:
- `sensor.py` `SessionCostSensor` → ändra till `coordinator._cable_session_cost_sek`
- `__init__.py` `_handle_charging_stopped` → ändra till `_cable_session_cost_sek`
- `__init__.py` `_fire_session_event()` → ändra till `_cable_session_cost_sek`
- `__init__.py` `_handle_cable_connected` (rad 1154) → **ta bort** nollställning härifrån, nollställs nu i kabelsessions-reset-blocket (`Available → Preparing`)
- `__init__.py` `_last_cost_energy_kwh` (rad 1155, 1181) → nollställ även dessa i kabelsessions-reset

### Fält som LÄGGS TILL

I `OCPPCoordinator.__init__()`:

```python
self._cable_session_energy_kwh: float = 0.0
self._cable_session_cost_sek: float = 0.0
self._cable_session_start_notified: bool = False
self._cable_session_stop_notified: bool = False
self._cable_session_start_time: datetime | None = None
self._suspended_ev_since: datetime | None = None
```

### Persistens

Lägg till i `_save_state()` / `_load_state()`:
```python
"cable_session_energy_kwh": self._cable_session_energy_kwh,
"cable_session_cost_sek": self._cable_session_cost_sek,
```
Så att energi/kostnad överlever en HA-omstart mitt i en kabelsession (t.ex. om HA
startas om under natten mellan planfönster 1 och 2).

---

## ✅ Fix 7 – Planeraren räknar om från noll efter varje delstopp (2026-03-20)

**Symptom:** Nattladdningen skapar 5–6 separata laddningssessioner istället för en
sammanhängande. Planeraren räknar om planen efter varje 30-min delstopp eftersom
`state.energy_kwh` nollställs vid varje ny OCPP-transaktion och SOC-entiteten inte
uppdateras under natten.

**Rotorsak:** `_update_charge_plan()` beräknar `energy_needed` utan att ta hänsyn
till redan laddad energi från tidigare deltransaktioner i samma kabelsession.

### Ändringar

**`__init__.py` – `__init__()`**

Nytt fält efter `_last_remote_start`:

```python
self._session_total_kwh: float = 0.0   # ackumulerad energi sedan kabeln kopplades in
```

**`__init__.py` – `_check_notify_events()` / Available-blocket**

Nollställ vid kabelurkoppling:

```python
if status == "Available":
    self._was_charging = False
    self._session_total_kwh = 0.0
```

**`__init__.py` – `_check_notify_events()` / Preparing-blocket**

Direkt EFTER befintliga nollställningar av `accumulated_cost` och `_last_cost_energy_kwh`:

```python
self._session_total_kwh += self.ocpp.state.energy_kwh  # spara föregående delsessions energi
```

**`__init__.py` – `_update_charge_plan()`**

Ersätt beräkning av `energy_needed`:

```python
# Innan:
energy_needed = (soc_needed / 100.0) * battery_capacity / DEFAULT_CHARGE_EFFICIENCY

# Efter:
already_charged_kwh = self._session_total_kwh + self.ocpp.state.energy_kwh
energy_needed = max(0.0, (soc_needed / 100.0) * battery_capacity / DEFAULT_CHARGE_EFFICIENCY - already_charged_kwh)
```

---

## ✅ Fix 8 – Dubbel RemoteStop inom sekunder (2026-03-20)

**Symptom:** Två update-cykler triggar ibland `remote_stop_transaction()` inom 1–2
sekunder av varandra. Ger dubblerade stopp-meddelanden i loggen.

**Rotorsak:** Ingen debounce på RemoteStop. Två `_async_update_data()`-cykler kan
båda nå stop-villkoret innan den första har hunnit ändra `state.charging`.

### Ändringar

**`__init__.py` – `__init__()`**

```python
self._last_remote_stop: datetime | None = None
```

**`__init__.py` – `_update_smart_charging()`**

På ALLA ställen där `remote_stop_transaction()` anropas (plan-window-blocket och
fallback-blocket), ersätt:

```python
self.hass.async_create_task(self.ocpp.remote_stop_transaction())
```

med:

```python
_now = datetime.now(timezone.utc)
if self._last_remote_stop and (_now - self._last_remote_stop).total_seconds() < 15:
    _LOGGER.debug("[SmartCharge] Dubbel-stop guardad (%.1fs sedan senaste)",
                  (_now - self._last_remote_stop).total_seconds())
else:
    self._last_remote_stop = _now
    self.hass.async_create_task(self.ocpp.remote_stop_transaction())
```

---

## ✅ Fix 9 – Upprepad "Inkopplad"-notis under natt-cykeln (2026-03-20)

**Symptom:** "Inkopplad"-notisen skickas vid varje OCPP-delsession (Preparing) under
natten, inte bara en gång per kabelinkoppling.

**Rotorsak:** `_notified_connect_session` jämförs mot `state.session_id`, som ändras
vid varje ny OCPP-transaktion. Lösningen är en enkel bool-flagga per kabelsession.

### Ändringar

**`__init__.py` – `__init__()`**

```python
self._cable_session_notified_connect: bool = False
```

**`__init__.py` – `_check_notify_events()` / Available-blocket**

```python
if status == "Available":
    self._was_charging = False
    self._session_total_kwh = 0.0
    self._cable_session_notified_connect = False
```

**`__init__.py` – `_check_notify_events()` / Preparing-villkoret**

Ersätt:
```python
and self._notified_connect_session != state.session_id
```
med:
```python
and not self._cable_session_notified_connect
```

I samma block, lägg till efter `self._notified_connect_session = state.session_id`:
```python
self._cable_session_notified_connect = True
```

---

## ✅ Fix 10 – Periodisk SOC-omläsning de första 30 minuterna efter inkoppling (2026-03-20)

**Symptom:** Bilappen uppdaterar SOC med fördröjning efter körning. Planeraren
beräknar `energy_needed` från gammal SOC som råkade vara i HA vid inkoppling.

**Rotorsak:** SOC-entiteten läses bara en gång vid Preparing. Om bilappen uppdaterar
SOC 5–10 minuter senare hinner inte planen justeras.

### Ändringar

**`__init__.py` – `__init__()`**

```python
self._cable_connect_time: datetime | None = None
self._soc_at_connect: float | None = None
self._soc_reread_done: bool = False
```

**`__init__.py` – `_check_notify_events()` / Preparing-blocket**

Sätter `_cable_connect_time`, `_soc_at_connect`, `_soc_reread_done = False`.

**`__init__.py` – `_check_notify_events()` / Available-blocket**

Nollställer `_cable_connect_time = None`, `_soc_reread_done = True`.

**`__init__.py` – `_check_soc_reread()` (ny metod)**

- Körs varje update-cykel (10s) under 30 min efter Preparing
- Avbryter vid `charging == True`
- Läser SOC-entiteten direkt, konverterar kWh→% om nödvändigt
- Om ΔSoC ≥ 5 pp → uppdaterar `soc_percent`, nollställer `_last_plan_update`, anropar `_update_charge_plan()`

**`__init__.py` – `_async_update_data()`**

Anrop `self._check_soc_reread()` direkt efter `self._update_soc_from_ha()`.

---

## Verifiering efter deploy (Fix 7–10)

```bash
grep -E "session_total|Dubbel-stop|cable_session|Fördröjd SOC|Omläsning|Plan window active|Outside plan|RemoteStart|RemoteStop" /config/ocpp_charger_debug.log | tail -50
```

Förväntat resultat i natt: en enda "Inkopplad"-notis, planeraren räknar ned
`energy_needed` successivt, och laddningen slutar när målet är nått istället för
att starta om.

---

## Tidigare öppna punkter (verifierade i kod 2026-03-14)
- ✅ Auto-start baserat på laddplan – implementerat och korrekt
- ✅ Manuell start med grace period – implementerat och korrekt
- ✅ Imorgondagens priser → laddplan uppdateras automatiskt – implementerat och korrekt