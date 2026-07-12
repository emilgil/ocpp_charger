"""Standalone unit tests for soc_estimate.py (Bug 29).

Run without Home Assistant:
    python3 tests/test_soc_estimate.py

soc_estimate.py is stdlib-only, so we put the module directory on sys.path and
import it directly (the package __init__.py pulls in HA deps and must NOT be imported).
"""
import sys
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import soc_estimate  # noqa: E402

EFF = 0.92


# ── estimation applies ────────────────────────────────────────────────────
def test_full_energy_reaches_target():
    # Kia eNiro from the live incident: start 66%, 64 kWh, target 100%.
    # Full needed energy = (100-66)/100 * 64 / 0.92 ≈ 23.65 kWh → ~100%.
    full = (100 - 66) / 100 * 64 / EFF
    est = soc_estimate.estimate_soc(66.0, full, 64.0, EFF, 66.0)
    assert 99.5 <= est <= 100.5, est


def test_half_energy_is_midpoint_not_target():
    # Regression guard for Bug 29: at half the needed energy the estimated SOC is
    # the midpoint (~83%), NOT the 100% target — so the goal must NOT count as reached.
    full = (100 - 66) / 100 * 64 / EFF
    est = soc_estimate.estimate_soc(66.0, full / 2, 64.0, EFF, 66.0)
    assert 82.0 <= est <= 85.0, est
    assert est < 100.0


def test_matches_planner_formula():
    # Exact value the planner logs: start=66.0% +12.27 kWh → 83.6%.
    est = soc_estimate.estimate_soc(66.0, 12.27, 64.0, EFF, 66.0)
    assert round(est, 1) == 83.6, est


# ── Bug 38: floor at fresh reported SOC ────────────────────────────────────
def test_floor_at_reported_soc_when_session_energy_lost():
    # Live incident 2026-07-12: HA restart mid-session erased _session_total_kwh,
    # so the estimate collapsed to start 52% + 2.64 kWh → 55.2% while the sensor
    # (fresh at transaction pause) reported 84%. SOC never drops during a cable
    # session, so the reported value is a safe floor.
    est = soc_estimate.estimate_soc(52.0, 2.64, 77.0, EFF, 84.0)
    assert est == 84.0, est


def test_estimate_above_reported_wins():
    # Stale sensor the other way (too LOW report, e.g. SOC entity frozen during
    # charging) is unaffected: max() picks the estimate as before.
    est = soc_estimate.estimate_soc(66.0, 12.27, 64.0, EFF, 66.0)
    assert round(est, 1) == 83.6, est


def test_floor_skipped_when_reported_none():
    # No reported SOC → plain estimate, unchanged behaviour.
    est = soc_estimate.estimate_soc(52.0, 2.64, 77.0, EFF, None)
    assert round(est, 1) == 55.2, est


# ── fallback to reported SOC (no estimation basis) ─────────────────────────
def test_no_start_soc_returns_reported():
    assert soc_estimate.estimate_soc(None, 10.0, 64.0, EFF, 55.0) == 55.0


def test_no_energy_returns_reported():
    assert soc_estimate.estimate_soc(66.0, 0.0, 64.0, EFF, 66.0) == 66.0


def test_zero_capacity_returns_reported_no_divzero():
    assert soc_estimate.estimate_soc(66.0, 10.0, 0.0, EFF, 66.0) == 66.0


def test_reported_none_ok():
    assert soc_estimate.estimate_soc(None, 0.0, 64.0, EFF, None) is None


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
    sys.exit(1 if failed else 0)
