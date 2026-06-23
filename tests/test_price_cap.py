"""Standalone unit tests for price_cap.py (Feature 5).

Run without Home Assistant:
    python3 tests/test_price_cap.py

price_cap.py is stdlib-only (it only imports the stdlib-only charge_planner
helpers), so we put the module directory on sys.path and import it directly –
the package __init__.py pulls in HA deps and must NOT be imported.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import price_cap  # noqa: E402

UTC = timezone.utc
DAY = datetime(2026, 6, 15, tzinfo=UTC)  # Monday


def at(h: int, m: int = 0) -> datetime:
    return DAY.replace(hour=h, minute=m)


def slot(h: int, m: int, ore: float) -> dict:
    return {"time": at(h, m), "ore_kwh": ore}


# Constant 4 kW → 1.0 kWh per 15-min slot (clean numbers).
POWER_4KW = lambda local_dt: 4.0  # noqa: E731
NEVER_DAY = lambda local_dt: False  # noqa: E731


def _select(prices, cap, *, now=None, deadline=None, allow_day=True,
            is_day_fn=NEVER_DAY, power_fn=POWER_4KW):
    return price_cap.select_price_cap_slots(
        prices,
        cap,
        now if now is not None else at(0, 0),
        deadline if deadline is not None else at(23, 59),
        power_fn=power_fn,
        is_day_fn=is_day_fn,
        allow_day_charging=allow_day,
        local_tz=UTC,
    )


# ── Price filter ───────────────────────────────────────────────────────────
def test_excludes_slots_above_cap():
    res = _select([slot(20, 0, 50), slot(20, 15, 150), slot(20, 30, 100)], cap=100)
    times = [s["time"] for s in res.qualifying_slots]
    assert times == [at(20, 0), at(20, 30)], times


def test_price_equal_to_cap_is_kept():
    res = _select([slot(20, 0, 100)], cap=100)
    assert len(res.qualifying_slots) == 1


# ── Past-slot filter (Bug 22 semantics: filter on slot END) ──────────────────
def test_drops_fully_past_slots_keeps_slot_containing_now():
    res = _select(
        [slot(19, 45, 10), slot(20, 0, 10), slot(20, 15, 10)],
        cap=100, now=at(20, 5),
    )
    times = [s["time"] for s in res.qualifying_slots]
    # 19:45 ends 20:00 <= now → dropped; 20:00 contains now → kept; 20:15 future.
    assert times == [at(20, 0), at(20, 15)], times


# ── Deadline filter ──────────────────────────────────────────────────────────
def test_drops_slots_ending_after_deadline():
    res = _select(
        [slot(20, 0, 10), slot(20, 15, 10), slot(20, 30, 10)],
        cap=100, deadline=at(20, 30),
    )
    times = [s["time"] for s in res.qualifying_slots]
    # 20:00 ends 20:15<=20:30 kept; 20:15 ends 20:30<=20:30 kept; 20:30 ends 20:45>20:30 dropped.
    assert times == [at(20, 0), at(20, 15)], times


# ── allow_day_charging filter ────────────────────────────────────────────────
def _is_day_6_to_22(local_dt):
    return 6 <= local_dt.hour < 22


def test_allow_day_false_excludes_day_slots():
    res = _select(
        [slot(10, 0, 10), slot(23, 0, 10)],
        cap=100, allow_day=False, is_day_fn=_is_day_6_to_22,
    )
    times = [s["time"] for s in res.qualifying_slots]
    assert times == [at(23, 0)], times


def test_allow_day_true_includes_day_slots():
    res = _select(
        [slot(10, 0, 10), slot(23, 0, 10)],
        cap=100, allow_day=True, is_day_fn=_is_day_6_to_22,
    )
    assert len(res.qualifying_slots) == 2


# ── Energy + aggregates ──────────────────────────────────────────────────────
def test_energy_and_aggregates():
    # power 4 kW → 1.0 kWh/slot. ore 50 → 0.5 SEK/kWh, ore 100 → 1.0 SEK/kWh.
    res = _select([slot(20, 0, 50), slot(20, 15, 100)], cap=100)
    assert all(abs(s["energy_kwh"] - 1.0) < 1e-9 for s in res.qualifying_slots)
    assert abs(res.total_kwh - 2.0) < 1e-9
    assert abs(res.total_cost_sek - 1.5) < 1e-9   # 0.5*1 + 1.0*1
    assert abs(res.avg_price_ore_kwh - 75.0) < 1e-6


def test_slot_price_kwh_is_in_sek():
    res = _select([slot(20, 0, 80)], cap=100)
    assert abs(res.qualifying_slots[0]["price_kwh"] - 0.80) < 1e-9


# ── Interval merge ───────────────────────────────────────────────────────────
def test_merges_adjacent_slots_and_separates_gaps():
    res = _select(
        [slot(20, 0, 10), slot(20, 15, 10), slot(21, 0, 10)],
        cap=100,
    )
    assert res.active_intervals == [
        (at(20, 0), at(20, 30)),
        (at(21, 0), at(21, 15)),
    ], res.active_intervals


# ── Empty result ─────────────────────────────────────────────────────────────
def test_no_qualifying_slots():
    res = _select([slot(20, 0, 200), slot(20, 15, 300)], cap=100)
    assert res.qualifying_slots == []
    assert res.active_intervals == []
    assert res.feasible is False
    assert res.total_kwh == 0.0
    assert res.avg_price_ore_kwh == 0.0


def test_feasible_true_when_slots_exist():
    res = _select([slot(20, 0, 50)], cap=100)
    assert res.feasible is True


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
        except Exception as exc:  # noqa: BLE001
            failed += 1
            print(f"ERROR {t.__name__}: {type(exc).__name__}: {exc}")
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    sys.exit(1 if failed else 0)
