"""Estimate current SOC from energy delivered (Bug 8 / Bug 29).

Pure, stdlib-only helper (mirrors charge_planner.py / deadline.py) so it can be
unit-tested standalone without importing Home Assistant.

Vehicles whose SOC entity doesn't refresh during charging (e.g. Kia eNiro, Skoda
Enyaq) need their current SOC estimated from the session start SOC plus the energy
delivered this cable session. Both the charge planner and the goal-reached check
consume this so they can't drift (Bug 29: the goal check previously compared
delivered energy against the plan's *remaining* energy, which double-counted the
delivered energy and stopped charging at the SOC midpoint).
"""
from __future__ import annotations


def estimate_soc(
    start_soc: float | None,
    already_charged_kwh: float,
    capacity_kwh: float,
    efficiency: float,
    reported_soc: float | None,
) -> float | None:
    """Return the estimated current SOC %, or ``reported_soc`` when no basis exists.

    Estimation applies only with a known start SOC, positive delivered energy and a
    positive capacity. The result is **uncapped** – callers cap against the target
    themselves when they need to (the planner does; the goal check compares directly).
    """
    if start_soc is not None and already_charged_kwh > 0 and capacity_kwh > 0:
        return start_soc + already_charged_kwh * efficiency / capacity_kwh * 100.0
    return reported_soc
