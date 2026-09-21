"""Automatic vehicle identification logic."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from .const import (
    AUTO_DETECT_SOC_TOLERANCE,
    PLUG_OUTCOME_MATCHED,
    PLUG_OUTCOME_NO_SENSORS,
    PLUG_OUTCOME_NOTIFY,
    PLUG_OUTCOME_WAIT,
    PLUG_REASON_MULTIPLE,
    PLUG_REASON_NONE,
    PLUG_REASON_PARTIAL,
    PLUG_STATE_OFF,
    PLUG_STATE_ON,
    VEHICLE_CAPACITY,
    VEHICLE_NAME,
    VEHICLE_PLUG_ENTITY,
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


@dataclass(frozen=True)
class PlugDetection:
    """Result of identify_by_plug_sensor (Feature 10)."""

    outcome: str                    # PLUG_OUTCOME_*
    vehicle: dict | None = None     # the matching vehicle dict (same object as in the list) for "matched"
    reason: str = ""                # human-readable, for _last_detection_reason
    reason_code: str | None = None  # PLUG_REASON_* for "notify" and "wait"


def identify_by_plug_sensor(vehicles: list[dict], hass: "HomeAssistant") -> PlugDetection:
    """
    Feature 10: decide which vehicle is plugged in from each vehicle's optional "plugged in" sensor.

    A sensor is usable when its state is "on" or "off"; unavailable/unknown/empty or a missing entity means the
    vehicle isn't covered. P = vehicles whose usable sensor shows "on".

      no vehicle has a sensor configured    → no_sensors  (caller keeps the SoC logic)
      |P| = 1 and every vehicle covered     → matched
      |P| = 1 and some vehicle not covered  → notify / partial_sensors
      |P| >= 2                              → notify / multiple_plugged
      |P| = 0                               → wait   / none_plugged  (the caller decides how long to wait)

    Pure: no timers, no notifications, no state. identify_vehicle() (SoC) is untouched.
    """
    if not any(v.get(VEHICLE_PLUG_ENTITY, "") for v in vehicles):
        return PlugDetection(PLUG_OUTCOME_NO_SENSORS, reason="No plug sensor configured")

    plugged: list[dict] = []
    usable = 0
    readings: list[str] = []
    for v in vehicles:
        name = v.get(VEHICLE_NAME, "?")
        entity_id = v.get(VEHICLE_PLUG_ENTITY, "")
        if not entity_id:
            readings.append(f"{name}=(no sensor)")
            continue
        state = hass.states.get(entity_id)
        raw = state.state if state is not None else None
        readings.append(f"{name}={entity_id}:{raw!r}")
        if raw in (PLUG_STATE_ON, PLUG_STATE_OFF):
            usable += 1
            if raw == PLUG_STATE_ON:
                plugged.append(v)
    all_covered = usable == len(vehicles)
    _LOGGER.info("[VehicleDetect] Plug sensors: %s", ", ".join(readings))

    if len(plugged) >= 2:
        names = ", ".join(v.get(VEHICLE_NAME, "?") for v in plugged)
        result = PlugDetection(
            PLUG_OUTCOME_NOTIFY,
            reason=f"Several vehicles show plugged in: {names}",
            reason_code=PLUG_REASON_MULTIPLE,
        )
    elif len(plugged) == 1 and all_covered:
        v = plugged[0]
        result = PlugDetection(
            PLUG_OUTCOME_MATCHED,
            vehicle=v,
            reason=f"Plug sensor {v[VEHICLE_PLUG_ENTITY]} shows {v.get(VEHICLE_NAME, '?')} plugged in",
        )
    elif len(plugged) == 1:
        result = PlugDetection(
            PLUG_OUTCOME_NOTIFY,
            reason="Plug sensors don't cover every vehicle",
            reason_code=PLUG_REASON_PARTIAL,
        )
    else:
        result = PlugDetection(
            PLUG_OUTCOME_WAIT,
            reason="No plug sensor shows plugged in",
            reason_code=PLUG_REASON_NONE,
        )
    _LOGGER.info("[VehicleDetect] Plug decision: %s – %s", result.outcome, result.reason)
    return result
