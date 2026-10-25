"""Regressionstest Bug 58 – Stopp/Återstår/Klar släpade efter när planen ändrades via en setter (inte pollen).

Start läser charge_plan.start direkt, men Stopp/Återstår/Klar läser estimated_completion/estimated_remaining_minutes,
som bara _update_eta() sätter – och den kördes bara i _async_update_data(). Setter-vägarna räknade om planen och pushade
till entiteterna med gammal ETA. Fix: async_set_updated_data() räknar om ETA före pushen.

Körs med rot-venv (koordinatortester, se tests/coordinator_harness.py):
    /mnt/c/temp/github/claude/venv/bin/python tests/test_bug58.py
Utan Home Assistant hoppas de över (SKIP).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import coordinator_harness as h  # noqa: E402

T0 = datetime(2026, 10, 25, 20, 0, tzinfo=timezone.utc)
PLAN_A = SimpleNamespace(feasible=True, end=T0 + timedelta(hours=3), duration_minutes=210)
PLAN_B = SimpleNamespace(feasible=True, end=T0 + timedelta(hours=10), duration_minutes=210)
INFEASIBLE = SimpleNamespace(feasible=False, end=T0, duration_minutes=0)

BASE_PUSH = "homeassistant.helpers.update_coordinator.DataUpdateCoordinator.async_set_updated_data"


def _coordinator():
    c = h.make_coordinator()
    c.ocpp.state.power_w = 0
    return c


def test_push_after_plan_change_refreshes_eta():
    c = _coordinator()
    c.charge_plan = PLAN_A
    with patch(BASE_PUSH):
        c.async_set_updated_data(c.ocpp.state)
        assert c.estimated_completion == PLAN_A.end
        c.charge_plan = PLAN_B   # planen räknas om i en setter, ingen poll emellan
        c.async_set_updated_data(c.ocpp.state)
    assert c.estimated_completion == PLAN_B.end
    assert c.estimated_remaining_minutes == PLAN_B.duration_minutes


def test_setter_path_set_allow_day_charging_refreshes_eta():
    c = _coordinator()
    c.charge_plan = PLAN_A
    c._update_eta()

    def replan():
        c.charge_plan = PLAN_B
    c._update_charge_plan = replan

    with patch(BASE_PUSH):
        c.set_allow_day_charging(True)

    assert c.estimated_completion == PLAN_B.end


def test_plan_becomes_infeasible_clears_eta():
    c = _coordinator()
    c.charge_plan = PLAN_A
    with patch(BASE_PUSH):
        c.async_set_updated_data(c.ocpp.state)
        c.charge_plan = INFEASIBLE
        c.async_set_updated_data(c.ocpp.state)
    assert c.estimated_completion is None
    assert c.estimated_remaining_minutes is None


def test_eta_failure_does_not_block_the_push():
    c = _coordinator()

    def boom():
        raise RuntimeError("eta")
    c._update_eta = boom

    with patch(BASE_PUSH) as push:
        c.async_set_updated_data(c.ocpp.state)
    assert push.call_count == 1


def test_charging_uses_power_based_eta():
    c = _coordinator()
    c.charge_plan = PLAN_A
    c.ocpp.state.power_w = 7000
    sentinel = T0 + timedelta(hours=2)
    c.smart_controller.estimate_completion_time = lambda **k: sentinel

    with patch(BASE_PUSH):
        c.async_set_updated_data(c.ocpp.state)

    assert c.estimated_completion == sentinel   # inte PLAN_A.end


def test_missing_ocpp_early_in_lifecycle_does_not_crash():
    c = _coordinator()
    del c.ocpp
    with patch(BASE_PUSH) as push:
        c.async_set_updated_data(None)
    assert push.call_count == 1


if __name__ == "__main__":
    h.run_tests(globals())
