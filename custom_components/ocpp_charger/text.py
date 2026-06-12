"""Text platform for OCPP EV Charger – manual deadline input (Feature 4)."""
from __future__ import annotations

from homeassistant.components.text import TextEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import OCPPCoordinator
from .const import CONF_CHARGER_ID, DOMAIN
from .deadline import parse_hhmm


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    async_add_entities([ManualDeadlineText(coordinator, entry)])


class ManualDeadlineText(CoordinatorEntity, TextEntity):
    """Free-text charge deadline (HH:MM).

    Empty = automatic deadline (weekday 06:00 / weekend none).
    Non-empty = use the given time; rolls to tomorrow if already past.
    Cleared automatically when the cable is disconnected (Available status).
    Persisted via the coordinator Store so it survives HA restarts.
    """

    _attr_pattern = r"^(\d{1,2}:\d{2})?$"
    _attr_mode = "text"
    _attr_native_min = 0
    _attr_native_max = 5

    def __init__(self, coordinator: OCPPCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator)
        self._coordinator = coordinator
        self._attr_unique_id = f"{entry.entry_id}_manual_deadline"
        self._attr_name = "Manual Deadline"
        self._attr_icon = "mdi:clock-end"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.data[CONF_CHARGER_ID])},
            name=f"EV Charger {entry.data[CONF_CHARGER_ID]}",
        )
        self._attr_has_entity_name = True

    @property
    def native_value(self) -> str:
        return self._coordinator.manual_deadline_str

    async def async_set_value(self, value: str) -> None:
        value = value.strip()
        if value and parse_hhmm(value) is None:
            return  # ignore invalid input (non-empty and not a valid HH:MM)
        self._coordinator.manual_deadline_str = value
        await self._coordinator._save_state()       # Feature 4: persist immediately
        self._coordinator._last_plan_update = None  # bypass throttle so it applies now
        self._coordinator._update_charge_plan()
        self._coordinator.async_set_updated_data(self._coordinator.ocpp.state)
