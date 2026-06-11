"""Regressionstest Bug 22 – sloten som innehåller `now` ska behållas i planen.

Körs fristående utan Home Assistant:
    python3 tests/test_charge_planner_bug22.py

charge_planner.py importerar bara stdlib, så vi lägger modulkatalogen
på sys.path och importerar modulen direkt (paketets __init__.py drar in
HA-beroenden och ska INTE importeras).
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import charge_planner  # noqa: E402

TZ = timezone.utc
POWER_KW = 11.04          # 16 A × 230 V × 3 faser
ENERGY_NEEDED = 22.0      # kWh → 8 slottar à 2.76 kWh (22.08 kWh, OBS: bara 0.08 marginal)
DEADLINE = datetime(2026, 6, 11, 6, 0, tzinfo=TZ)

CHEAP_START = datetime(2026, 6, 11, 2, 30, tzinfo=TZ)
CHEAP_END   = datetime(2026, 6, 11, 4, 45, tzinfo=TZ)   # 9 billiga slottar 02:30–04:45


def make_prices():
    """Kvartspriser 2026-06-10 22:00 → 2026-06-11 06:00. Billigt 02:30–04:45."""
    start = datetime(2026, 6, 10, 22, 0, tzinfo=TZ)
    prices = []
    for i in range(32):
        t = start + timedelta(minutes=15 * i)
        prices.append({"time": t, "value": 0.10 if CHEAP_START <= t < CHEAP_END else 1.00})
    return prices


def plan_at(now, *, contiguous):
    return charge_planner.plan_cheapest_window(
        make_prices(), ENERGY_NEEDED, POWER_KW, DEADLINE,
        contiguous=contiguous, now=now,
    )


def test_initial_plan_starts_0230():
    """Plan beräknad vid auto-start 02:30:01 börjar 02:30.

    Fallerar också före fixen: redan 1 s efter slot-start droppar det gamla
    filtret 02:30-sloten. I drift maskerades det av 5-min-freezen efter
    RemoteStart – samma rotorsak som mid-slot-fallet.
    """
    plan = plan_at(datetime(2026, 6, 11, 2, 30, 1, tzinfo=TZ), contiguous=True)
    assert plan.feasible, plan.message
    assert plan.start == CHEAP_START, f"start={plan.start}"


def test_recalc_mid_slot_keeps_active_slot_contiguous():
    """Bug 22 (contiguous): omräkning 02:35:11 får INTE tappa sloten 02:30–02:45."""
    now = datetime(2026, 6, 11, 2, 35, 11, tzinfo=TZ)
    plan = plan_at(now, contiguous=True)
    assert plan.feasible, plan.message
    assert plan.start == CHEAP_START, (
        f"plan.start hoppade fram till {plan.start} – aktiv slot tappad (Bug 22)"
    )
    assert plan.is_in_window(now), "02:35 ska ligga i planfönstret"


def test_recalc_mid_slot_keeps_active_slot_greedy():
    """Bug 22 (greedy): samma sak med greedy-algoritmen."""
    now = datetime(2026, 6, 11, 2, 35, 11, tzinfo=TZ)
    plan = plan_at(now, contiguous=False)
    assert plan.feasible, plan.message
    assert plan.start == CHEAP_START, (
        f"plan.start hoppade fram till {plan.start} – aktiv slot tappad (Bug 22)"
    )
    assert plan.is_in_window(now), "02:35 ska ligga i planfönstret"


def test_slot_dropped_exactly_at_its_end():
    """Gränsfall: vid now == slotens slut (02:45:00) ska 02:30-sloten droppas."""
    now = datetime(2026, 6, 11, 2, 45, 0, tzinfo=TZ)
    plan = plan_at(now, contiguous=True)
    assert plan.feasible, plan.message
    assert plan.start == datetime(2026, 6, 11, 2, 45, tzinfo=TZ), f"start={plan.start}"
    assert plan.is_in_window(now), (
        "laddningen ska fortsätta sömlöst – täcks av nästa slot (02:45–03:00), "
        "inte av 02:30-sloten som droppats"
    )


def test_future_plan_unchanged():
    """Auto-start-fallet: kl 22:00 ska planen fortfarande börja 02:30, in_window False."""
    now = datetime(2026, 6, 10, 22, 0, 30, tzinfo=TZ)
    plan = plan_at(now, contiguous=True)
    assert plan.feasible, plan.message
    assert plan.start == CHEAP_START, f"start={plan.start}"
    assert not plan.is_in_window(now), "22:00 ligger före planfönstret"


def test_price_gap_stops_at_slot_end():
    """Bug 11-interaktion: vid prishål ska stoppet fortfarande ske vid slot-slut.

    Greedy-plan med hål: billig slot 22:45–23:00, dyrt 23:00–23:15, billigt
    från 23:15. Vid now == 23:00:00 droppas 22:45-sloten (slut <= now) och
    is_in_window(23:00) ska vara False så att stop-logiken triggar i hålet.
    """
    gap_cheap_1 = datetime(2026, 6, 10, 22, 45, tzinfo=TZ)
    gap_expensive = datetime(2026, 6, 10, 23, 0, tzinfo=TZ)
    start = datetime(2026, 6, 10, 22, 0, tzinfo=TZ)
    prices = []
    for i in range(32):
        t = start + timedelta(minutes=15 * i)
        cheap = (t == gap_cheap_1) or (t >= gap_expensive + timedelta(minutes=15))
        prices.append({"time": t, "value": 0.10 if cheap else 1.00})

    now = datetime(2026, 6, 10, 23, 0, 0, tzinfo=TZ)
    plan = charge_planner.plan_cheapest_window(
        prices, ENERGY_NEEDED, POWER_KW, DEADLINE,
        contiguous=False, now=now,
    )
    assert plan.feasible, plan.message
    assert not plan.is_in_window(now), (
        "23:00 ligger i prishålet – 22:45-sloten ska vara droppad och "
        "stop-logiken ska trigga"
    )


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
