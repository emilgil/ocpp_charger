"""Bug 59: weekend is decided by the weekday of the *next* deadline_hour.

Run without Home Assistant:
    python3 tests/test_bug59.py

deadline.py is stdlib-only (see test_deadline.py).
"""
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import deadline  # noqa: E402

TZ = ZoneInfo("Europe/Stockholm")


def L(y, mo, d, h=0, mi=0):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


def prices_until(last_day_start: datetime):
    """15-min intervals from last_day_start - 1 day through last_day_start 23:45."""
    t = last_day_start - timedelta(days=1)
    end = last_day_start.replace(hour=23, minute=45)
    out = []
    while t <= end:
        out.append({"time": t})
        t += timedelta(minutes=15)
    return out


def dl(now, prices=None, **kw):
    return deadline.compute_deadline(
        now, TZ, prices if prices is not None else [], manual_deadline_str=kw.pop("manual", ""), **kw
    )


def horizon_end(prices):
    return max(p["time"] for p in prices) + timedelta(minutes=15)


def test_sunday_evening_targets_monday_0600():
    now = L(2026, 11, 1, 21, 35)
    assert dl(now, prices_until(L(2026, 11, 2))) == L(2026, 11, 2, 6)


def test_friday_after_six_uses_price_horizon():
    p = prices_until(L(2026, 11, 7))
    assert dl(L(2026, 11, 6, 10), p) == horizon_end(p)


def test_friday_before_six_is_friday_0600():
    assert dl(L(2026, 11, 6, 3), prices_until(L(2026, 11, 7))) == L(2026, 11, 6, 6)


def test_saturday_uses_price_horizon():
    p = prices_until(L(2026, 11, 8))
    assert dl(L(2026, 11, 7, 14), p) == horizon_end(p)


def test_sunday_early_morning_uses_price_horizon():
    p = prices_until(L(2026, 11, 8))
    assert dl(L(2026, 11, 8, 3), p) == horizon_end(p)


def test_monday_early_morning_is_monday_0600():
    assert dl(L(2026, 11, 2, 2), prices_until(L(2026, 11, 3))) == L(2026, 11, 2, 6)


def test_tuesday_evening_is_wednesday_0600():
    assert dl(L(2026, 11, 3, 21), prices_until(L(2026, 11, 4))) == L(2026, 11, 4, 6)


def test_sunday_exactly_0600_rolls_to_monday_0600():
    assert dl(L(2026, 11, 8, 6, 0), prices_until(L(2026, 11, 9))) == L(2026, 11, 9, 6)


def test_sunday_evening_no_prices_still_monday_0600():
    assert dl(L(2026, 11, 1, 21, 35), []) == L(2026, 11, 2, 6)


def test_friday_no_prices_falls_back_to_48h():
    now = L(2026, 11, 6, 10)
    assert dl(now, []) == now + timedelta(hours=48)


def test_sunday_evening_allow_day_charging_uses_price_horizon():
    p = prices_until(L(2026, 11, 2))
    assert dl(L(2026, 11, 1, 21, 35), p, allow_day_charging=True) == horizon_end(p)


def test_manual_deadline_wins_on_sunday_evening():
    now = L(2026, 11, 1, 21, 35)
    assert dl(now, prices_until(L(2026, 11, 2)), manual="07:00") == L(2026, 11, 2, 7)


def test_dst_weekend_boundaries():
    # Spring forward is Sun 2026-03-29 02:00.
    p = prices_until(L(2026, 3, 29))
    assert dl(L(2026, 3, 28, 22), p) == horizon_end(p)   # Sat night -> Sunday = weekend
    p = prices_until(L(2026, 3, 28))
    assert dl(L(2026, 3, 27, 12), p) == horizon_end(p)   # Fri -> Sat = weekend
    got = dl(L(2026, 3, 29, 22), prices_until(L(2026, 3, 30)))
    assert got == L(2026, 3, 30, 6) and got.hour == 6    # Sun night -> Mon 06:00 wall clock


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for fn in fns:
        try:
            fn()
            print(f"ok   {fn.__name__}")
        except AssertionError as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e!r}")
    sys.exit(1 if failed else 0)
