"""Bug 62 – Override Current-slidern nådde inte laddaren. Se bug62.md.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug62.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import logging
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402
from test_bug61 import _plan, _run, _arun  # noqa: E402

PKG = Path(__file__).resolve().parent.parent / "custom_components" / "ocpp_charger"


def _coordinator(store=None, plan=None, charging=False):
    h.coordinator_class()
    c = h.make_coordinator(store)
    c.notifier = MagicMock()
    c.charge_mode = "Smart (price-optimised)"
    st = c.ocpp.state
    st.connected = True
    st.cable_connected = True
    st.charging = charging
    st.connector_status = "Charging" if charging else "Preparing"
    st.transaction_id = 5 if charging else None
    st.power_w = 11000.0 if charging else 0.0
    c.charge_plan = plan if plan is not None else _plan(start_off=-10, end_off=60)
    c._auto_start_with_limit = AsyncMock()
    c._charging_goal_reached = lambda: (False, "")
    c.ocpp.set_charging_limit = AsyncMock()
    c.async_refresh = AsyncMock()
    c.ocpp.remote_stop_transaction = AsyncMock()
    c._guarded_remote_stop = MagicMock()
    c.schedule.set_override(False)
    c.schedule.day_current_a = c.schedule.night_current_a = 16.0   # tidsoberoende schema
    c.max_current = 16.0
    c.ocpp._default_limit_a = 16.0
    h.silence(c, "_update_charge_plan")
    return c


def _set(c, amps):
    return _arun(c.async_set_current_override, amps)


def _clear(c, reason="test"):
    return _arun(c.async_clear_current_override, reason)


def test_1_slider_under_charging_sends_limit_and_activates_switch():
    c = _coordinator(charging=True)
    _set(c, 8)
    c.ocpp.set_charging_limit.assert_called_once_with(8.0)
    assert c.max_current == 8.0 and c.ocpp._default_limit_a == 8.0
    assert c.schedule.override_active is True and c._override_used is True


def test_2_restart_transaction_reapplies_override_not_schedule():
    c = _coordinator(charging=True)
    _set(c, 8)
    assert c.ocpp._pending_limit_a is None and c.ocpp._default_limit_a == 8.0


def test_3_autostart_uses_override_current():
    c = _coordinator()
    _set(c, 8)
    _run(c._update_smart_charging)
    c._auto_start_with_limit.assert_called_once_with(8.0)


def test_4_replan_forced_despite_recent_remote_start():
    c = _coordinator()
    real = type(c)._update_charge_plan
    c._update_charge_plan = lambda *a, **k: real(c, *a, **k)
    c._last_remote_start = datetime.now(timezone.utc) - timedelta(seconds=30)
    logs = []

    class H(logging.Handler):
        def emit(self, r):
            logs.append(r.getMessage())

    lg = logging.getLogger("custom_components.ocpp_charger")
    hd = H(); lg.addHandler(hd); old = lg.level; lg.setLevel(logging.DEBUG)
    try:
        try:
            real(c)
        except Exception:
            pass
        assert any("Frozen after RemoteStart" in m for m in logs)
        logs.clear()
        try:
            real(c, force=True)
        except Exception:
            pass
        assert not any("Frozen after RemoteStart" in m for m in logs)
    finally:
        lg.removeHandler(hd); lg.setLevel(old)


def test_5_frozen_session_plan_replaced_only_when_now_inside_new_plan():
    c = _coordinator(charging=True)
    old = [(datetime.now(timezone.utc) - timedelta(minutes=5), datetime.now(timezone.utc) + timedelta(minutes=5))]
    c._session_plan_intervals = list(old)
    c._update_charge_plan = lambda *a, **k: setattr(c, "charge_plan", _plan(start_off=-10, end_off=90))
    c._replan_after_override_change()
    assert c._session_plan_intervals != old
    c._session_plan_intervals = list(old)
    c._update_charge_plan = lambda *a, **k: setattr(c, "charge_plan", _plan(start_off=30, end_off=90))
    c._replan_after_override_change()
    assert c._session_plan_intervals == old


def test_6_window_end_after_limited_charge_clears_override():
    c = _coordinator(charging=True)
    _set(c, 8)
    c.ocpp.state.charging = False
    c.charge_plan = _plan(start_off=-60, end_off=-5, extra=[(60, 120)])
    c.ocpp.set_charging_limit.reset_mock()
    _run(c._update_smart_charging)
    assert c.schedule.override_active is False and c._override_used is False
    assert c.max_current == 16.0
    c.ocpp.set_charging_limit.assert_called_with(16.0)


def test_7_goal_reached_clears_override():
    c = _coordinator(charging=True)
    _set(c, 8)
    c._charging_goal_reached = lambda: (True, "mål")
    c._manual_start_requested = False
    _run(c._update_smart_charging)
    assert c.schedule.override_active is False


def test_8_available_edge_clears_override():
    mod = __import__("custom_components.ocpp_charger", fromlist=["x"])
    c = _coordinator()
    _set(c, 8)
    c._last_connector_status_notify = "Finishing"
    c.ocpp.state.connector_status = "Available"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert c.schedule.override_active is False


def test_9_stop_clears_override_and_next_start_is_on_schedule():
    c = _coordinator(charging=True)
    _set(c, 8)
    _arun(c.async_stop_charging)
    assert c.schedule.override_active is False and c.max_current == 16.0
    c.ocpp.set_charging_limit.assert_called_with(16.0)


def test_10_slider_without_charging_stays_until_window_used_and_ended():
    c = _coordinator()
    _set(c, 8)
    c.ocpp.set_charging_limit.assert_called_once_with(8.0)
    assert c._override_used is False
    c.charge_plan = _plan(start_off=-60, end_off=-5, extra=[(60, 120)])
    _run(c._update_smart_charging)
    assert c.schedule.override_active is True   # aldrig använd → ligger kvar
    c.ocpp.state.charging = True
    _run(c._update_smart_charging)
    assert c._override_used is True
    c.ocpp.state.charging = False
    _run(c._update_smart_charging)
    assert c.schedule.override_active is False


def test_11_slider_without_charging_then_stop_clears():
    c = _coordinator()
    _set(c, 8)
    _arun(c.async_stop_charging)
    assert c.schedule.override_active is False


def test_12_switch_off_and_on():
    c = _coordinator(charging=True)
    _set(c, 8)
    _clear(c, "switch av")
    c.ocpp.set_charging_limit.assert_called_with(16.0)
    c.schedule.override_current_a = 10.0
    _set(c, c.schedule.override_current_a)
    c.ocpp.set_charging_limit.assert_called_with(10.0)


def test_13_value_is_clamped():
    c = _coordinator()
    _set(c, 2)
    assert c.schedule.override_current_a == 6.0
    _set(c, 99)
    assert c.schedule.override_current_a == 16.0   # CONF_MAX_CURRENT i harnessen


def test_14_persistence_roundtrip_and_missing_keys():
    store = h.FakeStore()
    c = _coordinator(store)
    _set(c, 8)
    c._override_used = True
    _arun(c._save_state)
    assert store.data["override_active"] is True and store.data["override_current_a"] == 8.0
    assert store.data["override_used"] is True
    c2 = _coordinator(store)
    _arun(c2._load_state)
    assert c2.schedule.override_active and c2.schedule.override_current_a == 8.0 and c2._override_used
    for k in ("override_active", "override_current_a", "override_used"):
        store.data.pop(k)
    c3 = _coordinator(store)
    _arun(c3._load_state)
    assert c3.schedule.override_active is False and c3._override_used is False


def test_15_current_limit_at_follows_override():
    c = _coordinator()
    local = datetime(2026, 11, 16, 13, 0)
    assert c.schedule.current_limit_at(local) == c.schedule.day_current_a
    c.schedule.set_override(True, current_a=8.0)
    assert c.schedule.current_limit_at(local) == 8.0
    assert c.schedule.current_limit_at(local.replace(hour=23)) == 8.0


def test_16_number_value_shows_schedule_when_inactive():
    h.coordinator_class()
    from custom_components.ocpp_charger.number import OverrideCurrentNumber
    c = _coordinator()
    n = OverrideCurrentNumber.__new__(OverrideCurrentNumber)
    n._coordinator = c
    assert n.native_value == c.schedule.current_limit()
    c.schedule.set_override(True, current_a=8.0)
    assert n.native_value == 8.0


def test_17_max_charging_current_removed_and_registry_cleaned():
    for f in PKG.glob("*.py"):
        t = f.read_text(encoding="utf-8")
        for bad in ("MaxCurrentNumber", "async_set_max_current", "NUMBER_MAX_CURRENT"):
            assert bad not in t, (f.name, bad)
    h.coordinator_class()
    from custom_components.ocpp_charger import number as num
    from custom_components.ocpp_charger.const import DOMAIN
    c = _coordinator()
    hass = MagicMock()
    hass.data = {DOMAIN: {"e1": c}}
    entry = MagicMock(); entry.entry_id = "e1"
    entry.data = {"charger_id": "X", "max_current": 16}
    reg = MagicMock()
    reg.async_get_entity_id.return_value = "number.old"
    with patch.object(num.er, "async_get", return_value=reg):
        asyncio.run(num.async_setup_entry(hass, entry, MagicMock()))
    reg.async_get_entity_id.assert_called_once_with("number", DOMAIN, "e1_max_current_limit")
    reg.async_remove.assert_called_once_with("number.old")
    reg.reset_mock(); reg.async_get_entity_id.return_value = None
    with patch.object(num.er, "async_get", return_value=reg):
        asyncio.run(num.async_setup_entry(hass, entry, MagicMock()))
    reg.async_remove.assert_not_called()


if __name__ == "__main__":
    h.run_tests(globals())
