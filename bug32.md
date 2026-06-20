# Bug 32 – Select-entiteter uppdateras inte vid extern statusändring

**Status:** ✅ Åtgärdad + deployad till live HA 2026-06-20

> **Implementerat enligt rapport** (radnummer verifierade mot skarp kod först, identiskt med
> deployad): alla tre select-klasser ärver nu `CoordinatorEntity, SelectEntity` och anropar
> `super().__init__(coordinator)`. `CoordinatorEntity` importeras. `current_option` /
> `extra_state_attributes` / `async_select_option` orörda. Samma mönster som switcharna redan
> använder. Notis-handlern lyssnar på `mobile_app_notification_action` (`__init__.py:189`).

## Symptom

När aktivt fordon byts via en knapp i en push-notis (eller av annan extern
trigger som inte går via selectorns egen `async_select_option()`) uppdateras
**inte** `select.ev_charger_garocs_48671aa056e80_active_vehicle` i Home
Assistant. Backend byter korrekt fordon internt (syns i debug-loggen,
`[Notify] User selected vehicle: ...` och `[Vehicle] Switched to ...`), men
ingen `state_changed`-händelse syns i logboken för select-entiteten och
dashboarden fortsätter visa det gamla fordonet.

Om användaren däremot väljer fordon **direkt i selectorn** i dashboarden
fungerar det – entitetens state uppdateras omedelbart och syns i logboken
(bekräftat: `select.select_option` → state ändrat till "Skoda Enyaq",
loggat i logbook).

Samma strukturella brist gäller `ChargeModeSelect` och
`PlannerAlgorithmSelect` – de har inte observerats buggiga ännu, men har
identisk kod-struktur och kommer fallera på samma sätt om laddläge eller
planeringsalgoritm någonsin sätts från annat håll än entitetens egen
`async_select_option()` (t.ex. en framtida notis, tjänst eller automation).

---

## Rotorsak

I `select.py` ärver alla tre select-klasser bara från `SelectEntity`:

```python
class ActiveVehicleSelect(SelectEntity):
class ChargeModeSelect(SelectEntity):
class PlannerAlgorithmSelect(SelectEntity):
```

Jämför med `switch.py`, där samtliga switchar ärver
`class XSwitch(CoordinatorEntity, SwitchEntity):` och anropar
`super().__init__(coordinator)` i `__init__()`. Det gör att de automatiskt
prenumererar på koordinatorns uppdateringar via
`coordinator.async_add_listener()`, så att varje
`coordinator.async_set_updated_data(...)` triggar `async_write_ha_state()`
på dem direkt – oavsett varifrån ändringen kom.

Select-entiteterna saknar detta arv helt. Deras `current_option`-properties
läser visserligen alltid live från koordinatorn (`self._coordinator.active_vehicle`
m.fl.), så värdet är aldrig "fel" i sig – men **ingenting talar om för Home
Assistant att det är dags att läsa om och skriva ut det nya värdet**, om inte
ändringen kom via entitetens egen service-anropade `async_select_option()`.

Flödet i `__init__.py` (notis-handlern, rad ~173–185) gör:

```python
coordinator.set_active_vehicle(vehicle)
coordinator._update_charge_plan()
coordinator.async_set_updated_data(coordinator.ocpp.state)
```

`async_set_updated_data()` är exakt mekanismen som skulle lösa detta – men
bara för entiteter som är `CoordinatorEntity`. Select-entiteterna missar
alltså tåget helt och förlitar sig istället på Home Assistants
standardpolling (normalt var ~30:e sekund), vilket upplevs som att "inget
händer".

---

## Önskat beteende

Alla tre select-entiteter ska reagera omedelbart på
`coordinator.async_set_updated_data()`, oavsett vad som triggade ändringen –
notis, automation, tjänsteanrop eller den egna selectorn i dashboarden.

---

## Fix: lägg till `CoordinatorEntity`-arv

### Berörd fil

`/config/custom_components/ocpp_charger/select.py`

### 1. Nytt import

