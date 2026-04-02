"""Sensor platform for OCPP EV Charger."""
from __future__ import annotations

from typing import cast

import zoneinfo
from datetime import datetime, timezone
from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfTime,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo, EntityCategory
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OCPPCoordinator
from .const import (
    CONF_CHARGER_ID,
    CONF_HOST,
    CONF_PORT,
    DOMAIN,
    SENSOR_CABLE,
    SENSOR_CURRENT,
    SENSOR_ELAPSED,
    SENSOR_ENERGY,
    SENSOR_ETA,
    SENSOR_POWER,
    SENSOR_PRICE,
    SENSOR_SESSION_ID,
    SENSOR_SOC,
    SENSOR_STATUS,
    SENSOR_TOTAL_COST,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up OCPP sensors."""
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]

    entities = [
        ChargerStatusSensor(coordinator, entry),
        SchedulePeriodSensor(coordinator, entry),
        ChargerPowerSensor(coordinator, entry),
        ChargerCurrentSensor(coordinator, entry),
        ChargerEnergySensor(coordinator, entry),
        ChargerSOCSensor(coordinator, entry),
        ChargerElapsedSensor(coordinator, entry),
        ChargerETASensor(coordinator, entry),
        ChargerPriceSensor(coordinator, entry),
        ChargerSessionIDSensor(coordinator, entry),
        ChargerSessionStartSensor(coordinator, entry),
        ChargerSessionEndSensor(coordinator, entry),
        PlannedChargeEndSensor(coordinator, entry),
        PlannedChargeStartSensor(coordinator, entry),
        EstimatedChargeCostSensor(coordinator, entry),
        PlannedChargeEnergySensor(coordinator, entry),
        SessionCostSensor(coordinator, entry),
        ChargeGoalAchievableSensor(coordinator, entry),
        ChargeCapacitySensor(coordinator, entry),
        PlannerSavingsSensor(coordinator, entry),
        TotalChargingCostSensor(coordinator, entry),
    ]
    async_add_entities(entities)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
        name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        manufacturer="OCPP Charger",
        model="OCPP 1.6",
        configuration_url=f"http://{entry.data[CONF_HOST]}:{entry.data[CONF_PORT]}",
    )


class OCPPSensorBase(CoordinatorEntity, SensorEntity):
    """Base class for OCPP sensors."""

    def __init__(
        self,
        coordinator: OCPPCoordinator,
        entry: ConfigEntry,
        unique_suffix: str,
        name: str,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_{unique_suffix}"
        self._attr_name = name
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    @property
    def _coord(self) -> "OCPPCoordinator":
        return cast("OCPPCoordinator", self.coordinator)


class ChargerStatusSensor(OCPPSensorBase):
    """Charger connector status."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_STATUS, "Status")

    @property
    def native_value(self) -> str:
        return self._coord.ocpp.state.connector_status

    @property
    def icon(self) -> str:
        status = self._coord.ocpp.state.connector_status
        icons = {
            "Available": "mdi:ev-plug-type2",
            "Preparing": "mdi:power-plug",
            "Charging": "mdi:battery-charging",
            "SuspendedEV": "mdi:pause-circle",
            "SuspendedEVSE": "mdi:pause-circle-outline",
            "Finishing": "mdi:check-circle",
            "Faulted": "mdi:alert-circle",
        }
        return icons.get(status, "mdi:ev-station")


class ChargerPowerSensor(OCPPSensorBase):
    """Active charging power in Watts."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_POWER, "Charging Power")
        self._attr_native_unit_of_measurement = UnitOfPower.WATT
        self._attr_device_class = SensorDeviceClass.POWER
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:lightning-bolt"

    @property
    def native_value(self) -> float:
        return round(self._coord.ocpp.state.power_w, 1)


class ChargerCurrentSensor(OCPPSensorBase):
    """Active charging current."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_CURRENT, "Charging Current")
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_device_class = SensorDeviceClass.CURRENT
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:current-ac"

    @property
    def native_value(self) -> float:
        return round(self._coord.ocpp.state.current_a, 2)


class ChargerEnergySensor(OCPPSensorBase):
    """Energy delivered this session (kWh)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_ENERGY, "Session Energy")
        self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
        self._attr_device_class = SensorDeviceClass.ENERGY
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING
        self._attr_icon = "mdi:battery-charging-100"

    @property
    def native_value(self):
        # Bug 6: Use cable session energy (accumulated across OCPP transactions)
        cable_energy = self._coord._cable_session_energy_kwh
        tx_energy = self._coord.ocpp.state.energy_kwh
        # During active tx, add current tx energy to accumulated cable session
        if self._coord.ocpp.state.transaction_id is not None and tx_energy > 0:
            return round(cable_energy + tx_energy, 3)
        if cable_energy > 0:
            return round(cable_energy, 3)
        if self._coord.ocpp.state.session_energy_start is None:
            return None
        return round(tx_energy, 3)



class ChargerSOCSensor(OCPPSensorBase):
    """State of charge of the vehicle battery (%)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_SOC, "Battery Level")
        self._attr_native_unit_of_measurement = "%"
        self._attr_device_class = SensorDeviceClass.BATTERY
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        soc = self._coord.ocpp.state.soc_percent
        return round(soc, 1) if soc is not None else None


