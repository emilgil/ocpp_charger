"""Standalone unit tests for estimate_completion_time (Bug 36).

Run without Home Assistant:
    python3 tests/test_smart_charge_bug36.py

smart_charge.py is stdlib-only, so we put the module directory on sys.path and
import it directly (the package __init__.py pulls in HA deps and must NOT be imported).

Bug 36: the SoC branch used the 64 kWh default battery and no efficiency
correction, so the ETA for a 77 kWh Skoda Enyaq at 4.2 kW came out 1h40m early
compared to charge_planner's energy_needed formula.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import smart_charge  # noqa: E402

EFF = 0.92


def _controller():
    return smart_charge.SmartChargeController()


def _hours_from_now(completion):
    return (completion - datetime.now(timezone.utc)) / timedelta(hours=1)


# ── SoC branch: battery_kwh + efficiency (the Bug 36 case) ────────────────
def test_soc_branch_uses_battery_kwh_and_efficiency():
    # Live incident: Enyaq 44%→80%, 77 kWh, 4.2 kW.
    # remaining = (80-44)/100 * 77 / 0.92 = 30.13 kWh → 30.13/4.2 = 7.174 h,
    # matching charge_planner's energy_needed — not the buggy 5.486 h.
    completion = _controller().estimate_completion_time(
        session_kwh=0.0,
        target_kwh=None,
        target_soc=80.0,
        current_soc=44.0,
        power_w=4200.0,
        battery_kwh=77.0,
        efficiency=EFF,
    )
    hours = _hours_from_now(completion)
    expected = (80 - 44) / 100 * 77 / EFF / 4.2
    assert abs(hours - expected) < 0.01, (hours, expected)


def test_soc_branch_default_efficiency_is_neutral():
    # Without the efficiency argument the behaviour is unchanged (eff=1.0):
    # (80-44)/100 * 64 = 23.04 kWh → 5.486 h.
    completion = _controller().estimate_completion_time(
        session_kwh=0.0,
        target_kwh=None,
        target_soc=80.0,
        current_soc=44.0,
        power_w=4200.0,
    )
    hours = _hours_from_now(completion)
    expected = (80 - 44) / 100 * 64 / 4.2
    assert abs(hours - expected) < 0.01, (hours, expected)


# ── target_kwh branch: grid-side measure, no efficiency correction ────────
def test_target_kwh_branch_ignores_efficiency():
    completion = _controller().estimate_completion_time(
        session_kwh=4.0,
        target_kwh=10.0,
        target_soc=80.0,
        current_soc=44.0,
        power_w=4200.0,
        battery_kwh=77.0,
        efficiency=EFF,
    )
    hours = _hours_from_now(completion)
    expected = (10.0 - 4.0) / 4.2
    assert abs(hours - expected) < 0.01, (hours, expected)


# ── unchanged edge cases ──────────────────────────────────────────────────
def test_no_power_returns_none():
    assert _controller().estimate_completion_time(
        session_kwh=0.0,
        target_kwh=None,
        target_soc=80.0,
        current_soc=44.0,
        power_w=0.0,
        battery_kwh=77.0,
        efficiency=EFF,
    ) is None


def test_goal_reached_returns_now():
    completion = _controller().estimate_completion_time(
        session_kwh=0.0,
        target_kwh=None,
        target_soc=80.0,
        current_soc=85.0,
        power_w=4200.0,
        battery_kwh=77.0,
        efficiency=EFF,
    )
    assert abs(_hours_from_now(completion)) < 0.01


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except AssertionError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
        except TypeError as exc:
            failed += 1
            print(f"FAIL  {t.__name__}: {exc}")
    sys.exit(1 if failed else 0)
