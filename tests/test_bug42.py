"""Regressionstest Bug 42 – Laddfönstret visade Smart-planens billigaste luckor i Immediate-läge.

Körs fristående utan Home Assistant:
    python3 tests/test_bug42.py

charge_planner.py och charge_windows.py importerar bara stdlib, så vi lägger
modulkatalogen på sys.path och importerar modulerna direkt (paketets
__init__.py drar in HA-beroenden och ska INTE importeras).

I Immediate ska fönstret vara ETT block: [sessionens starttid, nu + återstående
energi / effekt]. Testerna körs mot de rena funktionerna ``plan_immediate_window``,
``pick_immediate_power_kw`` och ``immediate_window_wanted``. Alla förväntade
värden är handräknade litteraler, inte beräknade av koden som testas.
"""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(
    0,
    str(Path(__file__).resolve().parents[1] / "custom_components" / "ocpp_charger"),
)
import charge_planner as cp  # noqa: E402
import charge_windows  # noqa: E402

UTC = timezone.utc


def at(hh, mm, ss=0):
    return datetime(2026, 9, 19, hh, mm, ss, tzinfo=UTC)


def slot(hh, mm, sek_per_kwh):
    return {"time": at(hh, mm), "value": sek_per_kwh}


def flat_prices(first, count=40, sek_per_kwh=0.5):
    """`count` kvartsslots i följd från `first`, alla till samma pris."""
    return [
        {"time": first + timedelta(minutes=15 * i), "value": sek_per_kwh}
        for i in range(count)
    ]


def approx(a, b, tol=1e-3):
    return abs(a - b) <= tol


# Fixtur för blandade priser: nu = 10:50, 10 kW, 5 kWh → fönster 10:50–11:20.
#   10:45-sloten  (1,00 SEK): överlapp 10:50–11:00 = 10 min → 10 kW × 1/6 h = 1,6667 kWh
#   11:00-sloten  (2,00 SEK): överlapp 15 min                → 2,5 kWh
#   11:15-sloten  (3,00 SEK): överlapp 11:15–11:20 = 5 min   → 0,8333 kWh
# 10:30 och 11:30 (9,00 SEK) ligger utanför fönstret och får inte komma med.
MIXED_PRICES = [
    slot(10, 30, 9.00),
    slot(10, 45, 1.00),
    slot(11, 0, 2.00),
    slot(11, 15, 3.00),
    slot(11, 30, 9.00),
]


def mixed_plan(**kwargs):
    return cp.plan_immediate_window(MIXED_PRICES, 5.0, 10.0, at(10, 50), **kwargs)


# ── plan_immediate_window ────────────────────────────────────────────────


def test_reported_case_is_one_block_ending_84_min_from_now():
    """Rapporterat fall 2026-09-19 10:58:33: 11,61 kWh kvar, bilen drar 8,3 kW.

    11,61 / 8,3 = 1,398795 h = 5035,66 s → ett enda block som slutar ca 84 min
    fram. Fångar: kvantisering till 15-minutersluckor (Smart-planeraren gav 5
    luckor à 2,75 kWh = 13,8 kWh) och fler än ett intervall.
    """
    now = at(10, 58, 33)

    plan = cp.plan_immediate_window(flat_prices(at(10, 45)), 11.61, 8.3, now)

    assert plan.feasible, plan.message
    assert approx((plan.end - now).total_seconds(), 5035.66, tol=1.0), plan.end
    assert plan.duration_minutes == 84
    assert plan.start == now
    assert plan.active_intervals == [(plan.start, plan.end)]
    assert plan.is_in_window(now)
    assert approx(plan.energy_kwh, 11.61)


