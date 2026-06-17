"""Standalone unit tests for deadline.py (Feature 4).

Run without Home Assistant:
    python3 tests/test_deadline.py

deadline.py is stdlib-only, so we put the module directory on sys.path and
import it directly (the package __init__.py pulls in HA deps and must NOT be
imported).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import deadline  # noqa: E402

UTC = timezone.utc
# 2026-06-15 is a Monday (weekday); 2026-06-13 is a Saturday (weekend).
MON = datetime(2026, 6, 15, tzinfo=UTC)
SAT = datetime(2026, 6, 13, tzinfo=UTC)


def at(base: datetime, h: int, m: int = 0) -> datetime:
    return base.replace(hour=h, minute=m)


# ── parse_hhmm ────────────────────────────────────────────────────────────
def test_parse_hhmm_valid():
    assert deadline.parse_hhmm("08:15") == (8, 15)
    assert deadline.parse_hhmm("8:15") == (8, 15)
    assert deadline.parse_hhmm(" 23:59 ") == (23, 59)
    assert deadline.parse_hhmm("00:00") == (0, 0)


def test_parse_hhmm_invalid():
    for bad in ("", "8", "8:5", "abc", "24:00", "12:60", "0815", "08-15"):
        assert deadline.parse_hhmm(bad) is None, bad


# ── compute_deadline: manual ──────────────────────────────────────────────
def test_manual_future_today():
    now = at(MON, 5, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="08:15")
    assert d == at(MON, 8, 15)


def test_manual_past_rolls_to_tomorrow():
    now = at(MON, 9, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="08:15")
    assert d == at(MON, 8, 15) + timedelta(days=1)


def test_manual_equal_now_rolls_to_tomorrow():
    now = at(MON, 8, 15)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="08:15")
    assert d == at(MON, 8, 15) + timedelta(days=1)


def test_invalid_manual_falls_through_to_auto():
    now = at(MON, 4, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="garbage")
    assert d == at(MON, 6, 0)   # weekday 06:00


# ── compute_deadline: automatic weekday ───────────────────────────────────
def test_weekday_before_six_today():
    now = at(MON, 4, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="")
    assert d == at(MON, 6, 0)


def test_weekday_after_six_tomorrow():
    now = at(MON, 7, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="")
    assert d == at(MON, 6, 0) + timedelta(days=1)


def test_weekday_custom_deadline_hour():
    now = at(MON, 3, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="", deadline_hour=5)
    assert d == at(MON, 5, 0)


# ── compute_deadline: automatic weekend ───────────────────────────────────
def test_weekend_uses_last_price_plus_15():
    now = at(SAT, 10, 0)
    last = datetime(2026, 6, 14, 23, 45, tzinfo=UTC)   # Sunday last slot
    prices = [
        {"time": datetime(2026, 6, 14, 23, 30, tzinfo=UTC)},
        {"time": last},
        {"time": datetime(2026, 6, 14, 22, 0, tzinfo=UTC)},
    ]
    d = deadline.compute_deadline(now, UTC, prices, manual_deadline_str="")
    assert d == last + timedelta(minutes=15)


def test_weekend_no_prices_fallback_48h():
    now = at(SAT, 10, 0)
    d = deadline.compute_deadline(now, UTC, [], manual_deadline_str="")
    assert d == now + timedelta(hours=48)


# ── compute_deadline: allow_day_charging (Bug 27) ─────────────────────────
def test_weekday_allow_day_charging_uses_price_horizon():
    # Weekday + allow_day_charging=True → deadline extends to end of price data
    # (same as weekend), so the planner can reach tomorrow's cheap daytime slots.
    now = at(MON, 4, 0)
    last = datetime(2026, 6, 16, 23, 45, tzinfo=UTC)   # Tuesday last slot
    prices = [
        {"time": datetime(2026, 6, 16, 10, 0, tzinfo=UTC)},  # cheap daytime slot
        {"time": last},
        {"time": at(MON, 5, 0)},
    ]
    d = deadline.compute_deadline(
        now, UTC, prices, manual_deadline_str="", allow_day_charging=True,
    )
    assert d == last + timedelta(minutes=15)


def test_weekday_allow_day_charging_no_prices_fallback_48h():
    now = at(MON, 4, 0)
    d = deadline.compute_deadline(
        now, UTC, [], manual_deadline_str="", allow_day_charging=True,
    )
    assert d == now + timedelta(hours=48)


def test_manual_deadline_wins_over_allow_day_charging():
    # Manual HH:MM has highest priority, even with allow_day_charging=True.
    now = at(MON, 5, 0)
    last = datetime(2026, 6, 16, 23, 45, tzinfo=UTC)
    prices = [{"time": last}]
    d = deadline.compute_deadline(
        now, UTC, prices, manual_deadline_str="08:15", allow_day_charging=True,
    )
    assert d == at(MON, 8, 15)


def test_weekday_allow_day_charging_false_still_six():
    # Regression: default (allow_day_charging=False) keeps weekday 06:00 deadline
    # even when later price data exists.
    now = at(MON, 4, 0)
    prices = [{"time": datetime(2026, 6, 16, 23, 45, tzinfo=UTC)}]
    d = deadline.compute_deadline(
        now, UTC, prices, manual_deadline_str="", allow_day_charging=False,
    )
    assert d == at(MON, 6, 0)


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
