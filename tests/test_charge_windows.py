"""Standalone unit tests for charge_windows.py (Feature 3).

Run without Home Assistant:
    python3 tests/test_charge_windows.py

charge_windows.py is stdlib-only, so we put the module directory on sys.path
and import it directly (the package __init__.py pulls in HA deps and must NOT
be imported).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import charge_windows  # noqa: E402

UTC = timezone.utc


def _interval(t: datetime, price_ore: float, energy: float) -> dict:
    """Build one plan.intervals entry the way charge_planner emits it."""
    return {
        "time": t.isoformat(),
        "price_ore_kwh": price_ore,
        "power_kw": energy * 4,        # 15-min slot → kWh*4 = kW (informational)
        "energy_kwh": energy,
    }


def test_build_two_slots_planned_energy_and_avg_price():
    """Two active windows, each covering one 15-min interval."""
    a_start = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    a_end   = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    b_start = datetime(2026, 6, 13, 1, 0, tzinfo=UTC)
    b_end   = datetime(2026, 6, 13, 1, 15, tzinfo=UTC)

    intervals = [
        _interval(a_start, 50.0, 2.0),
        _interval(b_start, 70.0, 3.0),
    ]
    active = [(a_start, a_end), (b_start, b_end)]

    # now is before both windows → neither completed
    now = datetime(2026, 6, 12, 22, 0, tzinfo=UTC)
    slots = charge_windows.build_charge_windows(active, intervals, [], now, UTC)

    assert len(slots) == 2
    assert slots[0]["planned_energy_kwh"] == 2.0
    assert slots[0]["avg_price_ore_kwh"] == 50.0
    assert slots[0]["actual_energy_kwh"] is None
    assert slots[0]["completed"] is False
    assert slots[1]["planned_energy_kwh"] == 3.0
    assert slots[1]["avg_price_ore_kwh"] == 70.0


def test_build_weighted_avg_price_across_multiple_intervals():
    """A 30-min window covers two intervals with different prices/energy."""
    w_start = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    mid     = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    w_end   = datetime(2026, 6, 12, 23, 30, tzinfo=UTC)

    intervals = [
        _interval(w_start, 40.0, 1.0),   # cost 40
        _interval(mid,     80.0, 3.0),   # cost 240
    ]
    active = [(w_start, w_end)]
    now = datetime(2026, 6, 12, 22, 0, tzinfo=UTC)

    slots = charge_windows.build_charge_windows(active, intervals, [], now, UTC)

    assert len(slots) == 1
    assert slots[0]["planned_energy_kwh"] == 4.0
    # weighted avg = (40 + 240) / 4.0 = 70.0
    assert slots[0]["avg_price_ore_kwh"] == 70.0


def test_build_completed_flag_for_past_slot():
    """A slot whose end is before `now` is marked completed."""
    s = datetime(2026, 6, 12, 1, 0, tzinfo=UTC)
    e = datetime(2026, 6, 12, 1, 15, tzinfo=UTC)
    intervals = [_interval(s, 50.0, 2.0)]
    now = datetime(2026, 6, 12, 3, 0, tzinfo=UTC)   # after the slot

    slots = charge_windows.build_charge_windows([(s, e)], intervals, [], now, UTC)
    assert slots[0]["completed"] is True


def test_build_preserves_actual_energy_from_existing_slot():
    """Rebuild must not wipe an actual_energy_kwh already recorded for a slot
    with the same start (so replans mid-charge keep post-hoc data)."""
    s = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    e = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    intervals = [_interval(s, 50.0, 2.0)]
    now = datetime(2026, 6, 12, 22, 0, tzinfo=UTC)

    start_iso = s.astimezone(UTC).isoformat()
    existing = [{
        "start": start_iso,
        "end": e.astimezone(UTC).isoformat(),
        "planned_energy_kwh": 2.0,
        "avg_price_ore_kwh": 50.0,
        "actual_energy_kwh": 1.85,
        "completed": True,
    }]

    slots = charge_windows.build_charge_windows([(s, e)], intervals, existing, now, UTC)
    assert slots[0]["actual_energy_kwh"] == 1.85


def test_actual_filled_when_slot_completes():
    """Snapshot captured at slot start; delta written when slot ends."""
    s = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    e = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    windows = [{
        "start": s.isoformat(), "end": e.isoformat(),
        "planned_energy_kwh": 2.0, "avg_price_ore_kwh": 50.0,
        "actual_energy_kwh": None, "completed": False,
    }]
    snapshots: dict = {}

    # During the slot at 23:05, cumulative = 5.0 kWh → snapshot baseline
    charge_windows.update_windows_actual(
        windows, snapshots, current_cumulative_kwh=5.0,
        now=datetime(2026, 6, 12, 23, 5, tzinfo=UTC),
    )
    assert windows[0]["actual_energy_kwh"] is None  # not done yet
    assert snapshots[s.isoformat()] == 5.0

    # After the slot at 23:20, cumulative = 6.8 kWh → actual = 1.8
    charge_windows.update_windows_actual(
        windows, snapshots, current_cumulative_kwh=6.8,
        now=datetime(2026, 6, 12, 23, 20, tzinfo=UTC),
    )
    assert windows[0]["completed"] is True
    assert windows[0]["actual_energy_kwh"] == 1.8


def test_actual_not_set_for_future_slot():
    """A slot that has not ended keeps actual_energy_kwh = None."""
    s = datetime(2026, 6, 13, 2, 0, tzinfo=UTC)
    e = datetime(2026, 6, 13, 2, 15, tzinfo=UTC)
    windows = [{
        "start": s.isoformat(), "end": e.isoformat(),
        "planned_energy_kwh": 2.0, "avg_price_ore_kwh": 50.0,
        "actual_energy_kwh": None, "completed": False,
    }]
    charge_windows.update_windows_actual(
        windows, {}, current_cumulative_kwh=1.0,
        now=datetime(2026, 6, 12, 22, 0, tzinfo=UTC),   # long before the slot
    )
    assert windows[0]["actual_energy_kwh"] is None
    assert windows[0]["completed"] is False


def test_actual_snapshot_keyed_by_start_iso_survives_reorder():
    """Snapshots keyed by start-ISO stay correct even if slot order changes."""
    s1 = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    e1 = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    s2 = datetime(2026, 6, 13, 1, 0, tzinfo=UTC)
    e2 = datetime(2026, 6, 13, 1, 15, tzinfo=UTC)

    def mk(s, e):
        return {"start": s.isoformat(), "end": e.isoformat(),
                "planned_energy_kwh": 2.0, "avg_price_ore_kwh": 50.0,
                "actual_energy_kwh": None, "completed": False}

    snapshots: dict = {}
    windows = [mk(s1, e1), mk(s2, e2)]
    # Snapshot slot1 baseline at 23:05 (cumulative 5.0)
    charge_windows.update_windows_actual(
        windows, snapshots, 5.0, datetime(2026, 6, 12, 23, 5, tzinfo=UTC))

    # Slot list rebuilt in reversed order; slot1 finishes at 23:20, cumulative 7.0
    windows = [mk(s2, e2), mk(s1, e1)]
    charge_windows.update_windows_actual(
        windows, snapshots, 7.0, datetime(2026, 6, 12, 23, 20, tzinfo=UTC))

    done = next(w for w in windows if w["start"] == s1.isoformat())
    assert done["actual_energy_kwh"] == 2.0   # 7.0 - 5.0, snapshot found by ISO


def test_actual_never_negative():
    """A cumulative drop (e.g. counter reset) clamps actual to 0."""
    s = datetime(2026, 6, 12, 23, 0, tzinfo=UTC)
    e = datetime(2026, 6, 12, 23, 15, tzinfo=UTC)
    windows = [{
        "start": s.isoformat(), "end": e.isoformat(),
        "planned_energy_kwh": 2.0, "avg_price_ore_kwh": 50.0,
        "actual_energy_kwh": None, "completed": False,
    }]
    snapshots = {s.isoformat(): 9.0}
    charge_windows.update_windows_actual(
        windows, snapshots, current_cumulative_kwh=4.0,   # dropped below baseline
        now=datetime(2026, 6, 12, 23, 20, tzinfo=UTC),
    )
    assert windows[0]["actual_energy_kwh"] == 0.0


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
