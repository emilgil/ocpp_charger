"""Bug 53 – laddplanen ignorerade Target Energy (kWh-mål).

target_kwh > 0 har prioritet över SOC-målet: planering, mål-nått-koll och ETA. Se bug53.md.
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug53.py
"""
import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402


def _coordinator(*, target_kwh, session_total=0.0, tx_energy=0.0, tx_id=1, soc=None, target_soc=80.0):
    c = h.make_coordinator()
    c.target_kwh = target_kwh
    c.target_soc = target_soc
    c._session_total_kwh = session_total
    c.ocpp.state.energy_kwh = tx_energy
    c.ocpp.state.transaction_id = tx_id
    c.ocpp.state.soc_percent = soc
    return c


def test_remaining_none_without_kwh_target():
    assert _coordinator(target_kwh=0.0)._remaining_kwh_goal() is None


def test_remaining_uses_whole_cable_session():
    # Garo-reset: aktiv tx har bara 2 kWh, men kabelsessionen 8 kWh totalt
    c = _coordinator(target_kwh=20.0, session_total=6.0, tx_energy=2.0)
    assert c._remaining_kwh_goal() == 12.0


def test_remaining_ignores_stale_tx_energy_without_transaction():
    c = _coordinator(target_kwh=20.0, session_total=6.0, tx_energy=2.0, tx_id=None)
    assert c._remaining_kwh_goal() == 14.0


def test_remaining_floors_at_zero():
    assert _coordinator(target_kwh=5.0, session_total=9.0)._remaining_kwh_goal() == 0.0


def test_goal_reached_by_kwh_across_garo_reset():
    c = _coordinator(target_kwh=20.0, session_total=19.0, tx_energy=1.5)
    reached, reason = c._charging_goal_reached()
    assert reached and "20.00" in reason


def test_goal_not_reached_by_soc_when_kwh_target_set():
    # SOC 95 % >= mål 80 %, men kWh-målet är inte nått → ska fortsätta
    c = _coordinator(target_kwh=20.0, session_total=3.0, soc=95.0)
    assert c._charging_goal_reached() == (False, "")


def test_soc_goal_unchanged_without_kwh_target():
    c = _coordinator(target_kwh=0.0, soc=95.0)
    reached, reason = c._charging_goal_reached()
    assert reached and reason.startswith("SOC")


# ── target_kwh nollas vid äkta urkoppling (kantdetekterad, Bug 51) ─────────────────────────────────────────

def _disconnect_step(c, status):
    import asyncio
    from unittest.mock import patch
    import custom_components.ocpp_charger as mod

    async def go():
        c.ocpp.state.connector_status = status
        with patch.object(mod, "async_call_later"):
            c._check_notify_events()
            await asyncio.sleep(0)
    asyncio.run(go())


def test_target_kwh_cleared_on_genuine_disconnect():
    c = _coordinator(target_kwh=20.0)
    c.notifier = MagicMock()
    c._last_connector_status_notify = "Charging"
    _disconnect_step(c, "Available")
    assert c.target_kwh == 0.0


def test_target_kwh_kept_on_repeated_available():
    c = _coordinator(target_kwh=20.0)
    c.notifier = MagicMock()
    c._last_connector_status_notify = "Available"
    _disconnect_step(c, "Available")
    assert c.target_kwh == 20.0


if __name__ == "__main__":
    h.run_tests(globals())
