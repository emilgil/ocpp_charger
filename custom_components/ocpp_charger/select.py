"""Select platform for OCPP EV Charger – charge mode and active vehicle."""
from __future__ import annotations

from typing import cast

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OCPPCoordinator
from .const import (
    ADHOC_VEHICLE_NAME, CHARGE_MODES, CONF_CHARGER_ID, CONF_VEHICLES,
    DOMAIN, PLANNER_ALGORITHMS, PLANNER_ALGO_GREEDY, SELECT_PLANNER_ALGORITHM,
    VEHICLE_CAPACITY, VEHICLE_NAME, VEHICLE_SOC_ENTITY,
)


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
        name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    entities: list[SelectEntity] = [
        ChargeModeSelect(coordinator, entry),
        PlannerAlgorithmSelect(coordinator, entry),
    ]

    vehicles = entry.data.get(CONF_VEHICLES, [])
    if len(vehicles) > 1:
        entities.append(ActiveVehicleSelect(coordinator, entry))

    async_add_entities(entities)


class ChargeModeSelect(SelectEntity):
    """Select the charging mode."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_charge_mode"
        self._attr_name = "Charging Mode"
        self._attr_options = CHARGE_MODES
        self._attr_icon = "mdi:ev-station"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    @property
    def current_option(self) -> str:
        return self._coordinator.charge_mode

    async def async_select_option(self, option: str) -> None:
        self._coordinator.set_charge_mode(option)


class ActiveVehicleSelect(SelectEntity):
    """Select which vehicle is currently connected to the charger.

    Switching the vehicle instantly updates battery capacity and the
    SOC entity used for estimation – no restart needed.
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_active_vehicle"
        self._attr_name = "Active Vehicle"
        self._attr_icon = "mdi:car-electric"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True
        self._attr_options = self._build_options()

    def _build_options(self) -> list[str]:
        vehicles = self._entry.data.get(CONF_VEHICLES, [])
        opts = [self._label(v) for v in vehicles]
        opts.append(ADHOC_VEHICLE_NAME)
        return opts

    @staticmethod
    def _label(v: dict) -> str:
        return f"{v[VEHICLE_NAME]} – {v[VEHICLE_CAPACITY]} kWh"

    @property
    def current_option(self) -> str | None:
        v = self._coordinator.active_vehicle
        return self._label(v) if v else None

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "auto_detection_reason": getattr(
                self._coordinator, "_last_detection_reason", ""
            ),
            "auto_detection_enabled": self._coordinator.auto_vehicle_detection,
        }

    async def async_select_option(self, option: str) -> None:
        """Find the vehicle matching the label and activate it."""
        if option == ADHOC_VEHICLE_NAME:
            self._coordinator.set_active_vehicle({
                VEHICLE_NAME: ADHOC_VEHICLE_NAME,
                VEHICLE_CAPACITY: self._coordinator.battery_capacity_kwh,
                VEHICLE_SOC_ENTITY: "",
            })
            # Signal UI to prompt for capacity update via the number entity
            self._coordinator.adhoc_vehicle_active = True
            self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)
            return
        self._coordinator.adhoc_vehicle_active = False
        vehicles = self._entry.data.get(CONF_VEHICLES, [])
        for v in vehicles:
            if self._label(v) == option:
                self._coordinator.set_active_vehicle(v)
                return


class PlannerAlgorithmSelect(SelectEntity):
    """Select the charge planning algorithm."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._entry = entry  # Bug 11: needed for persisting to entry.data
        self._attr_unique_id = f"{entry.entry_id}_{SELECT_PLANNER_ALGORITHM}"
        self._attr_name = "Planning Algorithm"
        self._attr_options = PLANNER_ALGORITHMS
        self._attr_icon = "mdi:chart-timeline-variant"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    @property
    def current_option(self) -> str:
        return self._coordinator.planner_algorithm

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "description": (
                "Greedy picks globally cheapest slots (may pause/resume). "
                "Contiguous finds the single cheapest block."
            ),
        }

    async def async_select_option(self, option: str) -> None:
        self._coordinator.planner_algorithm = option
        # Bug 11: Persist to entry.data so value survives HA restart
        new_data = dict(self._entry.data)
        new_data[SELECT_PLANNER_ALGORITHM] = option
        self._coordinator.hass.config_entries.async_update_entry(
            self._entry, data=new_data
        )
        # Force immediate replan
        self._coordinator._last_plan_update = None
        self._coordinator._update_charge_plan()
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)
