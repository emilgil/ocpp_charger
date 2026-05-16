"""Switch platform for OCPP EV Charger – toggleable features."""
from __future__ import annotations

from typing import cast

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OCPPCoordinator
from .const import CONF_CHARGER_ID, DOMAIN, SWITCH_ALLOW_DAY_CHARGING, SWITCH_DEADLINE_OVERRIDE


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([
        AutoVehicleDetectionSwitch(coordinator, entry),
        ScheduleOverrideSwitch(coordinator, entry),
        AllowDayChargingSwitch(coordinator, entry),
        DeadlineOverrideSwitch(coordinator, entry),
    ])


class AutoVehicleDetectionSwitch(CoordinatorEntity, SwitchEntity):
    """Toggle automatic vehicle identification on cable connect."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_auto_vehicle_detection"
        self._attr_name = "Auto Vehicle Detection"
        self._attr_icon = "mdi:car-search"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
            name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        )
        self._attr_has_entity_name = True

    @property
    def is_on(self) -> bool:
        return self._coordinator.auto_vehicle_detection

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.auto_vehicle_detection = True
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.auto_vehicle_detection = False
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)


class ScheduleOverrideSwitch(CoordinatorEntity, SwitchEntity):
    """Override day/night schedule with a manual current level."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_schedule_override"
        self._attr_name = "Override Charging Schedule"
        self._attr_icon = "mdi:clock-edit"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
            name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        )
        self._attr_has_entity_name = True

    @property
    def is_on(self) -> bool:
        return self._coordinator.schedule.override_active

    @property
    def extra_state_attributes(self) -> dict:
        s = self._coordinator.schedule
        return {
            "day_start":   str(s.day_start),
            "night_start":  str(s.night_start),
            "day_current_a":   s.day_current_a,
            "night_current_a":  s.night_current_a,
            "override_current_a": s.override_current_a,
        }

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.schedule.set_override(True)
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.schedule.set_override(False)
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)


class AllowDayChargingSwitch(CoordinatorEntity, SwitchEntity):
    """Allow charging during daytime hours (default: off on weekdays)."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_ALLOW_DAY_CHARGING}"
        self._attr_name = "Allow Day Charging"
        self._attr_icon = "mdi:weather-sunny"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
            name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        )
        self._attr_has_entity_name = True

    @property
    def is_on(self) -> bool:
        return self._coordinator.allow_day_charging

    @property
    def extra_state_attributes(self) -> dict:
        return {
            "auto_schedule": "weekend" if self._coordinator.allow_day_charging else "weekday",
            "info": "Auto OFF Sun 18:00–Fri 18:00. Override manually if home.",
        }

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.set_allow_day_charging(True)
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.set_allow_day_charging(False)
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)


class DeadlineOverrideSwitch(CoordinatorEntity, SwitchEntity):
    """Flip the weekday/weekend default deadline.

    Weekday: OFF = 06:00 (normal), ON = no deadline (vacation/holiday).
    Weekend: OFF = no deadline (normal), ON = 06:00 (early departure).
    """

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_{SWITCH_DEADLINE_OVERRIDE}"
        self._attr_name = "Deadline Override"
        self._attr_icon = "mdi:calendar-clock"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
            name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        )
        self._attr_has_entity_name = True

    @property
    def is_on(self) -> bool:
        return self._coordinator.deadline_override

    @property
    def extra_state_attributes(self) -> dict:
        import zoneinfo
        from datetime import datetime, timezone
        try:
            local_tz = zoneinfo.ZoneInfo(self._coordinator.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc
        is_weekend = datetime.now(local_tz).weekday() >= 5
        if self._coordinator.deadline_override:
            effective = "06:00 deadline" if is_weekend else "Ingen deadline (semester)"
        else:
            effective = "Ingen deadline (helg)" if is_weekend else "06:00 deadline (vardag)"
        return {
            "override_active": self._coordinator.deadline_override,
            "effective_mode": effective,
        }

    async def async_turn_on(self, **kwargs) -> None:
        self._coordinator.deadline_override = True
        self._coordinator._last_plan_update = None  # bypass throttle so deadline takes effect now
        self._coordinator._update_charge_plan()
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)

    async def async_turn_off(self, **kwargs) -> None:
        self._coordinator.deadline_override = False
        self._coordinator._last_plan_update = None
        self._coordinator._update_charge_plan()
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)
