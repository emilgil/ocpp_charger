"""Bug 55 – bilen stannar själv på målet (SuspendedEV): ingen RemoteStop, ingen stoppnotis.

B1: MeterValues satte charging=True även i SuspendedEV/SuspendedEVSE (ocpp_client).
C1: Bug 5-blocket låg efter Bug 13B (som returnerar på alla SuspendedEV) och nåddes aldrig (__init__).
Se bug55.md (kräver Bug 56). Riktig OCPPCoordinator/OCPPClient; fejkat: Store, hass-tjänster, async_call_later.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug55.py
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


# ── B1: MeterValues ─────────────────────────────────────────────────────────────────────────────────────

def _meter_values(status, *, charging=False, tx=7):
    """Kör ett MeterValues-anrop genom OCPPClient-hanteraren och returnerar state."""
    c = _coordinator()
    client = c.ocpp
    st = client.state
    st.connector_status = status
    st.charging = charging
    st.transaction_id = tx
    st._charging_start = None
    payload = {
        "connectorId": 1, "transactionId": tx,
        "meterValue": [{"timestamp": datetime.now(timezone.utc).isoformat(),
                        "sampledValue": [
                            {"measurand": "Energy.Active.Import.Register", "value": "1000", "unit": "Wh"},
                            {"measurand": "Power.Active.Import", "value": "0", "unit": "W"},
                        ]}],
    }
    asyncio.run(client._parse_meter_values(payload))
    return st


def test_meter_values_in_suspended_ev_keeps_charging_false():
    _module()
    st = _meter_values("SuspendedEV")
    assert st.charging is False
    assert st._charging_start is None


def test_meter_values_in_suspended_evse_keeps_charging_false():
    _module()
    st = _meter_values("SuspendedEVSE")
    assert st.charging is False


def test_meter_values_in_charging_sets_charging_true():
    _module()
    st = _meter_values("Charging")
    assert st.charging is True
    assert st._charging_start is not None


def test_meter_values_after_restart_unknown_status_becomes_charging():
    _module()
    st = _meter_values("Unknown")
    assert st.charging is True
    assert st.connector_status == "Charging"


# ── C1: Bug 5 före Bug 13B ──────────────────────────────────────────────────────────────────────────────

def _suspended(*, since_s, goal, tx=7):
    mod = _module()
    c = _coordinator()
    st = c.ocpp.state
    st.connected = True
    st.cable_connected = True
    st.charging = False
    st.connector_status = "SuspendedEV"
    st.power_w = 0.0
    st.transaction_id = tx
    c._suspended_ev_since = (
        datetime.now(timezone.utc) - timedelta(seconds=since_s) if since_s is not None else None
    )
    c._charging_goal_reached = lambda: (goal, "SOC 100% >= mål 100%" if goal else "")
    c._guarded_remote_stop = MagicMock()
    c._send_stop_notification = MagicMock()
    h.silence(c, "_update_charge_plan")
    return mod, c


def test_suspended_ev_over_60s_and_goal_reached_stops_and_notifies_once():
    _, c = _suspended(since_s=61, goal=True)
    _run(c._update_smart_charging)
    c._guarded_remote_stop.assert_called_once()
    c._send_stop_notification.assert_called_once()
    assert c._goal_reached_stop is True
    assert c._suspended_ev_since is None


def test_suspended_ev_goal_not_reached_does_nothing():
    _, c = _suspended(since_s=300, goal=False)
    _run(c._update_smart_charging)
    c._guarded_remote_stop.assert_not_called()
    c._send_stop_notification.assert_not_called()
    assert c._goal_reached_stop is False


def test_suspended_ev_under_60s_does_nothing_but_starts_timer():
    _, c = _suspended(since_s=None, goal=True)
    _run(c._update_smart_charging)
    c._guarded_remote_stop.assert_not_called()
    assert c._suspended_ev_since is not None
    c2 = _suspended(since_s=30, goal=True)[1]
    _run(c2._update_smart_charging)
    c2._guarded_remote_stop.assert_not_called()


def test_back_to_charging_within_60s_resets_timer():
    _, c = _suspended(since_s=30, goal=True)
    c.ocpp.state.connector_status = "Charging"
    c.ocpp.state.charging = True
    c.ocpp.state.power_w = 7000
    _run(c._update_smart_charging)
    assert c._suspended_ev_since is None
    c._guarded_remote_stop.assert_not_called()


def test_after_stop_transaction_no_second_remote_stop():
    _, c = _suspended(since_s=120, goal=True, tx=None)
    _run(c._update_smart_charging)
    c._guarded_remote_stop.assert_not_called()
    c._send_stop_notification.assert_not_called()


# ── hela nattflödet (med Bug 56) ────────────────────────────────────────────────────────────────────────

def test_full_night_flow_one_notification_with_energy_and_no_duplicate_at_unplug():
    mod = _module()
    _, c = _suspended(since_s=61, goal=True)
    del c._send_stop_notification            # riktig metod
    c._guarded_remote_stop = MagicMock()
    c._wake_active_vehicle = MagicMock()
    c.ocpp.state.energy_kwh = 36.49
    c.ocpp.state.accumulated_cost = 6.63
    with patch.object(mod, "async_call_later") as later:
        _run(c._update_smart_charging)
    later.assert_called_once()
    # StopTransaction
    c.ocpp.state.transaction_id = None
    c._cable_session_energy_kwh += 36.49
    c._cable_session_cost_sek += 6.63
    _run(lambda: asyncio.ensure_future(later.call_args.args[2]()))
    c.notifier.on_charging_stopped.assert_called_once()
    assert abs(c.notifier.on_charging_stopped.call_args.kwargs["energy_kwh"] - 36.49) < 0.01
    # urkoppling 10 h senare
    c.ocpp.state.connector_status = "Available"
    c._last_connector_status_notify = "SuspendedEV"
    with patch.object(mod, "async_call_later") as later2:
        _run(c._check_notify_events)
    later2.assert_not_called()


if __name__ == "__main__":
    h.run_tests(globals())
