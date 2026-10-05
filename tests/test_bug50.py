"""Bug 50 – ingen stoppnotis varken vid mål nått eller vid kabelurkoppling.

Två vägar till stoppnotisen var blockerade:
  A. Bug 11-skyddet i _check_notify_events() höll inne notisen när planen hade tid kvar, även när stoppet
     berodde på att målet nåtts (inte ett prishål).
  B. _send_stop_notification()._delayed avbröt alltid vid urkoppling (Bug 12-kabelkontrollen).
Se bug50.md. Riktig OCPPCoordinator (tests/coordinator_harness.py); fejkat: Store, hass-tjänster, async_call_later.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug50.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _module():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    import custom_components.ocpp_charger as mod
    return mod


def _coordinator(store=None):
    c = h.make_coordinator(store)
    c.notifier = MagicMock()
    return c


def _run(fn, *args, **kwargs):
    async def go():
        result = fn(*args, **kwargs)
        await asyncio.sleep(0)
        return result
    return asyncio.run(go())


def _active_plan():
    """Feasible plan som löper en timme till – det som Bug 11-skyddet tolkar som 'prishål'."""
    plan = MagicMock()
    plan.feasible = True
    plan.end = datetime.now(timezone.utc) + timedelta(hours=1)
    return plan


def _mid_stop(plan, *, goal_stop):
    """Laddning har just upphört (was_charging=True, charging=False) – stoppnotisen ska avgöras nu."""
    c = _coordinator()
    c.charge_plan = plan
    c.ocpp.state.connector_status = "SuspendedEVSE"
    c.ocpp.state.charging = False
    c.ocpp.state.session_id = "sess-1"
    c._notified_stop_session = None
    c._was_charging = True
    c._charging_seen_this_session = True
    c._preparing_timestamp = None
    c._goal_reached_stop = goal_stop
    return c


# ── A2: Bug 11-skyddet ──────────────────────────────────────────────────────────────────────────────────

def test_goal_reached_stop_sends_notification_even_though_plan_has_time_left():
    mod = _module()
    c = _mid_stop(_active_plan(), goal_stop=True)
    with patch.object(mod, "async_call_later") as later:
        _run(c._check_notify_events)
    later.assert_called_once()
    assert c._cable_session_stop_notified is True


def test_price_gap_pause_still_holds_the_notification():
    """Bug 11 ska fortfarande gälla för riktiga prishål (flaggan inte satt)."""
    mod = _module()
    c = _mid_stop(_active_plan(), goal_stop=False)
    with patch.object(mod, "async_call_later") as later:
        _run(c._check_notify_events)
    later.assert_not_called()
    assert c._cable_session_stop_notified is False


# ── A1: flaggan ─────────────────────────────────────────────────────────────────────────────────────────

def test_flag_defaults_to_false():
    _module()
    assert _coordinator()._goal_reached_stop is False


def test_goal_reached_branch_of_update_smart_charging_sets_the_flag():
    mod = _module()
    c = _coordinator()
    c.charge_mode = mod.CHARGE_MODE_SMART
    plan = _active_plan()
    plan.active_intervals = [(datetime.now(timezone.utc), plan.end)]
    c.charge_plan = plan
    c.ocpp.state.connected = True
    c.ocpp.state.cable_connected = True
    c.ocpp.state.charging = True
    c.ocpp.state.connector_status = "Charging"
    c.ocpp.state.power_w = 7000
    c._manual_start_requested = False
    c._charging_goal_reached = lambda: (True, "SOC 95% >= mål 95%")
    c._guarded_remote_stop = MagicMock()
    h.silence(c, "_update_charge_plan")

    _run(c._update_smart_charging)

    c._guarded_remote_stop.assert_called_once()
    assert c._goal_reached_stop is True


def test_new_charging_clears_the_flag():
    mod = _module()
    c = _coordinator()
    c._goal_reached_stop = True
    c.ocpp.state.connector_status = "Charging"
    c.ocpp.state.charging = True
    c.ocpp.state.power_w = 7000
    c.ocpp.state.session_id = "sess-2"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert c._goal_reached_stop is False


def test_cable_out_clears_the_flag():
    mod = _module()
    c = _coordinator()
    c._goal_reached_stop = True
    c.ocpp.state.connector_status = "Available"
    c._cable_session_energy_kwh = 0.0   # ingen sammanfattning, bara återställning
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert c._goal_reached_stop is False


def test_genuine_connect_clears_the_flag():
    mod = _module()
    c = _coordinator()
    c._goal_reached_stop = True
    c._cable_was_available = True
    c._last_connector_status_notify = "Available"
    c.ocpp.state.connector_status = "Preparing"
    c.ocpp.state.session_id = "sess-3"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert c._goal_reached_stop is False


# ── B2: urkopplingsvägen ────────────────────────────────────────────────────────────────────────────────

def _delayed_cb(mod, c, **kwargs):
    with patch.object(mod, "async_call_later") as later:
        _run(c._send_stop_notification, **kwargs)
    later.assert_called_once()
    return later.call_args.args[2]


def test_cable_out_summary_is_sent_although_cable_is_no_longer_connected():
    mod = _module()
    c = _coordinator()
    c._cable_session_energy_kwh = 23.1
    c.ocpp.state.cable_connected = False
    c.ocpp.state.charging = False
    cb = _delayed_cb(mod, c, from_cable_out=True)

    _run(lambda: asyncio.ensure_future(cb()))

    c.notifier.on_charging_stopped.assert_called_once()


def test_suspended_ev_path_keeps_the_cable_check():
    """Bug 12 gäller kvar utan from_cable_out."""
    mod = _module()
    c = _coordinator()
    c._cable_session_energy_kwh = 23.1
    c.ocpp.state.cable_connected = False
    c.ocpp.state.charging = False
    cb = _delayed_cb(mod, c)

    _run(lambda: asyncio.ensure_future(cb()))

    c.notifier.on_charging_stopped.assert_not_called()


def test_cable_out_still_respects_the_energy_and_charging_guards():
    mod = _module()
    c = _coordinator()
    c._cable_session_energy_kwh = 0.05
    c.ocpp.state.charging = False
    cb = _delayed_cb(mod, c, from_cable_out=True)

    _run(lambda: asyncio.ensure_future(cb()))

    c.notifier.on_charging_stopped.assert_not_called()


def test_check_notify_events_cable_out_passes_from_cable_out():
    mod = _module()
    c = _coordinator()
    c._cable_session_energy_kwh = 23.1
    c.ocpp.state.connector_status = "Available"
    c.ocpp.state.cable_connected = False
    c.ocpp.state.charging = False
    with patch.object(mod, "async_call_later") as later:
        _run(c._check_notify_events)
    later.assert_called_once()
    _run(lambda: asyncio.ensure_future(later.call_args.args[2]()))
    c.notifier.on_charging_stopped.assert_called_once()


def test_no_duplicate_when_stop_notification_already_sent():
    mod = _module()
    c = _coordinator()
    c._cable_session_energy_kwh = 23.1
    c._cable_session_stop_notified = True
    c.ocpp.state.connector_status = "Available"
    with patch.object(mod, "async_call_later") as later:
        _run(c._check_notify_events)
    later.assert_not_called()


# ── B1: persistens ──────────────────────────────────────────────────────────────────────────────────────

def test_stop_notified_flag_survives_a_restart():
    _module()
    store = h.FakeStore()
    c = _coordinator(store)
    c._cable_session_stop_notified = True
    asyncio.run(c._save_state())
    assert store.data["cable_session_stop_notified"] is True

    c2 = _coordinator(store)
    asyncio.run(c2._load_state())
    assert c2._cable_session_stop_notified is True


def test_restart_with_cable_out_does_not_send_a_false_summary():
    mod = _module()
    store = h.FakeStore()
    c = _coordinator(store)
    c._cable_session_energy_kwh = 23.1
    c._cable_session_stop_notified = True
    c.ocpp.state.cable_connected = False
    asyncio.run(c._save_state())

    c2 = _coordinator(store)
    asyncio.run(c2._load_state())
    c2.ocpp.state.connector_status = "Available"   # trigger-svaret efter omstart
    with patch.object(mod, "async_call_later") as later:
        _run(c2._check_notify_events)
    later.assert_not_called()


def test_old_store_without_key_and_cable_out_is_treated_as_already_sent():
    _module()
    store = h.FakeStore({"cable_connected": False, "cable_session_energy_kwh": 23.1})
    c = _coordinator(store)
    asyncio.run(c._load_state())
    assert c._cable_session_stop_notified is True


def test_old_store_without_key_and_cable_in_is_not_yet_sent():
    _module()
    store = h.FakeStore({"cable_connected": True, "cable_session_energy_kwh": 5.0})
    c = _coordinator(store)
    asyncio.run(c._load_state())
    assert c._cable_session_stop_notified is False


if __name__ == "__main__":
    h.run_tests(globals())