class ChargerElapsedSensor(OCPPSensorBase):
    """Elapsed charging time in minutes."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_ELAPSED, "Charging Time")
        self._attr_native_unit_of_measurement = UnitOfTime.MINUTES
        self._attr_icon = "mdi:timer"

    @property
    def native_value(self) -> int | None:
        elapsed = self._coord.elapsed_seconds
        if elapsed is None:
            return None
        return elapsed // 60

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        elapsed = self._coord.elapsed_seconds
        if elapsed is None:
            return {}
        hours = elapsed // 3600
        minutes = (elapsed % 3600) // 60
        seconds = elapsed % 60
        return {"formatted": f"{hours:02d}:{minutes:02d}:{seconds:02d}"}


class ChargerETASensor(OCPPSensorBase):
    """Estimated time of charging completion."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_ETA, "Estimated Completion")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-end"

    @property
    def native_value(self) -> datetime | None:
        return self._coord.estimated_completion


class ChargerPriceSensor(OCPPSensorBase):
    """Current electricity price in öre/kWh (Swedish energy market unit)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_PRICE, "Current Electricity Price")
        self._attr_native_unit_of_measurement = "öre/kWh"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_icon = "mdi:currency-usd"

    @property
    def native_value(self) -> float | None:
        p = self._coord.current_price
        return round(p, 2) if p is not None else None


class ChargerSessionIDSensor(OCPPSensorBase):
    """Active session ID."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_SESSION_ID, "Session ID")
        self._attr_icon = "mdi:identifier"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str | None:
        return self._coord.ocpp.state.session_id


class SchedulePeriodSensor(OCPPSensorBase):
    """Current schedule period: Day / Night / Override."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "schedule_period", "Charging Period")
        self._attr_icon = "mdi:clock-time-four"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> str:
        return self._coord.schedule.period_name()

    @property
    def extra_state_attributes(self) -> dict:
        s = self._coord.schedule
        return {
            "day_start":        str(s.day_start),
            "night_start":       str(s.night_start),
            "day_current_a":      s.day_current_a,
            "night_current_a":     s.night_current_a,
            "current_limit_a":  s.current_limit(),
        }


class ChargerSessionStartSensor(OCPPSensorBase):
    """Timestamp when the current charging session started."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "session_start", "Session Start")
        self._attr_device_class = SensorDeviceClass.TIMESTAMP
        self._attr_icon = "mdi:clock-start"
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> datetime | None:
        return self._coord.ocpp.state.session_start

    @property
    def available(self) -> bool:
        return self._coord.ocpp.state.session_start is not None


