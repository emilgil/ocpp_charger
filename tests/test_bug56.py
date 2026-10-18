"""Bug 56 – ingen sammanfattning vid urkoppling när transaktionen fortfarande är öppen.

Garo skickar Available före StopTransaction när kabeln dras under öppen tx, så kabelsessionens ackumulator är 0 när
urkopplingsvillkoret utvärderas, och _send_stop_notification() fångade energin för tidigt. Se bug56.md.
Riktig OCPPCoordinator (tests/coordinator_harness.py); fejkat: Store, hass-tjänster, async_call_later.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug56.py
Utan Home Assistant hoppas de över (SKIP).
"""
import asyncio
import logging
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _module():
    h.coordinator_class()   # SKIP utan HA; lägger repo-roten på sys.path
    import custom_components.ocpp_charger as mod
    return mod


def _coordinator():
    c = h.make_coordinator()
    c.notifier = MagicMock()
    return c


def _run(fn, *args, **kwargs):
    async def go():
        result = fn(*args, **kwargs)
        await asyncio.sleep(0)
        return result
    return asyncio.run(go())


def _unplug(c, *, accumulator, tx_id, tx_kwh, tx_cost=0.0):
    """Available-kant (kabeln dras ur) med given ackumulator och eventuell öppen transaktion."""
    st = c.ocpp.state
    st.connector_status = "Available"
    st.charging = False
    st.transaction_id = tx_id
    st.energy_kwh = tx_kwh
    st.accumulated_cost = tx_cost
    c._last_connector_status_notify = "SuspendedEV"
    c._cable_session_energy_kwh = accumulator
    c._cable_session_cost_sek = 0.0
    c._cable_session_stop_notified = False


def _check(mod, c):
    with patch.object(mod, "async_call_later") as later:
        _run(c._check_notify_events)
    return later


def _fire(c, later):
    cb = later.call_args.args[2]
    _run(lambda: asyncio.ensure_future(cb()))


def _stop_transaction(c, kwh, cost=0.0):
    """Vad StopTransaction-hanteraren i ocpp_client gör mot koordinatorn."""
    c.ocpp.state.transaction_id = None
    c._cable_session_energy_kwh += kwh
    c._cable_session_cost_sek += cost


# ── A1 + B1 ─────────────────────────────────────────────────────────────────────────────────────────────

def test_open_tx_at_unplug_sends_summary_with_energy_from_stop_transaction():
    mod = _module()
    c = _coordinator()
    _unplug(c, accumulator=0.0, tx_id=7, tx_kwh=36.5, tx_cost=6.6)
    later = _check(mod, c)
    later.assert_called_once()
    _stop_transaction(c, 36.49, 6.63)   # kommer ~80 ms efter Available
    _fire(c, later)
    c.notifier.on_charging_stopped.assert_called_once()
    kw = c.notifier.on_charging_stopped.call_args.kwargs
    assert abs(kw["energy_kwh"] - 36.49) < 0.01 and abs(kw["actual_cost_sek"] - 6.63) < 0.01


def test_stop_transaction_never_arrives_falls_back_to_value_at_call():
    mod = _module()
    c = _coordinator()
    _unplug(c, accumulator=0.0, tx_id=7, tx_kwh=36.5, tx_cost=6.6)
    later = _check(mod, c)
    _fire(c, later)
    kw = c.notifier.on_charging_stopped.call_args.kwargs
    assert abs(kw["energy_kwh"] - 36.5) < 0.01 and abs(kw["actual_cost_sek"] - 6.6) < 0.01


def test_closed_tx_existing_case_unchanged():
    mod = _module()
    c = _coordinator()
    _unplug(c, accumulator=23.1, tx_id=None, tx_kwh=0.0)
    later = _check(mod, c)
    later.assert_called_once()
    _fire(c, later)
    c.notifier.on_charging_stopped.assert_called_once()
    assert abs(c.notifier.on_charging_stopped.call_args.kwargs["energy_kwh"] - 23.1) < 0.01


def test_no_energy_no_call_and_logs_debug_line():
    mod = _module()
    c = _coordinator()
    _unplug(c, accumulator=0.0, tx_id=None, tx_kwh=0.0)
    with patch.object(mod._LOGGER, "debug") as dbg:
        later = _check(mod, c)
    later.assert_not_called()
    assert any("[Bug56]" in str(call.args[0]) for call in dbg.call_args_list)


def test_already_consumed_logs_and_sends_nothing():
    mod = _module()
    c = _coordinator()
    c._cable_session_stop_notified = True
    with patch.object(mod, "async_call_later") as later, patch.object(mod._LOGGER, "debug") as dbg:
        _run(c._send_stop_notification, from_cable_out=True)
    later.assert_not_called()
    assert any("[Bug56]" in str(call.args[0]) and "förbrukad" in str(call.args[0]) for call in dbg.call_args_list)


def test_repeated_available_does_not_run_unplug_block():
    """Bug 51 oförändrad: upprepad Available (ingen kant) ger ingen sammanfattning."""
    mod = _module()
    c = _coordinator()
    _unplug(c, accumulator=0.0, tx_id=7, tx_kwh=36.5)
    c._last_connector_status_notify = "Available"
    later = _check(mod, c)
    later.assert_not_called()


if __name__ == "__main__":
    h.run_tests(globals())