def test_cost_and_average_are_weighted_by_overlap():
    """10:50–11:20 över tre olika priser: kostnad 1,6667×1 + 2,5×2 + 0,8333×3 = 9,1667 SEK.

    Snittpriset = 9,1667 / 5 kWh = 1,8333 SEK/kWh = 183,3 öre. Fångar ett
    oviktat snitt ((100+200+300)/3 = 200 öre) och en tappad första slot.
    """
    plan = mixed_plan()

    assert plan.feasible, plan.message
    assert plan.estimated_cost_sek == 9.17
    assert plan.avg_price_ore_kwh == 183.3
    assert len(plan.intervals) == 3


def test_slot_that_started_before_now_is_kept_with_time_clipped_to_now():
    """Bug 22-analogi: sloten 10:45–11:00 innehåller nu (10:50) och ska vara med.

    Postens `time` klipps till nu, annars matchar den inte
    build_charge_windows() (`iv_start <= time < iv_end`, iv_start = nu).
    """
    plan = mixed_plan()

    times = [iv["time"] for iv in plan.intervals]
    assert times == [
        at(10, 50).isoformat(),
        at(11, 0).isoformat(),
        at(11, 15).isoformat(),
    ], times
    assert [iv["price_ore_kwh"] for iv in plan.intervals] == [100.0, 200.0, 300.0]
    energies = [iv["energy_kwh"] for iv in plan.intervals]
    assert approx(energies[0], 1.6667, 1e-4)
    assert approx(energies[1], 2.5, 1e-4)
    assert approx(energies[2], 0.8333, 1e-4)


def test_window_from_now_becomes_one_charge_window_with_full_energy():
    """End-to-end mot Charge Windows-sensorns byggare (Feature 3).

    Utan klippningen av `time` (föregående test) tappas 10:45-sloten och
    planned_energy_kwh blir 3,333 i stället för 5,0.
    """
    plan = mixed_plan()

    windows = charge_windows.build_charge_windows(
        plan.active_intervals, plan.intervals, [], at(10, 50), UTC,
    )

    assert len(windows) == 1
    assert windows[0]["start"] == at(10, 50).isoformat()
    assert windows[0]["end"] == at(11, 20).isoformat()
    assert windows[0]["planned_energy_kwh"] == 5.0
    assert windows[0]["avg_price_ore_kwh"] == 183.3
    assert windows[0]["completed"] is False


def test_window_start_in_the_past_extends_the_block_but_not_the_remaining_plan():
    """Beslut 2026-09-19: vänsterkanten är sessionens starttid (10:26), inte nu.

    Bara `start`/`active_intervals` sträcker sig bakåt. Återstående tid, energi
    och intervallposter gäller från nu, annars räknas redan levererad energi
    med och `_update_eta` (som återanvänder duration_minutes) visar 116 min
    i stället för 84.
    """
    now = at(10, 58, 33)

    plan = cp.plan_immediate_window(
        flat_prices(at(10, 15)), 11.61, 8.3, now, window_start=at(10, 26),
    )

    assert plan.feasible, plan.message
    assert plan.start == at(10, 26)
    assert plan.active_intervals[0][0] == at(10, 26)
    assert approx((plan.active_intervals[0][1] - now).total_seconds(), 5035.66, tol=1.0)
    assert plan.duration_minutes == 84
    assert approx(plan.energy_kwh, 11.61)
    assert all(iv["time"] >= now.isoformat() for iv in plan.intervals), plan.intervals
    assert approx(sum(iv["energy_kwh"] for iv in plan.intervals), 11.61)


def test_window_from_session_start_is_one_charge_window_over_past_and_future():
    """Charge Windows ger ETT block 10:26–11:20 även när starten ligger före nu."""
    plan = mixed_plan(window_start=at(10, 26))

    windows = charge_windows.build_charge_windows(
        plan.active_intervals, plan.intervals, [], at(10, 50), UTC,
    )

    assert len(windows) == 1
    assert windows[0]["start"] == at(10, 26).isoformat()
    assert windows[0]["end"] == at(11, 20).isoformat()
    assert windows[0]["planned_energy_kwh"] == 5.0


