"""Button platform for OCPP EV Charger."""
from __future__ import annotations

from typing import cast

from homeassistant.components.button import ButtonEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from . import OCPPCoordinator
from .const import CONF_CHARGER_ID, DOMAIN


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up button entities."""
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities(
        [
            StartChargingButton(coordinator, entry),
            StopChargingButton(coordinator, entry),
        ]
    )


def _device_info(entry: ConfigEntry) -> DeviceInfo:
    return DeviceInfo(
        identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
        name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
    )


class StartChargingButton(ButtonEntity):
    """Button to manually start charging."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_start_charging"
        self._attr_name = "Start Charging"
        self._attr_icon = "mdi:play-circle"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    async def async_press(self) -> None:
        await self._coordinator.async_start_charging()


class StopChargingButton(ButtonEntity):
    """Button to manually stop charging."""

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_stop_charging"
        self._attr_name = "Stop Charging"
        self._attr_icon = "mdi:stop-circle"
        self._attr_device_info = _device_info(entry)
        self._attr_has_entity_name = True

    async def async_press(self) -> None:
        await self._coordinator.async_stop_charging()
