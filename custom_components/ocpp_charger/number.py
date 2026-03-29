"""Number platform for OCPP EV Charger."""
from __future__ import annotations

from typing import cast

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfElectricCurrent
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OCPPCoordinator
from .const import CONF_BATTERY_CAPACITY, CONF_CHARGER_ID, CONF_MAX_CURRENT, DEFAULT_BATTERY_CAPACITY_KWH, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up number entities."""
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            MaxCurrentNumber(coordinator, entry),
            TargetSOCNumber(coordinator, entry),
            TargetKWhNumber(coordinator, entry),
            BatteryCapacityNumber(coordinator, entry),
            OverrideCurrentNumber(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
        name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
    )


class MaxCurrentNumber(CoordinatorEntity, NumberEntity):
    """Set maximum charging current."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_max_current_limit"
        self._attr_name = "Max Charging Current"
        self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE
        self._attr_native_min_value = 6.0
        self._attr_native_max_value = float(entry.data.get(CONF_MAX_CURRENT, 32))
        self._attr_native_step = 1.0
        self._attr_mode = NumberMode.SLIDER
        self._attr_icon = "mdi:current-ac"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True


    @property
    def native_value(self) -> float:
        return self._coordinator.max_current

    async def async_set_native_value(self, value: float) -> None:
        await self._coordinator.async_set_max_current(value)


class TargetSOCNumber(CoordinatorEntity, NumberEntity):
    """Target state-of-charge in percent."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_target_soc"
        self._attr_name = "Target Battery Level"
        self._attr_native_unit_of_measurement = "%"
        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 5.0
        self._attr_mode = NumberMode.SLIDER
        self._attr_icon = "mdi:battery-80"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True


    @property
    def native_value(self) -> float:
        return self._coordinator.target_soc

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_target_soc(value)


class TargetKWhNumber(CoordinatorEntity, NumberEntity):
    """Target energy to charge in kWh (0 = unlimited)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_target_kwh"
        self._attr_name = "Target Energy"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_native_min_value = 0.0
        self._attr_native_max_value = 100.0
        self._attr_native_step = 0.5
        self._attr_mode = NumberMode.BOX
        self._attr_icon = "mdi:battery-plus"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True


    @property
    def native_value(self) -> float:
        return self._coordinator.target_kwh

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.set_target_kwh(value)


class BatteryCapacityNumber(CoordinatorEntity, NumberEntity):
    """Configurable battery capacity used for SOC estimation."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_battery_capacity"
        self._attr_name = "Battery Capacity"
        self._attr_native_unit_of_measurement = "kWh"
        self._attr_native_min_value = 5.0
        self._attr_native_max_value = 200.0
        self._attr_native_step = 0.5
        self._attr_mode = NumberMode.BOX
        self._attr_icon = "mdi:battery"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True


    @property
    def native_value(self) -> float:
        return self._coordinator.battery_capacity_kwh

    @property
    def name(self) -> str:
        if self._coordinator.adhoc_vehicle_active:
            return "Battery Capacity (New Vehicle)"
        return "Battery Capacity"

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.battery_capacity_kwh = value
        # If adhoc vehicle is active, keep its capacity in sync
        if self._coordinator.adhoc_vehicle_active and self._coordinator.active_vehicle:
            self._coordinator.active_vehicle["capacity_kwh"] = value
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)


class OverrideCurrentNumber(CoordinatorEntity, NumberEntity):
    """Manual current override used when schedule override switch is on.

    When 'New Vehicle' is active this also doubles as the ad-hoc capacity input:
    changing it immediately updates the active vehicle's capacity.
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_override_current"
        self._attr_name = "Override Current"
        self._attr_native_unit_of_measurement = "A"
        self._attr_native_min_value = 6.0
        self._attr_native_max_value = float(entry.data.get(CONF_MAX_CURRENT, 32))
        self._attr_native_step = 1.0
        self._attr_mode = NumberMode.SLIDER
        self._attr_icon = "mdi:current-ac"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True


    @property
    def native_value(self) -> float:
        return self._coordinator.schedule.override_current_a

    async def async_set_native_value(self, value: float) -> None:
        self._coordinator.schedule.set_override(
            self._coordinator.schedule.override_active, current_a=value
        )
        if self._coordinator.schedule.override_active and self._coordinator.ocpp.state.charging:
            await self._coordinator.ocpp.set_charging_limit(value)
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)