def test_window_start_in_the_future_is_clamped_to_now():
    """En starttid efter nu (klockdrift) får inte ge ett block som börjar i framtiden."""
    plan = mixed_plan(window_start=at(11, 30))

    assert plan.start == at(10, 50)
    assert plan.active_intervals[0][0] == at(10, 50)


def test_nothing_to_charge_is_infeasible_and_never_divides_by_zero():
    """energi <= 0 eller effekt <= 0 → feasible=False (anroparen tömmer då fönstret)."""
    now = at(10, 50)
    for energy, power in [(0.0, 8.3), (-1.0, 8.3), (11.61, 0.0), (11.61, -3.0)]:
        plan = cp.plan_immediate_window(MIXED_PRICES, energy, power, now)
        assert plan.feasible is False, (energy, power)
        assert plan.active_intervals == [], (energy, power)


def test_price_data_ending_mid_window_uses_last_known_price_for_the_tail():
    """Prisdata tar slut 11:00; fönstret 10:45–11:45 (10 kWh à 10 kW).

    Fyra kvartsslots à 2,5 kWh. 10:45 kostar 1,00 SEK, resten saknar data och
    prissätts med närmast föregående kända pris (1,00, inte det första 0,50):
    kostnad 10 × 1,00 = 10,00 SEK, snitt 100 öre. Listan ges omvänt sorterad.
    Fångar tappad svans (kostnad 2,5), första-pris (6,25) och pris 0.
    """
    prices = [slot(10, 45, 1.00), slot(10, 30, 0.50)]  # osorterad, inget efter 10:45

    plan = cp.plan_immediate_window(prices, 10.0, 10.0, at(10, 45))

    assert plan.feasible, plan.message
    assert len(plan.intervals) == 4
    assert approx(sum(iv["energy_kwh"] for iv in plan.intervals), 10.0)
    assert plan.estimated_cost_sek == 10.0
    assert plan.avg_price_ore_kwh == 100.0


# ── pick_immediate_power_kw ──────────────────────────────────────────────


def test_pick_power_uses_measured_power_once_charging_ramped_up():
    assert cp.pick_immediate_power_kw(8300.0, True, 11.04) == 8.3
    assert cp.pick_immediate_power_kw(1000.0, True, 11.04) == 1.0   # tröskeln är inklusive


def test_pick_power_falls_back_to_schedule_power_when_measurement_is_unusable():
    # Ramp-up: 999 W skulle annars ge ett 11 kWh-fönster på 11 timmar.
    assert cp.pick_immediate_power_kw(999.0, True, 11.04) == 11.04
    # Laddningen står still men power_w hänger kvar på ett gammalt värde.
    assert cp.pick_immediate_power_kw(8300.0, False, 11.04) == 11.04
    assert cp.pick_immediate_power_kw(None, True, 11.04) == 11.04


# ── immediate_window_wanted ──────────────────────────────────────────────


def test_window_is_wanted_while_charging_and_before_charging_has_started():
    assert cp.immediate_window_wanted(True, "Charging", False, 11.6) is True
    # Beslut 2026-09-19: fönster även innan laddningen startat.
    assert cp.immediate_window_wanted(True, "Preparing", False, 11.6) is True
    assert cp.immediate_window_wanted(True, "SuspendedEVSE", False, 11.6) is True
    assert cp.immediate_window_wanted(True, None, False, 11.6) is True


def test_window_is_not_wanted_when_cable_out_car_satisfied_goal_reached_or_nothing_needed():
    assert cp.immediate_window_wanted(False, "Available", False, 11.6) is False
    assert cp.immediate_window_wanted(True, "SuspendedEV", False, 11.6) is False
    assert cp.immediate_window_wanted(True, "Charging", True, 11.6) is False
    assert cp.immediate_window_wanted(True, "Charging", False, 0.0) is False


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_")]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"PASS  {t.__name__}")
        except Exception as exc:  # AssertionError = fel resultat, annat = kraschade
            failed += 1
            print(f"FAIL  {t.__name__}: {type(exc).__name__}: {exc}")
    sys.exit(1 if failed else 0)
