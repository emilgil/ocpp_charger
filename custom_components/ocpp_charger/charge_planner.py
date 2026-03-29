"""Optimal charge planner using interval prices from gespot/similar sensors.

The planner is schedule-aware: each 15-minute interval is assigned the charging
power that the day/night schedule would produce at that time.  This means a
cheap noon slot with a 6 A day-limit is correctly compared against a more
expensive midnight slot with a 12 A night-limit.

Algorithm: greedy selection of cheapest slots by cost-per-kWh.
Slots are sorted by effective price (cost / energy) and picked until the
energy target is met.  This allows non-contiguous charging windows – e.g.
charge 22:00-23:00, pause, then resume 02:00-04:00 if those are cheapest.

The result contains ``active_intervals`` – a list of (start, end) tuples
that the coordinator uses for auto-start/stop decisions.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

_LOGGER = logging.getLogger(__name__)

INTERVAL_MINUTES = 15
INTERVAL_HOURS   = INTERVAL_MINUTES / 60   # 0.25


@dataclass
class ChargePlan:
    """Result of an optimal charge window calculation."""
    start: datetime
    end: datetime
    duration_minutes: int
    energy_kwh: float
    estimated_cost_sek: float
    avg_price_ore_kwh: float
    intervals: list[dict] = field(default_factory=list)
    active_intervals: list[tuple[datetime, datetime]] = field(default_factory=list)
    feasible: bool = True
    partial: bool = False   # True if insufficient data – plan covers only what is available
    message: str = ""

    def is_in_window(self, t: datetime) -> bool:
        """Return True if *t* falls within any active charging interval."""
        for iv_start, iv_end in self.active_intervals:
            if iv_start <= t <= iv_end:
                return True
        return False

    def next_start_after(self, t: datetime) -> datetime | None:
        """Return the start of the next active interval that begins at or after *t*."""
        for iv_start, _iv_end in self.active_intervals:
            if iv_start >= t:
                return iv_start
        return None


def _to_utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _merge_intervals(
    slots: list[dict], interval_duration: timedelta
) -> list[tuple[datetime, datetime]]:
    """Merge adjacent/overlapping 15-min slots into contiguous (start, end) tuples."""
    if not slots:
        return []
    sorted_slots = sorted(slots, key=lambda s: s["time"])
    merged: list[tuple[datetime, datetime]] = []
    cur_start = sorted_slots[0]["time"]
    cur_end = sorted_slots[0]["time"] + interval_duration

    for s in sorted_slots[1:]:
        s_start = s["time"]
        s_end = s_start + interval_duration
        # Allow 60 s tolerance for adjacency (handles imprecise interval boundaries)
        if (s_start - cur_end).total_seconds() <= 60:
            cur_end = s_end
        else:
            merged.append((cur_start, cur_end))
            cur_start = s_start
            cur_end = s_end
    merged.append((cur_start, cur_end))
    return merged


# ── Slot selection strategies ─────────────────────────────────────────────

def _select_greedy(
    slots: list[dict], energy_needed_kwh: float
) -> tuple[list[dict], bool, bool]:
    """Pick the globally cheapest slots until energy target is met.

    Returns (chosen_slots, feasible, partial).
    """
    slots_by_price = sorted(slots, key=lambda s: (s["price"], s["time"]))

    chosen: list[dict] = []
    collected = 0.0

    for s in slots_by_price:
        if collected >= energy_needed_kwh:
            break
        chosen.append(s)
        collected += s["energy_kwh"]

    if collected >= energy_needed_kwh:
        return chosen, True, False

    total_available = sum(s["energy_kwh"] for s in slots)
    _LOGGER.warning(
        "[ChargePlanner] Insufficient data: need %.2f kWh, only %.2f kWh available",
        energy_needed_kwh, total_available,
    )
    if collected > 0:
        return chosen, True, True
    return chosen, False, False


def _select_contiguous(
    slots: list[dict], energy_needed_kwh: float
) -> tuple[list[dict], bool, bool]:
    """Sliding-window: find the cheapest contiguous block of slots.

    Returns (chosen_slots, feasible, partial).
    """
    slots.sort(key=lambda s: s["time"])
    n = len(slots)

    best_cost = float("inf")
    best_l    = 0
    best_r    = n - 1
    feasible  = False

    left     = 0
    acc_e    = 0.0
    acc_cost = 0.0

    for right in range(n):
        # Check for time gap – reset left to current right
        if right > 0:
            gap = (slots[right]["time"] - slots[right - 1]["time"]).total_seconds()
            if abs(gap - 900) > 60:
                left     = right
                acc_e    = 0.0
                acc_cost = 0.0

        acc_e    += slots[right]["energy_kwh"]
        acc_cost += slots[right]["cost"]

        # Shrink window from left while we still have enough energy
        while acc_e - slots[left]["energy_kwh"] >= energy_needed_kwh:
            acc_e    -= slots[left]["energy_kwh"]
            acc_cost -= slots[left]["cost"]
            left     += 1

        if acc_e >= energy_needed_kwh and acc_cost < best_cost:
            feasible  = True
            best_cost = acc_cost
            best_l    = left
            best_r    = right

    if not feasible:
        total_available = sum(s["energy_kwh"] for s in slots)
        _LOGGER.warning(
            "[ChargePlanner] Insufficient data: need %.2f kWh, only %.2f kWh available",
            energy_needed_kwh, total_available,
        )
        if total_available > 0:
            return list(slots), True, True
        return list(slots), False, False

    chosen = slots[best_l: best_r + 1]

    # Trim trailing slots once energy target is met
    trimmed: list[dict] = []
    collected = 0.0
    for s in chosen:
        if collected >= energy_needed_kwh:
            break
        trimmed.append(s)
        collected += s["energy_kwh"]

    return (trimmed if trimmed else chosen), True, False


def plan_cheapest_window(
    interval_prices: list[dict[str, Any]],
    energy_needed_kwh: float,
    power_kw: float,                          # fallback if schedule_fn not given
    deadline: datetime,
    *,
    contiguous: bool = False,
    now: datetime | None = None,
    schedule_fn: Callable[[datetime], float] | None = None,
    voltage: float = 230.0,
    num_phases: int = 3,
    local_tz=None,
) -> ChargePlan:
    """
    Find the cheapest set of 15-min intervals that delivers energy_needed_kwh,
    respecting per-interval power from the schedule.

    Two algorithms:
      - **Greedy** (default, contiguous=False): picks the globally cheapest
        slots regardless of position, enabling multi-window charging.
      - **Contiguous** (contiguous=True): sliding-window that finds the
        cheapest single block of consecutive slots.

    Args:
        interval_prices:  [{time: datetime, value: float (SEK/kWh)}, ...]
        energy_needed_kwh: total energy to deliver
        power_kw:         fallback power when schedule_fn is None
        deadline:         charging must finish by this time
        contiguous:       if True, use sliding-window for a single block
        now:              current time (default: utcnow)
        schedule_fn:      callable(local_datetime) -> limit_ampere
        voltage:          grid voltage per phase (V)
        num_phases:       number of phases
        local_tz:         local timezone for schedule evaluation
    """
    now = now or datetime.now(timezone.utc)
    now_utc      = _to_utc(now)
    deadline_utc = _to_utc(deadline)
    interval_duration = timedelta(minutes=INTERVAL_MINUTES)

    if energy_needed_kwh <= 0:
        return ChargePlan(
            start=now_utc, end=now_utc, duration_minutes=0,
            energy_kwh=0, estimated_cost_sek=0, avg_price_ore_kwh=0,
            feasible=True, message="No charging needed – target already reached.",
        )

    # ── Build enriched slot list ──────────────────────────────────────────
    slots: list[dict] = []
    for iv in interval_prices:
        t_utc = _to_utc(iv["time"])
        if t_utc < now_utc:
            continue
        if t_utc + interval_duration > deadline_utc:
            continue

        if schedule_fn is not None and local_tz is not None:
            t_local    = t_utc.astimezone(local_tz)
            limit_a    = schedule_fn(t_local)
            slot_pw    = (limit_a * voltage * num_phases) / 1000.0
        else:
            slot_pw = power_kw

        slot_e = slot_pw * INTERVAL_HOURS   # kWh this slot can deliver

        slots.append({
            "time":       t_utc,
            "price":      float(iv["value"]),
            "power_kw":   slot_pw,
            "energy_kwh": slot_e,
            "cost":       float(iv["value"]) * slot_e,
        })

    if not slots:
        return ChargePlan(
            start=now_utc, end=deadline_utc, duration_minutes=0,
            energy_kwh=0, estimated_cost_sek=0, avg_price_ore_kwh=0,
            feasible=False, message="No price data available in the requested window.",
        )

    if contiguous:
        chosen, feasible, _partial = _select_contiguous(slots, energy_needed_kwh)
    else:
        chosen, feasible, _partial = _select_greedy(slots, energy_needed_kwh)

    if not chosen:
        return ChargePlan(
            start=now_utc, end=deadline_utc, duration_minutes=0,
            energy_kwh=0, estimated_cost_sek=0, avg_price_ore_kwh=0,
            feasible=False, message="No suitable intervals found.",
        )

    # Sort chosen slots chronologically for display and interval merging
    chosen.sort(key=lambda s: s["time"])

    actual_energy = sum(s["energy_kwh"] for s in chosen)
    actual_cost   = sum(s["cost"]       for s in chosen)
    start_dt      = chosen[0]["time"]
    end_dt        = chosen[-1]["time"] + interval_duration
    active_ivs    = _merge_intervals(chosen, interval_duration)
    # Duration = sum of active intervals, not envelope
    active_minutes = sum(
        int((e - s).total_seconds() / 60) for s, e in active_ivs
    )
    avg_price_ore = (actual_cost / actual_energy * 100) if actual_energy > 0 else 0.0

    # Build human-readable message
    window_strs = [
        f"{s.astimezone().strftime('%H:%M')}–{e.astimezone().strftime('%H:%M')}"
        for s, e in active_ivs
    ]
    windows_desc = ", ".join(window_strs)
    msg = (
        f"Charge {actual_energy:.1f} kWh in {len(active_ivs)} window(s): "
        f"{windows_desc} "
        f"({active_minutes} min active) at avg {avg_price_ore:.1f} öre/kWh, "
        f"cost ≈ {actual_cost:.2f} SEK."
    )
    if _partial:
        msg += " (Warning: insufficient future data – plan may be incomplete.)"

    _LOGGER.info("[ChargePlanner] %s", msg)

    return ChargePlan(
        start=start_dt,
        end=end_dt,
        duration_minutes=active_minutes,
        energy_kwh=round(actual_energy, 3),
        estimated_cost_sek=round(actual_cost, 2),
        avg_price_ore_kwh=round(avg_price_ore, 1),
        intervals=[
            {
                "time":          s["time"].isoformat(),
                "price_ore_kwh": round(s["price"] * 100, 2),
                "power_kw":      round(s["power_kw"], 2),
                "energy_kwh":    round(s["energy_kwh"], 4),
            }
            for s in chosen
        ],
        active_intervals=active_ivs,
        feasible=feasible,
        partial=_partial,
        message=msg,
    )