```python
from homeassistant.helpers.update_coordinator import CoordinatorEntity
```

### 2. `ChargeModeSelect`

Före:
```python
class ChargeModeSelect(SelectEntity):
    """Select the charging mode."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
```

Efter:
```python
class ChargeModeSelect(CoordinatorEntity, SelectEntity):
    """Select the charging mode."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
```

### 3. `ActiveVehicleSelect`

Före:
```python
class ActiveVehicleSelect(SelectEntity):
    """Select which vehicle is currently connected to the charger.
    ...
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
```

Efter:
```python
class ActiveVehicleSelect(CoordinatorEntity, SelectEntity):
    """Select which vehicle is currently connected to the charger.
    ...
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
```

### 4. `PlannerAlgorithmSelect`

Före:
```python
class PlannerAlgorithmSelect(SelectEntity):
    """Select the charge planning algorithm."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry  # Bug 11: needed for persisting to entry.data
```

Efter:
```python
class PlannerAlgorithmSelect(CoordinatorEntity, SelectEntity):
    """Select the charge planning algorithm."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry  # Bug 11: needed for persisting to entry.data
```

Inget annat ändras i klasserna. `current_option`/`extra_state_attributes`/
`async_select_option` lämnas orörda – de läser redan live från koordinatorn,
problemet var enbart avsaknaden av push-prenumeration.

---

## Verifiering CC ska köra mot skarp kod (192.168.1.97) INNAN implementation

Den uppladdade kopian av `select.py` kan ha driftat från deployad kod.
Bekräfta först exakt innehåll och radnummer:

```bash
grep -n -E "class (ChargeModeSelect|ActiveVehicleSelect|PlannerAlgorithmSelect)|def __init__|self\._coordinator = coordinator|^from homeassistant" \
  /config/custom_components/ocpp_charger/select.py
```

Bekräfta att `CoordinatorEntity` inte redan importeras (annars dubbel-import):

```bash
grep -n "CoordinatorEntity" /config/custom_components/ocpp_charger/select.py
```

Justera radnummer/kontext efter faktiskt utfall innan ändringarna görs.

---

## Verifiering efter implementation

1. Anslut kabeln så notisen "välj fordon" triggas (eller tvinga fram den
   manuellt om det finns en tjänst för det).
2. Tryck på det fordon i notisen som **inte** redan är valt.
3. Kontrollera **omedelbart** (inom någon sekund, inte efter 30 s polling):

```bash
grep -iE "User selected vehicle|Switched to" /config/ocpp_charger_debug.log | tail -5
```

4. Kontrollera i HA **Logbook** att
   `select.ev_charger_garocs_48671aa056e80_active_vehicle` fick en
   `state_changed`-händelse med samma tidsstämpel som notis-klicket (inte
   först vid nästa polling-cykel).
5. Kontrollera att dashboardens "Aktivt fordon"-väljare visar rätt fordon
   direkt, utan att behöva uppdatera sidan.
6. Regressionstest: byt fordon manuellt via selectorn i dashboarden – ska
   fortsätta fungera precis som innan (logbook-post + korrekt state).
7. Regressionstest: byt `Charging Mode` och `Planning Algorithm` manuellt –
   ska fortsätta fungera som innan (dessa hade inget känt externt
   trigger-flöde, så regressionen är huvudsakligen att verifiera att inget
   gick sönder).

---

## Interaktion med tidigare buggar

| Bug | Påverkan |
|-----|----------|
| Bug 11 (PlannerAlgorithmSelect persistens till entry.data) | Oförändrad – persistens-logiken i `async_select_option` rörs inte |
| Bug 26 (persistens av coordinator-variabler) | Orelaterad – detta är ett entity-push-problem, inte ett state-persistens-problem |

---

## Berörda filer

- `select.py` – nytt import (`CoordinatorEntity`) + ändrat klassarv och
  `__init__()` för `ChargeModeSelect`, `ActiveVehicleSelect` och
  `PlannerAlgorithmSelect`.

Inga ändringar i `__init__.py`, `const.py` eller andra filer.
