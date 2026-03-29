"""Automatic vehicle identification logic."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import (
    AUTO_DETECT_SOC_TOLERANCE,
    VEHICLE_CAPACITY,
    VEHICLE_NAME,
    VEHICLE_SOC_ENTITY,
)

_LOGGER = logging.getLogger(__name__)


def identify_vehicle(
    vehicles: list[dict],
    ocpp_soc: float | None,
    hass: "HomeAssistant",
) -> tuple[dict | None, str]:
    """
    Identify which vehicle is most likely connected.

    Strategy (in order):
      1. If OCPP reports SOC: find the vehicle whose SOC-entity value
         is within AUTO_DETECT_SOC_TOLERANCE % of the OCPP value.
      2. Fallback: pick the vehicle with the lowest SOC-entity reading.
      3. Final fallback: first vehicle in list (no SOC data available).

    Returns (vehicle_dict, reason_string).
    """
    if not vehicles:
        return None, "No vehicles registered"

    if len(vehicles) == 1:
        v = vehicles[0]
        return v, f"Only registered vehicle: {v[VEHICLE_NAME]}"

    # Build mapping: vehicle → current SOC from its entity (if available)
    soc_map: dict[int, float] = {}
    for i, v in enumerate(vehicles):
        entity_id = v.get(VEHICLE_SOC_ENTITY, "")
        if not entity_id:
            continue
        state = hass.states.get(entity_id)
        if state and state.state not in ("unavailable", "unknown", ""):
            try:
                soc_map[i] = float(state.state)
            except ValueError:
                _LOGGER.debug("[VehicleDetect] Could not read SOC from entity %s", entity_id)

    # ── Strategy 1: OCPP SOC match ────────────────────────────────────
    if ocpp_soc is not None and soc_map:
        best_idx: int | None = None
        best_diff = float("inf")
        for idx, entity_soc in soc_map.items():
            diff = abs(entity_soc - ocpp_soc)
            if diff < best_diff:
                best_diff = diff
                best_idx = idx

        if best_idx is not None and best_diff <= AUTO_DETECT_SOC_TOLERANCE:
            v = vehicles[best_idx]
            _LOGGER.info(
                "[VehicleDetect] Match via OCPP SOC: ocpp=%.1f%% vehicle=%s entity=%.1f%% diff=%.1f%%",
                ocpp_soc, v[VEHICLE_NAME], soc_map[best_idx], best_diff,
            )
            return v, (
                f"OCPP SOC {ocpp_soc:.1f}% matched {v[VEHICLE_NAME]} "
                f"({soc_map[best_idx]:.1f}%, diff {best_diff:.1f}%)"
            )

        # OCPP SOC finns men ingen entitet matched inom toleransen
        if ocpp_soc is not None and soc_map:
            _LOGGER.info(
                "OCPP SOC %.1f%% matched no vehicle within ±%.0f%% – "
                "falling back to lowest SOC",
                ocpp_soc, AUTO_DETECT_SOC_TOLERANCE,
            )

    # ── Strategy 2: lowest SOC entity ────────────────────────────────
    if soc_map:
        lowest_idx = min(soc_map, key=lambda i: soc_map[i])
        v = vehicles[lowest_idx]
        _LOGGER.info(
            "[VehicleDetect] Fallback – lowest SOC: vehicle=%s soc=%.1f%%",
            v[VEHICLE_NAME], soc_map[lowest_idx],
        )
        return v, (
            f"Lowest SOC among registered vehicles: "
            f"{v[VEHICLE_NAME]} ({soc_map[lowest_idx]:.1f}%)"
        )

    # ── Strategy 3: no data at all ────────────────────────────────────
    v = vehicles[0]
    _LOGGER.info(
        "[VehicleDetect] No SOC data – defaulting to first vehicle: %s", v[VEHICLE_NAME]
    )
    return v, f"No SOC data – default vehicle: {v[VEHICLE_NAME]}"
