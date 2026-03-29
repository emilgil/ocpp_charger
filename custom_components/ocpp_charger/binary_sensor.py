"""Binary sensor platform for OCPP EV Charger."""
from __future__ import annotations

from typing import cast

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OCPPCoordinator
from .const import CONF_CHARGER_ID, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensors."""
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            CableConnectedBinarySensor(coordinator, entry),
            ChargingActiveBinarySensor(coordinator, entry),
            ChargerOnlineBinarySensor(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    from .const import CONF_HOST, CONF_PORT
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
        name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
    )


class OCPPBinarySensorBase(CoordinatorEntity, BinarySensorEntity):
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


class CableConnectedBinarySensor(OCPPBinarySensorBase):
    """True when a vehicle cable is plugged in."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "cable_connected", "Cable Connected")
        self._attr_device_class = BinarySensorDeviceClass.PLUG
        self._attr_icon = "mdi:ev-plug-type2"

    @property
    def is_on(self) -> bool:
        return self._coord.ocpp.state.cable_connected


class ChargingActiveBinarySensor(OCPPBinarySensorBase):
    """True when actively charging."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charging_active", "Charging")
        self._attr_device_class = BinarySensorDeviceClass.BATTERY_CHARGING
        self._attr_icon = "mdi:battery-charging"

    @property
    def is_on(self) -> bool:
        return self._coord.ocpp.state.charging


class ChargerOnlineBinarySensor(OCPPBinarySensorBase):
    """True when the charger is connected via OCPP WebSocket."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry, "charger_online", "Charger Connected")
        self._attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
        self._attr_icon = "mdi:lan-connect"

    @property
    def is_on(self) -> bool:
        return self._coord.ocpp.state.connected