class ChargerSessionEndSensor(OCPPSensorBase):
    """Estimated remaining charging time as a human-readable string."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "session_end", "Estimated Charge Time Remaining")
        self._attr_device_class = None
        self._attr_icon = "mdi:timer-outline"

    @property
    def native_value(self) -> str | None:
        total_minutes = self._coord.estimated_remaining_minutes
        if total_minutes is None:
            return None
        if total_minutes == 0:
            return "0 min"
        hours, minutes = divmod(total_minutes, 60)
        if hours > 0:
            return f"{hours} h {minutes} min" if minutes else f"{hours} h"
        return f"{minutes} min"

    @property
    def available(self) -> bool:
        return self._coord.estimated_remaining_minutes is not None


class PlannedChargeStartSensor(OCPPSensorBase):
    """Optimal charge window start time."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "planned_charge_start", "Planned Charge Start")
        self._attr_device_class = None
        self._attr_icon = "mdi:clock-start"

    @property
    def native_value(self) -> str | None:
        plan = self._coord.charge_plan
        if not plan or not plan.feasible or not plan.start:
            return None
        try:
            local_tz = zoneinfo.ZoneInfo(self._coord.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc
        return plan.start.astimezone(local_tz).strftime("%H:%M")

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._coord.charge_plan
        if not plan:
            return {}
        try:
            local_tz = zoneinfo.ZoneInfo(self._coord.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc
        windows = [
            f"{s.astimezone(local_tz).strftime('%H:%M')}–{e.astimezone(local_tz).strftime('%H:%M')}"
            for s, e in plan.active_intervals
        ] if plan.active_intervals else []
        return {
            "feasible":            plan.feasible,
            "message":             plan.message,
            "energy_kwh":          plan.energy_kwh,
            "estimated_cost_sek":  plan.estimated_cost_sek,
            "avg_price_ore_kwh":   plan.avg_price_ore_kwh,
            "duration_minutes":    plan.duration_minutes,
            "active_windows":      windows,
        }


class PlannedChargeEndSensor(OCPPSensorBase):
    """Optimal charge window end time."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "planned_charge_end", "Planned Charge End")
        self._attr_device_class = None
        self._attr_icon = "mdi:clock-check"

    @property
    def native_value(self) -> str | None:
        plan = self._coord.charge_plan
        if not plan or not plan.feasible or not plan.end:
            return None
        try:
            local_tz = zoneinfo.ZoneInfo(self._coord.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc
        return plan.end.astimezone(local_tz).strftime("%H:%M")

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._coord.charge_plan
        if not plan:
            return {}
        return {
            "feasible":          plan.feasible,
            "message":           plan.message,
            "num_intervals":     len(plan.intervals),
            "intervals":         plan.intervals[:8],  # first 8 for display
        }


class EstimatedChargeCostSensor(OCPPSensorBase):
    """Estimated total cost for the planned charge session (from charge planner)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "estimated_charge_cost", "Estimated Charge Cost")
        self._attr_native_unit_of_measurement = "SEK"
        self._attr_icon = "mdi:cash-clock"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        plan = self._coord.charge_plan
        if plan and plan.feasible and plan.estimated_cost_sek is not None:
            return round(plan.estimated_cost_sek, 2)
        return None


class PlannedChargeEnergySensor(OCPPSensorBase):
    """Planned charge energy in kWh (from charge planner)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "planned_charge_energy", "Planned Charge Energy")
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_icon = "mdi:lightning-bolt"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def native_value(self) -> float | None:
        plan = self._coord.charge_plan
        if plan and plan.feasible and plan.energy_kwh:
            return round(plan.energy_kwh, 2)
        return None


class SessionCostSensor(OCPPSensorBase):
    """Accumulated actual cost for the current charging session."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "session_cost", "Session Cost")
        self._attr_native_unit_of_measurement = "SEK"
        self._attr_icon = "mdi:cash"
        self._attr_state_class = SensorStateClass.TOTAL_INCREASING

    @property
    def native_value(self) -> float | None:
        # Bug 6: Use cable session cost (accumulated across OCPP transactions)
        state = self._coord.ocpp.state
        cable_cost = self._coord._cable_session_cost_sek
        tx_cost = state.accumulated_cost
        if state.transaction_id is not None and tx_cost > 0:
            return round(cable_cost + tx_cost, 2)
        if cable_cost > 0:
            return round(cable_cost, 2)
        if state.session_start is None:
            return None
        return round(tx_cost, 2)

    @property
    def available(self) -> bool:
        return (self._coord.ocpp.state.session_start is not None
                or self._coord._cable_session_cost_sek > 0)


class TotalChargingCostSensor(OCPPSensorBase):
    """Cumulative total charging cost across all sessions (SEK)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, SENSOR_TOTAL_COST, "Total Charging Cost")
        self._attr_native_unit_of_measurement = "SEK"
        self._attr_state_class = SensorStateClass.TOTAL
        self._attr_icon = "mdi:cash-multiple"

    @property
    def native_value(self) -> float | None:
        return round(self._coord.ocpp.state.total_cost, 2)


class ChargeGoalAchievableSensor(OCPPSensorBase):
    """Whether the charge target can be reached within the planned window."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charge_goal_achievable", "Charge Goal Achievable")
        self._attr_icon = "mdi:check-circle-outline"

    @property
    def native_value(self) -> bool | None:
        plan = self._coord.charge_plan
        if plan is None:
            return None
        if not plan.feasible:
            return False
        return not plan.partial

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._coord.charge_plan
        if not plan:
            return {}
        return {
            "feasible": plan.feasible,
            "partial": plan.partial,
            "message": plan.message,
        }


