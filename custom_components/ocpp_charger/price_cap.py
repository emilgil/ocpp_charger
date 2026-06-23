"""Feature 5: Price-cap charging slot selection.

Pure, stdlib-only helpers (mirrors charge_planner.py / deadline.py) so they can
be unit-tested standalone without importing Home Assistant.  OCPPCoordinator
wraps select_price_cap_slots(), supplying the HA state (prices, schedule, unit,
deadline, allow_day_charging) it needs.

The price-cap rule is deliberately simple: charge every 15-minute slot whose
spot price is at or below the cap, subject to the SAME constraints the ordinary
planner respects — the charging deadline and allow_day_charging.  The SoC target
is still enforced separately by the coordinator's _charging_goal_reached().
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Callable

try:  # in-package import
    from .charge_planner import (
        INTERVAL_HOURS,
        INTERVAL_MINUTES,
        _merge_intervals,
        _to_utc,
    )
except ImportError:  # standalone (tests put the module dir on sys.path)
    from charge_planner import (  # type: ignore[no-redef]
        INTERVAL_HOURS,
        INTERVAL_MINUTES,
        _merge_intervals,
        _to_utc,
    )


@dataclass
class PriceCapPlan:
    """Result of a price-cap slot selection."""

    qualifying_slots: list[dict] = field(default_factory=list)   # [{time, price_kwh(SEK), energy_kwh}]
    active_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    total_kwh: float = 0.0
    total_cost_sek: float = 0.0
    avg_price_ore_kwh: float = 0.0

    @property
    def feasible(self) -> bool:
        return bool(self.qualifying_slots)


def select_price_cap_slots(
    prices: list[dict],
    cap_ore_kwh: float,
    now: datetime,
    deadline: datetime,
    *,
    power_fn: Callable[[datetime], float],
    is_day_fn: Callable[[datetime], bool],
    allow_day_charging: bool,
    local_tz,
) -> PriceCapPlan:
    """Select all future, in-deadline slots priced at or below the cap.

    Args:
        prices:       [{"time": datetime, "ore_kwh": float}, ...] – price already
                      converted to öre/kWh by the caller.
        cap_ore_kwh:  the cap, in öre/kWh; slots with ore_kwh > cap are dropped.
        now:          current time (aware or naive-UTC).
        deadline:     charging must finish by this time; slots ending after it
                      are dropped (same filter as charge_planner.py).
        power_fn:     callable(local_datetime) -> charging power in kW for that
                      slot (schedule-aware, like the ordinary planner).
        is_day_fn:    callable(local_datetime) -> True if the slot is a day slot.
        allow_day_charging: when False, day slots are excluded even when under cap.
        local_tz:     tzinfo used to evaluate power_fn / is_day_fn per slot.

    Returns:
        PriceCapPlan with qualifying slots (chronological), merged active
        intervals, and aggregate energy / cost / average price.
    """
    interval = timedelta(minutes=INTERVAL_MINUTES)
    now_utc = _to_utc(now)
    deadline_utc = _to_utc(deadline)

    qualifying: list[dict] = []
    for iv in prices:
        t_utc = _to_utc(iv["time"])

        # Drop fully-past slots; keep the slot that contains `now` (Bug 22:
        # filter on slot END, not start).
        if t_utc + interval <= now_utc:
            continue
        # Drop slots ending after the deadline (same as charge_planner.py).
        if t_utc + interval > deadline_utc:
            continue

        t_local = t_utc.astimezone(local_tz)
        # Respect allow_day_charging exactly like the ordinary planner's filter.
        if not allow_day_charging and is_day_fn(t_local):
            continue

        if float(iv["ore_kwh"]) > cap_ore_kwh:
            continue

        power_kw = power_fn(t_local)
        qualifying.append({
            "time": t_utc,
            "price_kwh": float(iv["ore_kwh"]) / 100.0,   # öre → SEK/kWh
            "energy_kwh": power_kw * INTERVAL_HOURS,
        })

    qualifying.sort(key=lambda s: s["time"])
    active_intervals = _merge_intervals(
        [{"time": s["time"]} for s in qualifying], interval
    )

    total_kwh = sum(s["energy_kwh"] for s in qualifying)
    total_cost = sum(s["price_kwh"] * s["energy_kwh"] for s in qualifying)
    avg_ore = (total_cost / total_kwh * 100.0) if total_kwh > 0 else 0.0

    return PriceCapPlan(
        qualifying_slots=qualifying,
        active_intervals=active_intervals,
        total_kwh=total_kwh,
        total_cost_sek=total_cost,
        avg_price_ore_kwh=avg_ore,
    )
