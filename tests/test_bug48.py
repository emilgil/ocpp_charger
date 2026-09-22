"""Bug 48 – SoC-väckningen vid laddstopp riktar sig mot den aktiva bilen, inte alltid Kia.

Riktig OCPPCoordinator (tests/coordinator_harness.py). Fejkat: hass-tjänsterna och tid (async_call_later).
Bygger på feature10c:s _wake_vehicles() (redan testad i test_feature10_coordinator.py) – de här testerna
bevisar bara att den nya _wake_active_vehicle() och de två kia_uvo-anropsställena riktar väckningen mot
self.active_vehicle i stället för att hårdkoda Kia. Se claude_bug48.md.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug48.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

NIRO = {"name": "Kia eNiro", "capacity_kwh": 64.0, "soc_entity": "sensor.e_niro_ev_battery_level",
        "soc_unit": "percent", "max_current_a": 0, "wake_action": "kia_uvo.force_update"}
ENYAQ = {"name": "Skoda Enyaq", "capacity_kwh": 77.0, "soc_entity": "sensor.skoda_enyaq_battery_percentage",
         "soc_unit": "percent", "wake_action": "button.skoda_enyaq_wake_up_car"}


def _module():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    import custom_components.ocpp_charger as mod
    return mod


def _coordinator(vehicles=(NIRO, ENYAQ)):
    c = h.make_coordinator(vehicles=list(vehicles))
    c.notifier = MagicMock()
    return c


def _run(fn):
    """Kör fn() inuti en löpande loop och låt schemalagda tasks (hass.async_create_task) hinna köras en gång."""
    async def go():
        fn()
        await asyncio.sleep(0)
    asyncio.run(go())


# ── _wake_active_vehicle ────────────────────────────────────────────────────────────────────────────────

def test_wake_active_vehicle_fires_the_active_vehicles_own_action():
    c = _coordinator()
    c.active_vehicle = ENYAQ

    _run(c._wake_active_vehicle)

    c.hass.services.async_call.assert_called_once_with(
        "button", "press", {"entity_id": "button.skoda_enyaq_wake_up_car"}, blocking=False,
    )


def test_wake_active_vehicle_fires_kias_service_when_kia_is_active():
    c = _coordinator()
    c.active_vehicle = NIRO

    _run(c._wake_active_vehicle)

    c.hass.services.async_call.assert_called_once_with("kia_uvo", "force_update", {}, blocking=False)


def test_wake_active_vehicle_is_a_noop_without_an_active_vehicle():
    c = _coordinator()
    c.active_vehicle = None

    _run(c._wake_active_vehicle)   # ska inte krascha

    c.hass.services.async_call.assert_not_called()


def test_wake_active_vehicle_is_a_noop_when_the_vehicle_dict_has_no_wake_action_key():
    """Befintliga bilkonfigurationer saknar nyckeln helt (ingen migrering) – se claude_feature10.md."""
    c = _coordinator(vehicles=[{k: v for k, v in NIRO.items() if k != "wake_action"}, ENYAQ])
    c.active_vehicle = c._vehicles[0]

    _run(c._wake_active_vehicle)

    c.hass.services.async_call.assert_not_called()


# ── _send_stop_notification(): kabel-ur / SuspendedEV-flödet ────────────────────────────────────────────

def test_send_stop_notification_wakes_the_enyaq_when_the_enyaq_charged():
    """Regression: innan fixen anropades alltid kia_uvo.force_update här, oavsett aktiv bil."""
    mod = _module()
    c = _coordinator()
    c.active_vehicle = ENYAQ
    with patch.object(mod, "async_call_later"):
        _run(c._send_stop_notification)

    c.hass.services.async_call.assert_called_once_with(
        "button", "press", {"entity_id": "button.skoda_enyaq_wake_up_car"}, blocking=False,
    )


def test_send_stop_notification_still_wakes_kia_when_kia_charged():
    mod = _module()
    c = _coordinator()
    c.active_vehicle = NIRO
    with patch.object(mod, "async_call_later"):
        _run(c._send_stop_notification)

    c.hass.services.async_call.assert_called_once_with("kia_uvo", "force_update", {}, blocking=False)


# ── _check_notify_events(): "Charging stopped"-grenen (Bug 10) ──────────────────────────────────────────

def _coordinator_mid_stop(active_vehicle):
    """En koordinator precis vid gränsen där laddning just upphört och stopp-notisen ska schemaläggas."""
    c = _coordinator()
    c.active_vehicle = active_vehicle
    c.charge_plan = None  # ingen plan att hålla inne notisen för
    c.ocpp.state.connector_status = "Charging"  # varken Available eller Preparing
    c.ocpp.state.charging = False
    c.ocpp.state.session_id = "sess-1"
    c._notified_stop_session = None
    c._was_charging = True
    c._charging_seen_this_session = True
    c._preparing_timestamp = None
    return c


def test_check_notify_events_stop_branch_wakes_the_enyaq_when_the_enyaq_charged():
    mod = _module()
    c = _coordinator_mid_stop(ENYAQ)
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)

    c.hass.services.async_call.assert_called_once_with(
        "button", "press", {"entity_id": "button.skoda_enyaq_wake_up_car"}, blocking=False,
    )


def test_check_notify_events_stop_branch_still_wakes_kia_when_kia_charged():
    mod = _module()
    c = _coordinator_mid_stop(NIRO)
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)

    c.hass.services.async_call.assert_called_once_with("kia_uvo", "force_update", {}, blocking=False)


if __name__ == "__main__":
    h.run_tests(globals())