class ChargeCapacitySensor(OCPPSensorBase):
    """How much can be charged within the planned window, as % of target."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charge_capacity", "Chargeable Amount")
        self._attr_native_unit_of_measurement = "%"
        self._attr_icon = "mdi:battery-arrow-up"
        self._attr_state_class = SensorStateClass.MEASUREMENT

    @property
    def available(self) -> bool:
        plan = self._coord.charge_plan
        return plan is not None and plan.feasible

    @property
    def native_value(self) -> float | None:
        plan = self._coord.charge_plan
        if plan is None or not plan.feasible:
            return None
        coord = self._coord
        current_soc = coord.ocpp.state.soc_percent
        if current_soc is None:
            return None
        target_soc = float(coord.target_soc) if coord.target_soc > 0 else 80.0
        soc_needed = max(0.0, target_soc - current_soc)
        if soc_needed <= 0:
            return 100.0
        # How much SoC % can we add with the planned energy?
        capacity_kwh = coord.battery_capacity_kwh
        if capacity_kwh <= 0:
            return None
        from .const import DEFAULT_CHARGE_EFFICIENCY
        achievable_soc = (plan.energy_kwh * DEFAULT_CHARGE_EFFICIENCY / capacity_kwh) * 100.0
        pct_of_target = min(100.0, round(achievable_soc / soc_needed * 100.0, 1))
        return pct_of_target

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._coord.charge_plan
        if not plan or not plan.feasible:
            return {}
        coord = self._coord
        current_soc = coord.ocpp.state.soc_percent or 0.0
        target_soc = float(coord.target_soc) if coord.target_soc > 0 else 80.0
        return {
            "energy_kwh": plan.energy_kwh,
            "current_soc": current_soc,
            "target_soc": target_soc,
            "achievable_soc": round(current_soc + (plan.energy_kwh / coord.battery_capacity_kwh * 100.0), 1) if coord.battery_capacity_kwh > 0 else None,
        }


class PlannerSavingsSensor(OCPPSensorBase):
    """Difference in estimated cost between Greedy and Contiguous planning.

    Positive value means Greedy is cheaper; negative means Contiguous is cheaper.
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "planner_savings", "Planner Savings")
        self._attr_native_unit_of_measurement = "SEK"
        self._attr_icon = "mdi:scale-balance"
        self._attr_state_class = SensorStateClass.MEASUREMENT
        self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self) -> float | None:
        plan = self._coord.charge_plan
        alt = getattr(self._coord, "_alt_plan", None)
        if not plan or not alt or not plan.feasible or not alt.feasible:
            return None
        from .const import PLANNER_ALGO_GREEDY
        if self._coord.planner_algorithm == PLANNER_ALGO_GREEDY:
            # active=greedy, alt=contiguous → savings = contiguous - greedy
            return round(alt.estimated_cost_sek - plan.estimated_cost_sek, 2)
        else:
            # active=contiguous, alt=greedy → savings = contiguous - greedy
            return round(plan.estimated_cost_sek - alt.estimated_cost_sek, 2)

    @property
    def extra_state_attributes(self) -> dict:
        plan = self._coord.charge_plan
        alt = getattr(self._coord, "_alt_plan", None)
        attrs: dict[str, Any] = {
            "active_algorithm": self._coord.planner_algorithm,
        }
        if plan and plan.feasible:
            attrs["active_cost_sek"] = plan.estimated_cost_sek
            attrs["active_avg_ore_kwh"] = plan.avg_price_ore_kwh
            attrs["active_windows"] = len(plan.active_intervals)
        if alt and alt.feasible:
            attrs["alt_cost_sek"] = alt.estimated_cost_sek
            attrs["alt_avg_ore_kwh"] = alt.avg_price_ore_kwh
            attrs["alt_windows"] = len(alt.active_intervals)
        return attrs
