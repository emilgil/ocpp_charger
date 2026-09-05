"""Regressionstest Bug 40 – tyst dag-till-nästa-dag-hopp i laddplanen.

Körs fristående utan Home Assistant:
    python3 tests/test_bug40.py

charge_planner.py importerar bara stdlib, så vi lägger modulkatalogen på
sys.path och importerar modulen direkt (paketets __init__.py drar in
HA-beroenden och ska INTE importeras).

Testar den rena beslutspredikaten ``is_next_day_shift()``. Nyckelvalet:
jämförelsen görs mot förra planens *slut*-dag, inte dess start-dag, så att en
vanlig vardagsnatt som glider från "22:00 idag" till "02:00 imorgon" före
samma 06:00-deadline INTE räknas som ett hopp – bara ett helt dygns
uppskjutning gör det.
"""
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import charge_planner  # noqa: E402
from charge_planner import ChargePlan, is_next_day_shift  # noqa: E402

try:
    from zoneinfo import ZoneInfo
    STHLM = ZoneInfo("Europe/Stockholm")
except Exception:  # pragma: no cover - zoneinfo/tzdata missing
    STHLM = None

UTC = timezone.utc


def plan(start, end, *, feasible=True):
    """Minimal ChargePlan – bara fälten is_next_day_shift() läser."""
    return ChargePlan(
        start=start,
        end=end,
        duration_minutes=int((end - start).total_seconds() // 60),
        energy_kwh=10.0,
        estimated_cost_sek=5.0,
        avg_price_ore_kwh=50.0,
        feasible=feasible,
    )


def test_the_bug_weekend_whole_day_defer():
    """Rapporterat fall: lör 14:45–16:00 → sön 12:45–14:00 vid nya priser."""
    prev = plan(datetime(2026, 9, 5, 14, 45, tzinfo=UTC), datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 6, 12, 45, tzinfo=UTC), datetime(2026, 9, 6, 14, 0, tzinfo=UTC))
    now = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=True) is True


def test_weekday_night_slide_is_not_a_shift():
    """Vardagsnatt: 22:00 idag → 02:00 imorgon, samma 06:00-deadline. INTE ett hopp."""
    prev = plan(datetime(2026, 9, 9, 22, 0, tzinfo=UTC), datetime(2026, 9, 10, 0, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 10, 2, 0, tzinfo=UTC), datetime(2026, 9, 10, 3, 15, tzinfo=UTC))
    now = datetime(2026, 9, 9, 21, 0, tzinfo=UTC)
    # prev slutar den 10:e, new börjar den 10:e → slut-dag < start-dag är falskt
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=True) is False


def test_same_night_shift_later_is_not_a_shift():
    """23:00–01:00 → 02:00–04:00 samma natt (båda slutar/börjar den 6:e). INTE ett hopp."""
    prev = plan(datetime(2026, 9, 5, 23, 0, tzinfo=UTC), datetime(2026, 9, 6, 1, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 6, 2, 0, tzinfo=UTC), datetime(2026, 9, 6, 4, 0, tzinfo=UTC))
    now = datetime(2026, 9, 5, 22, 0, tzinfo=UTC)
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=True) is False


def test_same_day_replan_is_not_a_shift():
    """Normal omplanering inom idag: 10:00–12:00 → 14:00–16:00. INTE ett hopp."""
    prev = plan(datetime(2026, 9, 5, 10, 0, tzinfo=UTC), datetime(2026, 9, 5, 12, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 5, 14, 0, tzinfo=UTC), datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    now = datetime(2026, 9, 5, 9, 0, tzinfo=UTC)
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=True) is False


def test_day_rolled_over_prev_no_longer_today():
    """Efter midnatt: förra planens start är "igår" → hållet släpps (predikat False)."""
    prev = plan(datetime(2026, 9, 5, 14, 45, tzinfo=UTC), datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 6, 12, 45, tzinfo=UTC), datetime(2026, 9, 6, 14, 0, tzinfo=UTC))
    now = datetime(2026, 9, 6, 0, 30, tzinfo=UTC)   # nu är det den 6:e
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=True) is False


def test_cable_disconnected_is_never_a_shift():
    prev = plan(datetime(2026, 9, 5, 14, 45, tzinfo=UTC), datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    new = plan(datetime(2026, 9, 6, 12, 45, tzinfo=UTC), datetime(2026, 9, 6, 14, 0, tzinfo=UTC))
    now = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    assert is_next_day_shift(prev, new, now, UTC, cable_connected=False) is False


def test_none_or_infeasible_plans_are_not_a_shift():
    p = plan(datetime(2026, 9, 5, 14, 45, tzinfo=UTC), datetime(2026, 9, 5, 16, 0, tzinfo=UTC))
    n = plan(datetime(2026, 9, 6, 12, 45, tzinfo=UTC), datetime(2026, 9, 6, 14, 0, tzinfo=UTC))
    now = datetime(2026, 9, 5, 13, 0, tzinfo=UTC)
    assert is_next_day_shift(None, n, now, UTC, cable_connected=True) is False
    assert is_next_day_shift(p, None, now, UTC, cable_connected=True) is False
    assert is_next_day_shift(
        plan(p.start, p.end, feasible=False), n, now, UTC, cable_connected=True
    ) is False
    assert is_next_day_shift(
        p, plan(n.start, n.end, feasible=False), now, UTC, cable_connected=True
    ) is False


def test_local_tz_is_applied_to_day_boundaries():
    """Dagsgränsen ska räknas i lokal tid, inte UTC.

    prev-fönstret slutar 21:30 UTC lördag = 23:30 lokal (Europe/Stockholm, sommar).
    Lokalt är start- och slut-dag alltså fortfarande lördag den 5:e; new börjar
    söndag den 6:e lokalt → hopp. I ren UTC hade slutet varit 21:30 den 5:e –
    här sammanfaller resultaten, men testet befäster att .astimezone() körs.
    """
    if STHLM is None:
        print("SKIP  test_local_tz_is_applied_to_day_boundaries (ingen zoneinfo)")
        return
    prev = plan(
        datetime(2026, 9, 5, 16, 0, tzinfo=UTC),   # 18:00 lokal
        datetime(2026, 9, 5, 21, 30, tzinfo=UTC),  # 23:30 lokal, fortf. lördag lokalt
    )
    new = plan(
        datetime(2026, 9, 6, 10, 45, tzinfo=UTC),  # 12:45 lokal söndag
        datetime(2026, 9, 6, 12, 0, tzinfo=UTC),
    )
    now = datetime(2026, 9, 5, 11, 0, tzinfo=UTC)   # 13:00 lokal lördag
    assert is_next_day_shift(prev, new, now, STHLM, cable_connected=True) is True

    # Kontroll: skjut prev-slutet till 22:30 UTC = 00:30 lokal söndag. Nu är
    # prev:s lokala slut-dag söndag = new:s start-dag → inte längre ett hopp.
    prev_crosses = plan(
        datetime(2026, 9, 5, 16, 0, tzinfo=UTC),
        datetime(2026, 9, 5, 22, 30, tzinfo=UTC),   # 00:30 lokal söndag
    )
    assert is_next_day_shift(prev_crosses, new, now, STHLM, cable_connected=True) is False


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
