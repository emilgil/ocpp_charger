"""Feature 3: build Charge Windows sensor data from a ChargePlan.

Pure, stdlib-only helpers so they can be unit-tested standalone (like
charge_planner.py) without importing Home Assistant.  OCPPCoordinator wraps
these functions, supplying the HA state they need.
"""
from __future__ import annotations

from datetime import datetime, timezone


def build_charge_windows(
    active_intervals: list[tuple[datetime, datetime]],
    intervals: list[dict],
    existing_slots: list[dict],
    now: datetime,
    local_tz,
) -> list[dict]:
    """Build per-slot charge-window dicts from a feasible ChargePlan.

    Args:
        active_intervals: merged (start, end) aware-UTC tuples (plan.active_intervals).
        intervals:        plan.intervals — list of
                          {"time": iso, "price_ore_kwh": float, "energy_kwh": float, ...}.
        existing_slots:   previous window dicts; actual_energy_kwh is preserved
                          when a new slot shares the same start-ISO.
        now:              aware datetime, used for the `completed` flag.
        local_tz:         tzinfo used to render start/end ISO strings.

    Returns:
        A new list of slot dicts (does not mutate inputs).
    """
    now_utc = now.astimezone(timezone.utc)
    existing = {s["start"]: s for s in existing_slots}
    new_slots: list[dict] = []

    for iv_start, iv_end in active_intervals:
        matching = [
            iv for iv in intervals
            if iv_start <= datetime.fromisoformat(iv["time"]) < iv_end
        ]
        planned_e = round(sum(iv["energy_kwh"] for iv in matching), 3)
        total_cost = sum(iv["price_ore_kwh"] * iv["energy_kwh"] for iv in matching)
        avg_price = round(total_cost / planned_e, 1) if planned_e > 0 else 0.0

        start_iso = iv_start.astimezone(local_tz).isoformat()
        end_iso = iv_end.astimezone(local_tz).isoformat()

        prev = existing.get(start_iso)
        actual_e = prev["actual_energy_kwh"] if prev else None

        new_slots.append({
            "start": start_iso,
            "end": end_iso,
            "planned_energy_kwh": planned_e,
            "avg_price_ore_kwh": avg_price,
            "actual_energy_kwh": actual_e,
            "completed": now_utc > iv_end,
        })

    return new_slots


def update_windows_actual(
    windows: list[dict],
    snapshots: dict[str, float],
    current_cumulative_kwh: float,
    now: datetime,
) -> None:
    """Post-hoc fill actual_energy_kwh for completed slots (mutates in place).

    Args:
        windows:                slot dicts from build_charge_windows().
        snapshots:              start-ISO → cumulative kWh captured at slot start.
                                Mutated as slots are entered.
        current_cumulative_kwh: total energy delivered this cable session so far.
        now:                    aware datetime.

    A slot's actual energy is the cumulative energy at slot end minus the
    cumulative energy snapshotted when `now` first reached the slot start.
    """
    now_utc = now.astimezone(timezone.utc)

    for slot in windows:
        start_iso = slot["start"]
        try:
            slot_start = datetime.fromisoformat(slot["start"]).astimezone(timezone.utc)
            slot_end = datetime.fromisoformat(slot["end"]).astimezone(timezone.utc)
        except (ValueError, KeyError):
            continue

        # Capture the baseline once we have entered the slot.
        if now_utc >= slot_start and start_iso not in snapshots:
            snapshots[start_iso] = current_cumulative_kwh

        if now_utc <= slot_end:
            continue  # slot not finished yet

        slot["completed"] = True
        if slot["actual_energy_kwh"] is None:
            # If we never observed the start (e.g. plan built after the slot, or
            # HA restart), default the baseline to "now" → actual 0 rather than a
            # bogus large value.
            baseline = snapshots.get(start_iso, current_cumulative_kwh)
            slot["actual_energy_kwh"] = max(
                0.0, round(current_cumulative_kwh - baseline, 3)
            )
