"""Feature 10 – koordinatorn: sensorbaserad bilidentifiering vid kabelanslutning.

Riktig OCPPCoordinator (tests/coordinator_harness.py). Fejkat: hass.states (sensorernas råstate), tid
(async_call_later), sensorlyssnaren (async_track_state_change_event), SoC-identifieringen (identify_vehicle – räknas)
och notifieraren. Scenarionumren följer claude_feature10.md § Verifiering.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_feature10_coordinator.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
ENYAQ_PLUG = "binary_sensor.skoda_enyaq_charger_connected"
NIRO_PLUG = "binary_sensor.e_niro_ev_battery_plug"

# Fordonsordning som i driftsatt konfiguration: eNiro först (= aktiv i en färsk koordinator), båda med sensor
NIRO = {"name": "Kia eNiro", "capacity_kwh": 64.0, "soc_entity": "sensor.e_niro_ev_battery_level",
        "soc_unit": "percent", "max_current_a": 0, "plug_entity": NIRO_PLUG}
ENYAQ = {"name": "Skoda Enyaq", "capacity_kwh": 77.0, "soc_entity": "sensor.skoda_enyaq_battery_percentage",
         "soc_unit": "percent", "plug_entity": ENYAQ_PLUG}
VEHICLES = [NIRO, ENYAQ]

QUIET = ("_update_soc_from_ha", "_update_charge_plan", "async_set_updated_data")   # planering/SOC-läsning/push


def _without_plug(vehicle):
    return {k: v for k, v in vehicle.items() if k != "plug_entity"}


def _module():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    import custom_components.ocpp_charger as mod
    return mod


class _State:
    def __init__(self, state):
        self.state = state


class Wires:
    """Fejkad tid och sensorlyssnare. Handtagen registreras så att testet kan avfyra dem och se om de avbröts."""

    def __init__(self):
        self.timers = []       # {"delay", "action", "cancelled"}
        self.listeners = []    # {"entities", "action", "unsubbed"}

    def call_later(self, hass, delay, action):
        timer = {"delay": delay, "action": action, "cancelled": False}
        self.timers.append(timer)

        def cancel():
            timer["cancelled"] = True
        return cancel

    def track(self, hass, entity_ids, action):
        listener = {"entities": list(entity_ids), "action": action, "unsubbed": False}
        self.listeners.append(listener)

        def unsub():
            listener["unsubbed"] = True
        return unsub


class SocSpy:
    """Ersätter identify_vehicle (SoC-logiken): räknar anrop och väljer vehicles[0] – 'SoC-fallbacken'."""

    def __init__(self):
        self.calls = 0

    def __call__(self, vehicles, ocpp_soc, hass):
        self.calls += 1
        return vehicles[0], "SoC fallback"


@contextmanager
def _patched():
    mod = _module()
    wires, soc = Wires(), SocSpy()
    with patch.object(mod, "async_call_later", wires.call_later), \
            patch.object(mod, "async_track_state_change_event", wires.track), \
            patch.object(mod, "identify_vehicle", soc):
        yield wires, soc


def _coordinator(states, *, vehicles=VEHICLES, wait=None):
    """Riktig koordinator; hass.states.get ger `states` (entity_id → råstate, saknad = finns inte)."""
    c = h.silence(h.make_coordinator(vehicles=vehicles), *QUIET)
    c.hass.states.get.side_effect = lambda eid: _State(states[eid]) if eid in states else None
    c.notifier = MagicMock()
    if wait is not None:
        c.entry.data["plug_wait_seconds"] = wait
    return c


def _connect(c):
    """Kabeln kopplas in: poll-cykeln ser Available → Preparing."""
    c._last_connector_status = "Available"
    c.ocpp.state.connector_status = "Preparing"
    c._check_vehicle_auto_detect()


def _cable_out(c):
    """Kabeln dras ur: OCPP-uppdateringen kör den riktiga _check_notify_events (Available-grenen städar)."""
    async def run():
        c.ocpp.state.connector_status = "Available"
        c._check_notify_events()
        await asyncio.sleep(0)   # låt _save_state()-tasken gå klart
    asyncio.run(run())


# ── beslutstabellen genom koordinatorn ───────────────────────────────────────────────────────────────────────────

def test_scenario1_one_plugged_and_all_covered_selects_that_vehicle_without_notification():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        assert c.active_vehicle["name"] == "Kia eNiro"

        _connect(c)

        assert c.active_vehicle["name"] == "Skoda Enyaq"
        assert ENYAQ_PLUG in c._last_detection_reason
        assert soc.calls == 0
        c.notifier.on_vehicle_selection_needed.assert_not_called()
        assert wires.timers == [] and wires.listeners == []


def test_scenario2_the_other_vehicle_is_selected_too():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "on"})
        c.active_vehicle = c._vehicles[1]          # Enyaq var aktiv sedan förra gången

        _connect(c)

        assert c.active_vehicle["name"] == "Kia eNiro"
        assert soc.calls == 0
        c.notifier.on_vehicle_selection_needed.assert_not_called()


def test_a_match_records_the_sensor_reason_even_when_the_vehicle_does_not_change():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "on"})   # eNiro är redan aktiv

        _connect(c)

        assert c.active_vehicle["name"] == "Kia eNiro"
        assert NIRO_PLUG in c._last_detection_reason


def test_scenario3_two_plugged_notifies_at_once_with_the_soc_fallback_active():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "on"})

        _connect(c)

        assert soc.calls == 1
        c.notifier.on_vehicle_selection_needed.assert_called_once_with("multiple_plugged", c._vehicles, "Kia eNiro")
        assert wires.timers == [] and wires.listeners == []


def test_scenario4_a_sensor_turning_on_within_the_wait_selects_that_vehicle():
    with _patched() as (wires, soc):
        states = {ENYAQ_PLUG: "off", NIRO_PLUG: "off"}
        c = _coordinator(states)

        _connect(c)

        assert soc.calls == 1                                    # SoC-fallbacken gäller under väntan
        assert [t["delay"] for t in wires.timers] == [90]        # standardvärdet
        assert sorted(wires.listeners[0]["entities"]) == sorted([ENYAQ_PLUG, NIRO_PLUG])
        c.notifier.on_vehicle_selection_needed.assert_not_called()

        states[ENYAQ_PLUG] = "on"                                # molnsensorn hinner ikapp
        wires.listeners[0]["action"](MagicMock())

        assert c.active_vehicle["name"] == "Skoda Enyaq"
        assert wires.timers[0]["cancelled"] and wires.listeners[0]["unsubbed"]
        c.notifier.on_vehicle_selection_needed.assert_not_called()


def test_a_sensor_change_that_still_shows_nothing_keeps_waiting():
    with _patched() as (wires, soc):
        states = {ENYAQ_PLUG: "off", NIRO_PLUG: "off"}
        c = _coordinator(states)
        _connect(c)

        states[ENYAQ_PLUG] = "unavailable"
        wires.listeners[0]["action"](MagicMock())

        assert not wires.timers[0]["cancelled"] and not wires.listeners[0]["unsubbed"]
        c.notifier.on_vehicle_selection_needed.assert_not_called()


def test_a_second_plugged_sensor_during_the_wait_notifies_at_once_and_stops_waiting():
    with _patched() as (wires, soc):
        states = {ENYAQ_PLUG: "off", NIRO_PLUG: "off"}
        c = _coordinator(states)
        _connect(c)

        states[ENYAQ_PLUG] = states[NIRO_PLUG] = "on"
        wires.listeners[0]["action"](MagicMock())

        c.notifier.on_vehicle_selection_needed.assert_called_once_with("multiple_plugged", c._vehicles, "Kia eNiro")
        assert wires.timers[0]["cancelled"] and wires.listeners[0]["unsubbed"]


def test_scenario5_nothing_plugged_in_within_the_wait_notifies_none_plugged():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
        _connect(c)

        wires.timers[0]["action"](None)                          # tidsgränsen går ut

        c.notifier.on_vehicle_selection_needed.assert_called_once_with("none_plugged", c._vehicles, "Kia eNiro")
        assert wires.listeners[0]["unsubbed"]                    # lyssnaren lever bara under väntefönstret
        assert soc.calls == 1


def test_scenario6_only_one_vehicle_has_a_sensor_notifies_partial_at_once():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on"}, vehicles=[_without_plug(NIRO), ENYAQ])

        _connect(c)

        assert soc.calls == 1
        c.notifier.on_vehicle_selection_needed.assert_called_once_with("partial_sensors", c._vehicles, "Kia eNiro")
        assert wires.timers == [] and wires.listeners == []


def test_scenario7_no_sensor_configured_is_exactly_todays_behaviour():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "on"}, vehicles=[_without_plug(NIRO), _without_plug(ENYAQ)])

        _connect(c)

        assert soc.calls == 1
        c.notifier.on_vehicle_selection_needed.assert_not_called()
        assert wires.timers == [] and wires.listeners == []


def test_scenario8_an_unavailable_sensor_is_partial_coverage():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "unavailable", NIRO_PLUG: "on"})

        _connect(c)

        c.notifier.on_vehicle_selection_needed.assert_called_once_with("partial_sensors", c._vehicles, "Kia eNiro")


def test_scenario11_zero_wait_notifies_at_once_without_a_timer_or_listener():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"}, wait=0)

        _connect(c)

        c.notifier.on_vehicle_selection_needed.assert_called_once_with("none_plugged", c._vehicles, "Kia eNiro")
        assert wires.timers == [] and wires.listeners == []


def test_scenario12_the_wait_time_is_read_on_every_connection():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"}, wait=5)
        _connect(c)

        c.entry.data["plug_wait_seconds"] = 90     # options-ändring, ingen omstart
        _connect(c)

        assert [t["delay"] for t in wires.timers] == [5, 90]
        assert wires.timers[0]["cancelled"]        # en ny anslutning ersätter ett ännu levande fönster


def test_the_listener_tracks_only_configured_sensors():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off"}, vehicles=[_without_plug(NIRO), ENYAQ])

        _connect(c)

        assert wires.listeners[0]["entities"] == [ENYAQ_PLUG]


def test_the_notification_is_sent_at_most_once_per_cable_session():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "on"})

        c._send_vehicle_selection("multiple_plugged")
        c._send_vehicle_selection("multiple_plugged")

        c.notifier.on_vehicle_selection_needed.assert_called_once()


def test_bad_wait_settings_fall_back_to_the_default():
    with _patched():
        c = _coordinator({})
        assert c._plug_wait_seconds() == 90
        for raw, expected in ((0, 0), (30.0, 30), ("45", 45), (None, 90), ("abc", 90), (-5, 0)):
            c.entry.data["plug_wait_seconds"] = raw
            assert c._plug_wait_seconds() == expected, raw


def test_the_existing_preconditions_still_apply():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        c.auto_vehicle_detection = False
        _connect(c)
        assert c.active_vehicle["name"] == "Kia eNiro" and soc.calls == 0 and wires.timers == []

        single = _coordinator({ENYAQ_PLUG: "on"}, vehicles=[ENYAQ])       # < 2 bilar
        _connect(single)
        assert soc.calls == 0 and wires.timers == [] and single.active_vehicle["name"] == "Skoda Enyaq"


# ── städning ─────────────────────────────────────────────────────────────────────────────────────────────────────

def test_scenario10_cable_out_during_the_wait_cancels_everything_and_sends_nothing():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
        _connect(c)
        assert not wires.timers[0]["cancelled"]

        _cable_out(c)

        assert wires.timers[0]["cancelled"] and wires.listeners[0]["unsubbed"]
        c.notifier.on_vehicle_selection_needed.assert_not_called()
        assert c._vehicle_manually_chosen is False and c._selection_notified is False


def test_cable_out_clears_an_outstanding_selection_notification_and_the_flags():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "on"})
        _connect(c)                                       # notis skickad (multiple_plugged)
        c._vehicle_manually_chosen = True

        _cable_out(c)

        c.notifier.clear_vehicle_selection_notification.assert_called_once()
        assert c._vehicle_manually_chosen is False and c._selection_notified is False


def test_async_stop_cancels_the_wait_window():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
        _connect(c)
        c.ocpp.stop = AsyncMock()

        asyncio.run(c.async_stop())

        assert wires.timers[0]["cancelled"] and wires.listeners[0]["unsubbed"]


# ── användarens val ──────────────────────────────────────────────────────────────────────────────────────────────

def test_scenario9_the_users_choice_wins_and_nothing_overwrites_it():
    with _patched() as (wires, soc):
        states = {ENYAQ_PLUG: "on", NIRO_PLUG: "on"}
        c = _coordinator(states)
        _connect(c)                                       # multiple_plugged → notis, SoC-fallbacken (eNiro) aktiv
        c.notifier.on_vehicle_selection_needed.assert_called_once()

        chosen = c._vehicles[1]
        c.set_active_vehicle(chosen)                      # det åtgärdshanteraren gör först
        c.on_vehicle_chosen_by_user(chosen)               # …och sedan detta

        assert c.active_vehicle is chosen and c._vehicle_manually_chosen is True
        assert "Manually selected" in c._last_detection_reason and "Skoda Enyaq" in c._last_detection_reason
        c.notifier.clear_vehicle_selection_notification.assert_called_once()

        # en ny identifiering i samma kabelsession (här skulle sensorerna annars peka ut eNiro) får inte skriva över valet
        states[ENYAQ_PLUG] = "off"
        soc_calls = soc.calls
        _connect(c)
        assert c.active_vehicle is chosen and soc.calls == soc_calls


def test_a_choice_during_the_wait_cancels_it_and_a_stale_timer_does_nothing():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
        _connect(c)

        c.on_vehicle_chosen_by_user(c._vehicles[1])

        assert wires.timers[0]["cancelled"] and wires.listeners[0]["unsubbed"]
        c.notifier.clear_vehicle_selection_notification.assert_not_called()   # ingen notis hade skickats
        wires.timers[0]["action"](None)                                        # en försenad avfyrning
        c.notifier.on_vehicle_selection_needed.assert_not_called()


def test_the_select_vehicle_action_calls_on_vehicle_chosen_by_user():
    """Closuren i async_setup_entry går inte att bygga utan en hel hass: kontrollera kopplingen i källkoden."""
    src = (ROOT / "custom_components" / "ocpp_charger" / "__init__.py").read_text(encoding="utf-8")
    start = src.index("elif action.startswith(NOTIFY_ACTION_SELECT_VEHICLE):")
    branch = src[start:src.index("entry.async_on_unload(", start)]
    assert "coordinator.on_vehicle_chosen_by_user(vehicle)" in branch
    assert branch.index("coordinator.set_active_vehicle(vehicle)") < branch.index("coordinator.on_vehicle_chosen_by_user(vehicle)")


def test_a_tap_before_the_poll_edge_is_not_overwritten_by_identification():
    """Samma kabelsession: 'Laddkabel inkopplad' skickas från händelsevägen vid Preparing, medan identifieringen körs på
    den senare poll-kanten. Trycker användaren på en bil emellan ska identifieringen inte skriva över valet."""
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})    # sensorerna pekar ut Enyaq
        c.ocpp.state.connector_status = "Preparing"               # Preparing-händelsen har kommit, kanten inte än
        chosen = c._vehicles[0]                                   # användaren trycker på eNiro (aktiv sedan start)

        c.set_active_vehicle(chosen)
        c.on_vehicle_chosen_by_user(chosen)
        assert c._vehicle_manually_chosen is True

        _connect(c)                                               # poll-kanten Available → Preparing

        assert c.active_vehicle is chosen
        assert soc.calls == 0
        c.notifier.on_vehicle_selection_needed.assert_not_called()
        assert wires.timers == [] and wires.listeners == []


def test_a_tap_while_the_cable_is_out_does_not_suppress_the_next_identification():
    """En kvarliggande 'Laddkabel inkopplad'-notis kan tryckas på efter urkoppling. Inget rensar flaggan före nästa
    Preparing (inga OCPP-uppdateringar utan kabel), så att armera den skulle tysta identifieringen – även SoC-
    fallbacken – för hela nästa session."""
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "off"})
        c.ocpp.state.connector_status = "Available"               # kabeln är ur
        stale = c._vehicles[0]

        c.set_active_vehicle(stale)                               # åtgärdshanteraren gör detta först
        c.on_vehicle_chosen_by_user(stale)

        assert c._vehicle_manually_chosen is False
        assert "Manually selected" in c._last_detection_reason    # valet loggas ändå

        _connect(c)                                               # nästa inkoppling

        assert c.active_vehicle["name"] == "Skoda Enyaq"          # sensorn avgör som vanligt
        assert ENYAQ_PLUG in c._last_detection_reason


def test_the_wait_callbacks_are_event_loop_callbacks():
    """Utan @callback kör HA metoden i en executor-tråd och koordinatorns tillstånd rörs från fel tråd (plan Decision 5).
    Testerna anropar callbackarna direkt, så bara detta test fångar ett borttaget dekorationsrad."""
    _module()   # SKIP utan HA
    from homeassistant.core import is_callback
    with _patched():
        c = _coordinator({})
        assert is_callback(c._on_plug_sensor_change)
        assert is_callback(c._on_plug_wait_timeout)


# ── Bug: on_cable_connected's egen vals-notis rensades aldrig ──────────────────────────────────────────────────────

def test_a_manual_choice_also_dismisses_the_legacy_cable_connected_notification():
    """[Bug] on_cable_connected skickar samma fordonsknappar (tag ocpp_cable_connected) som Feature 10:s egen
    vals-notis, men bara den senare rensades vid ett val – den förra blev liggande kvar för alltid (till nästa
    kabelsession skriver över den, tidigast timmar senare). Live-observerat 2026-09-22: användaren fick båda
    notiserna och den äldre försvann inte trots ett lyckat val."""
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "on", NIRO_PLUG: "on"})
        _connect(c)   # multiple_plugged → Feature 10-notisen skickad

        chosen = c._vehicles[0]
        c.set_active_vehicle(chosen)
        c.on_vehicle_chosen_by_user(chosen)

        c.notifier.dismiss_cable_connected_notification.assert_called_once()


def test_cable_out_also_dismisses_the_legacy_cable_connected_notification():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})
        _connect(c)

        _cable_out(c)

        c.notifier.dismiss_cable_connected_notification.assert_called_once()


# ── wake_action (feature10c, scenario 5b/5c) ─────────────────────────────────────────────────────────────────────

def _connect_async(c):
    """Like _connect(), but inside a running loop so hass.async_create_task actually schedules."""
    async def run():
        _connect(c)
        await asyncio.sleep(0)
    asyncio.run(run())


def test_wake_action_fires_button_press_and_a_bare_service_call_once_each():
    with _patched() as (wires, soc):
        vehicles = [
            {**NIRO, "wake_action": "kia_uvo.force_update"},
            {**ENYAQ, "wake_action": "button.skoda_enyaq_wake_up_car"},
        ]
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"}, vehicles=vehicles)

        _connect_async(c)

        calls = [call.args for call in c.hass.services.async_call.call_args_list]
        assert ("kia_uvo", "force_update", {}) in calls
        assert ("button", "press", {"entity_id": "button.skoda_enyaq_wake_up_car"}) in calls
        assert c.hass.services.async_call.call_count == 2

        # a sensor change mid-wait re-runs the decision table but must not fire the wake actions again
        wires.listeners[0]["action"](MagicMock())
        assert c.hass.services.async_call.call_count == 2


def test_wake_action_is_skipped_when_not_configured():
    with _patched() as (wires, soc):
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"})   # VEHICLES fixture has no wake_action

        _connect_async(c)

        c.hass.services.async_call.assert_not_called()


def test_a_failing_wake_action_does_not_block_the_other_vehicle_or_the_wait():
    with _patched() as (wires, soc):
        vehicles = [
            {**NIRO, "wake_action": "kia_uvo.force_update"},
            {**ENYAQ, "wake_action": "button.skoda_enyaq_wake_up_car"},
        ]
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"}, vehicles=vehicles)
        c.hass.services.async_call.side_effect = Exception("boom")

        _connect_async(c)

        assert c.hass.services.async_call.call_count == 2   # both attempted despite the first raising
        assert len(wires.timers) == 1 and len(wires.listeners) == 1   # the wait itself still started


def test_wake_action_also_fires_when_the_wait_time_is_zero():
    with _patched() as (wires, soc):
        vehicles = [{**NIRO, "wake_action": "kia_uvo.force_update"}, ENYAQ]
        c = _coordinator({ENYAQ_PLUG: "off", NIRO_PLUG: "off"}, vehicles=vehicles, wait=0)

        _connect_async(c)

        c.hass.services.async_call.assert_called_once_with("kia_uvo", "force_update", {}, blocking=False)
        c.notifier.on_vehicle_selection_needed.assert_called_once_with("none_plugged", c._vehicles, "Kia eNiro")


if __name__ == "__main__":
    h.run_tests(globals())
