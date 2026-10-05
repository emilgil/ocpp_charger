"""Bug 51 – dagladdningsnotis var 15:e minut med urkopplad kabel.

Garo skickar StatusNotification Available var 15:e minut även utan statusändring. Urkopplingsblocket i
_check_notify_events() var nivåstyrt, så varje upprepning nollade bl.a. charge_plan (Bug 49) → prev_plan None →
dag-notisen gick igen. Dessutom saknade dag-grenen i _update_charge_plan() kabelvillkor.
Se bug51.md. Riktig OCPPCoordinator (tests/coordinator_harness.py); fejkat: Store, hass-tjänster, async_call_later.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug51.py
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


def _step(mod, c, status):
    """En StatusNotification-cykel: _check_notify_events() med given status."""
    c.ocpp.state.connector_status = status
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)


def _armed_after_disconnect(mod, c):
    """Äkta urkoppling Charging → Available, sedan sätts sånt som användaren gör med kabeln ur."""
    c._last_connector_status_notify = "Charging"
    _step(mod, c, "Available")
    c.charge_plan = MagicMock()
    c._day_charging_dismissed = True
    c.price_cap_ore_kwh = 80.0
    c._cable_session_energy_kwh = 0.0


# ── A: kantdetektering av Available ─────────────────────────────────────────────────────────────────────

def test_genuine_disconnect_runs_the_disconnect_block():
    mod = _module()
    c = _coordinator()
    c.charge_plan = MagicMock()
    c._cable_was_available = False
    c._last_connector_status_notify = "Charging"
    _step(mod, c, "Available")
    assert c.charge_plan is None
    assert c._cable_was_available is True


def test_repeated_available_does_not_rerun_the_disconnect_block():
    mod = _module()
    c = _coordinator()
    _armed_after_disconnect(mod, c)
    _step(mod, c, "Available")
    assert c.charge_plan is not None
    assert c._day_charging_dismissed is True
    assert c.price_cap_ore_kwh == 80.0


def test_first_available_after_restart_counts_as_a_transition():
    mod = _module()
    c = _coordinator()
    assert c._last_connector_status_notify == ""
    c.charge_plan = MagicMock()
    c._cable_was_available = False
    _step(mod, c, "Available")
    assert c.charge_plan is None
    assert c._cable_was_available is True


def test_repeated_available_does_not_resend_the_stop_notification():
    mod = _module()
    c = _coordinator()
    c._last_connector_status_notify = "Available"
    c._cable_session_energy_kwh = 23.1
    c._cable_session_stop_notified = False
    c._send_stop_notification = MagicMock()
    _step(mod, c, "Available")
    c._send_stop_notification.assert_not_called()


def test_disconnect_transition_still_sends_the_stop_notification():
    mod = _module()
    c = _coordinator()
    c._last_connector_status_notify = "Finishing"
    c._cable_session_energy_kwh = 23.1
    c._send_stop_notification = MagicMock()
    _step(mod, c, "Available")
    c._send_stop_notification.assert_called_once_with(from_cable_out=True)


def test_connect_after_repeated_available_is_still_a_genuine_connect():
    mod = _module()
    c = _coordinator()
    c._last_connector_status_notify = "Charging"
    _step(mod, c, "Available")
    _step(mod, c, "Available")
    _step(mod, c, "Available")
    assert c._cable_was_available is True
    c.ocpp.state.session_id = "sess-new"
    _step(mod, c, "Preparing")
    c.notifier.on_cable_connected.assert_called_once()


# ── B: kabelvillkor på dag-notisen ──────────────────────────────────────────────────────────────────────

def _day_plan_coordinator(mod, *, cable_connected):
    """Dagladdning på, ingen tidigare plan, dagplanen billigare än natten – notisen ska gå om kabeln är i."""
    c = _coordinator()
    now = datetime.now(timezone.utc)
    c.config[mod.CONF_PRICE_FORECAST_ENTITY] = "sensor.price"
    state_obj = MagicMock()
    state_obj.attributes = {"today_interval_prices": [{"time": now, "price": 0.5}],
                            "tomorrow_interval_prices": []}
    c.hass.states.get.return_value = state_obj
    c.allow_day_charging = True
    c.charge_plan = None
    c._day_charging_dismissed = False
    c.ocpp.state.cable_connected = cable_connected
    c.ocpp.state.soc_percent = 20.0
    c.target_soc = 80.0
    c._compute_deadline = lambda *a, **k: now + timedelta(hours=20)
    c.schedule = MagicMock()
    c.schedule.is_day_time.return_value = True
    c.schedule.current_limit.return_value = 16
    h.silence(c, "_rebuild_charge_windows")

    def plan(avg):
        p = MagicMock()
        p.feasible = True
        p.start = now + timedelta(hours=1)
        p.end = now + timedelta(hours=3)
        p.avg_price_ore_kwh = avg
        p.estimated_cost_sek = 10.0
        return p

    calls = []

    def fake_planner(**kwargs):
        calls.append(kwargs)
        return plan(50.0 if len(calls) <= 2 else 90.0)   # new_plan + alt_plan = dag; därefter nattjämförelsen

    return c, patch.object(mod, "plan_cheapest_window", side_effect=fake_planner)


def test_day_notification_suppressed_when_cable_is_out():
    mod = _module()
    c, planner = _day_plan_coordinator(mod, cable_connected=False)
    with planner:
        _run(c._update_charge_plan)
    c.notifier.on_day_charging_chosen.assert_not_called()


def test_day_notification_sent_once_when_cable_is_in():
    mod = _module()
    c, planner = _day_plan_coordinator(mod, cable_connected=True)
    with planner:
        _run(c._update_charge_plan)
    c.notifier.on_day_charging_chosen.assert_called_once()


if __name__ == "__main__":
    h.run_tests(globals())
