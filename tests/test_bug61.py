"""Bug 61 – STOP-knappen: auto-start startade om laddningen, och laddad energi tappades ur SOC-estimatet.

A: manuellt stopp spärrar auto-start resten av planfönstret. B: Preparing-returen efter manuellt stopp låg före
Bug 13A/33-ackumuleringen. C: notiser vid manuellt stopp och återupptagen laddning. Se bug61.md.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug61.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _module():
    h.coordinator_class()
    import custom_components.ocpp_charger as mod
    return mod


def _plan(*, start_off, end_off, extra=None):
    from custom_components.ocpp_charger.charge_planner import ChargePlan
    now = datetime.now(timezone.utc)
    ivs = [(now + timedelta(minutes=start_off), now + timedelta(minutes=end_off))]
    for s, e in extra or []:
        ivs.append((now + timedelta(minutes=s), now + timedelta(minutes=e)))
    return ChargePlan(
        start=ivs[0][0], end=ivs[-1][1], duration_minutes=60, energy_kwh=10.0, estimated_cost_sek=5.0,
        avg_price_ore_kwh=50.0, active_intervals=ivs, feasible=True,
    )


def _coordinator(store=None, plan=None):
    h.coordinator_class()
    c = h.make_coordinator(store)
    c.notifier = MagicMock()
    c.charge_mode = "Smart (price-optimised)"
    st = c.ocpp.state
    st.connected = True
    st.cable_connected = True
    st.charging = False
    st.connector_status = "Preparing"
    st.transaction_id = None
    st.power_w = 0.0
    c.charge_plan = plan if plan is not None else _plan(start_off=-10, end_off=60)
    c._auto_start_with_limit = AsyncMock()
    c._charging_goal_reached = lambda: (False, "")
    h.silence(c, "_update_charge_plan")
    return c


def _run(fn, *args, **kwargs):
    async def go():
        if getattr(fn, "__name__", "") == "_update_smart_charging":
            fn.__self__._last_smart_charge_run = None   # kringgå 2 s-debouncen
        result = fn(*args, **kwargs)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        return result
    return asyncio.run(go())


def _arun(coro_fn, *args):
    async def go():
        r = await coro_fn(*args)
        await asyncio.sleep(0)
        return r
    return asyncio.run(go())


def _stop(c):
    c.ocpp.remote_stop_transaction = AsyncMock()
    c.async_refresh = AsyncMock()
    _arun(c.async_stop_charging)


# ── Spärren ──────────────────────────────────────────────────────────────────────────────────────────────

def test_1_stop_blocks_autostart_in_open_window():
    c = _coordinator()
    _stop(c)
    assert c._manual_stop_block is True
    _run(c._update_smart_charging)
    c._auto_start_with_limit.assert_not_called()


def test_2_without_stop_autostart_fires():
    c = _coordinator()
    _run(c._update_smart_charging)
    c._auto_start_with_limit.assert_called_once()


def test_3_block_released_when_window_ends():
    c = _coordinator(plan=_plan(start_off=-60, end_off=-5, extra=[(60, 120)]))
    c._manual_stop_block = True
    _run(c._update_smart_charging)
    assert c._manual_stop_block is False
    c._auto_start_with_limit.assert_not_called()


def test_4_next_window_autostarts_after_release():
    c = _coordinator(plan=_plan(start_off=-60, end_off=-5, extra=[(60, 120)]))
    c._manual_stop_block = True
    _run(c._update_smart_charging)          # utanför fönster → hävs
    c.charge_plan = _plan(start_off=-1, end_off=60)   # nästa fönster öppnas
    _run(c._update_smart_charging)
    c._auto_start_with_limit.assert_called_once()


def test_5_no_plan_keeps_block():
    c = _coordinator()
    c.charge_plan = None
    c._manual_stop_block = True
    _run(c._update_smart_charging)
    assert c._manual_stop_block is True


def test_6_extended_window_keeps_block():
    c = _coordinator()
    c._manual_stop_block = True
    c.charge_plan = _plan(start_off=-10, end_off=90)   # förlängt, nuet fortfarande innanför
    _run(c._update_smart_charging)
    assert c._manual_stop_block is True
    c._auto_start_with_limit.assert_not_called()


def test_7_start_button_releases_block_and_starts():
    c = _coordinator()
    c._manual_stop_block = True
    c.ocpp.set_charging_limit = AsyncMock()
    c.ocpp.remote_start_transaction = AsyncMock()
    c.async_refresh = AsyncMock()
    _arun(c.async_start_charging)
    assert c._manual_stop_block is False
    c.ocpp.remote_start_transaction.assert_awaited_once()


def test_8_available_edge_clears_both_flags():
    mod = _module()
    c = _coordinator()
    c._manual_stop_block = True
    c._resume_notify_pending = True
    c._last_connector_status_notify = "Finishing"
    c.ocpp.state.connector_status = "Available"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert c._manual_stop_block is False
    assert c._resume_notify_pending is False


def test_9_persistence_roundtrip_and_missing_keys():
    store = h.FakeStore()
    c = _coordinator(store)
    c._manual_stop_block = True
    c._resume_notify_pending = True
    _arun(c._save_state)
    assert store.data["manual_stop_block"] is True
    assert store.data["resume_notify_pending"] is True
    c2 = _coordinator(store)
    _arun(c2._load_state)
    assert c2._manual_stop_block is True and c2._resume_notify_pending is True
    store.data.pop("manual_stop_block")
    store.data.pop("resume_notify_pending")
    c3 = _coordinator(store)
    _arun(c3._load_state)
    assert c3._manual_stop_block is False and c3._resume_notify_pending is False


# ── SOC-estimatet ────────────────────────────────────────────────────────────────────────────────────────

def _after_stop_preparing(mod, c):
    """STOP → Finishing → StopTransaction → Preparing."""
    c._session_start_soc = 63.0
    c._cable_was_available = False
    c._last_connector_status_notify = "Charging"
    c.ocpp.state.energy_kwh = 8.06
    c.ocpp.state.transaction_id = 7
    c._manual_stop_requested = True
    c.ocpp.state.connector_status = "Finishing"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    c.ocpp.state.transaction_id = None
    c.ocpp.state.connector_status = "Preparing"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)


def test_10_energy_accumulated_after_manual_stop():
    mod = _module()
    c = _coordinator()
    _after_stop_preparing(mod, c)
    assert abs(c._session_total_kwh - 8.06) < 0.001
    c.notifier.on_cable_connected.assert_not_called()   # test 14: ingen Inkopplad-notis


def test_11_no_double_count_on_next_pass():
    mod = _module()
    c = _coordinator()
    _after_stop_preparing(mod, c)
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert abs(c._session_total_kwh - 8.06) < 0.001


def test_12_new_transaction_keeps_energy():
    mod = _module()
    c = _coordinator()
    _after_stop_preparing(mod, c)
    c.ocpp.state.energy_kwh = 0.5       # ny StartTransaction nollade, 0,5 kWh sedan dess
    c.ocpp.state.transaction_id = 8
    assert abs(c._session_kwh_delivered() - 8.56) < 0.001


def test_13_garo_reset_without_manual_stop_still_accumulates():
    mod = _module()
    c = _coordinator()
    c._cable_was_available = False
    c._last_connector_status_notify = "Charging"
    c.ocpp.state.energy_kwh = 3.0
    c.ocpp.state.connector_status = "Preparing"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    assert abs(c._session_total_kwh - 3.0) < 0.001


# ── Notiser ──────────────────────────────────────────────────────────────────────────────────────────────

def test_15_stop_sends_manual_stop_notice_once_without_consuming_summary_flag():
    c = _coordinator()
    c._cable_session_stop_notified = False
    _stop(c)
    c.notifier.on_manual_stop.assert_called_once()
    assert c._cable_session_stop_notified is False


def test_16_notify_on_stop_false_sends_nothing():
    c = _coordinator()
    c._notify_on_stop = False
    _stop(c)
    c.notifier.on_manual_stop.assert_not_called()


def _charging_pass(mod, c, *, manual):
    st = c.ocpp.state
    st.charging = True
    st.power_w = 7000.0
    st.connector_status = "Charging"
    st.transaction_id = 9
    c._manual_start_requested = manual
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)


def test_17_resume_notice_once():
    mod = _module()
    c = _coordinator()
    c._resume_notify_pending = True
    _charging_pass(mod, c, manual=False)
    c.notifier.on_charging_resumed.assert_called_once()
    assert c.notifier.on_charging_resumed.call_args.kwargs["manual"] is False
    assert c._resume_notify_pending is False
    _charging_pass(mod, c, manual=False)
    c.notifier.on_charging_resumed.assert_called_once()


def test_17b_resume_notice_manual_flag():
    mod = _module()
    c = _coordinator()
    c._resume_notify_pending = True
    _charging_pass(mod, c, manual=True)
    assert c.notifier.on_charging_resumed.call_args.kwargs["manual"] is True


def test_18_notify_on_start_false_clears_flag_without_notice():
    mod = _module()
    c = _coordinator()
    c._notify_on_start = False
    c._resume_notify_pending = True
    _charging_pass(mod, c, manual=False)
    c.notifier.on_charging_resumed.assert_not_called()
    assert c._resume_notify_pending is False


def test_19_stop_then_unplug_no_resume_notice():
    mod = _module()
    c = _coordinator()
    c._resume_notify_pending = True
    c._last_connector_status_notify = "Finishing"
    c.ocpp.state.connector_status = "Available"
    with patch.object(mod, "async_call_later"):
        _run(c._check_notify_events)
    c.notifier.on_charging_resumed.assert_not_called()
    assert c._resume_notify_pending is False


def test_notifier_texts():
    h.coordinator_class()
    from custom_components.ocpp_charger.notifier import ChargerNotifier
    n = ChargerNotifier.__new__(ChargerNotifier)
    n._send = MagicMock()
    n.on_manual_stop(8.06, 74.0, None)
    title, text = n._send.call_args.args
    assert title == "Laddning stoppad"
    assert "8.1 kWh" in text and "SOC ca 74 %" in text and "Ingen ny start planerad." in text
    n.on_charging_resumed(74.0, 11.0, None, manual=True)
    title, text = n._send.call_args.args
    assert title == "Laddning återupptagen" and "manuellt" in text and "11.0 kW" in text


if __name__ == "__main__":
    h.run_tests(globals())
