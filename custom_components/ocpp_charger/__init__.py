"""OCPP EV Charger integration for Home Assistant."""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from homeassistant.components import mqtt
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.helpers.event import async_call_later
from homeassistant.helpers.storage import Store

from .const import (
    CONF_CHARGER_ID,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_HOST,
    CONF_MAX_CURRENT,
    CONF_MQTT_TOPIC_PREFIX,
    CONF_NUM_PHASES,
    CONF_PORT,
    CONF_BATTERY_CAPACITY,
    CONF_SOC_ENTITY,
    CONF_VEHICLES,
    VEHICLE_CAPACITY,
    VEHICLE_NAME,
    VEHICLE_MAX_CURRENT_A,
    AUTO_DETECT_SOC_TOLERANCE,
    CONF_AUTO_VEHICLE_DETECTION,
    CONF_SCHEDULE_DAY_CURRENT,
    CONF_SCHEDULE_DAY_START,
    CONF_SCHEDULE_NIGHT_CURRENT,
    CONF_SCHEDULE_NIGHT_START,
    CONF_REST_AUTH_TYPE,
    CONF_REST_BASE_URL,
    CONF_REST_PASSWORD,
    CONF_REST_TOKEN,
    CONF_REST_USERNAME,
    REST_AUTH_NONE,
    SERVICE_REST_CALL,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    CONF_NOTIFY_ON_CONNECT,
    CONF_NOTIFY_ON_START,
    CONF_NOTIFY_ON_STOP,
    DEFAULT_CHARGE_DEADLINE_HOUR,
    SENSOR_PLAN_START,
    SENSOR_PLAN_END,
    DEFAULT_MQTT_PREFIX,
    DEFAULT_SCHEDULE_DAY_CURRENT,
    DEFAULT_SCHEDULE_DAY_START,
    DEFAULT_SCHEDULE_NIGHT_CURRENT,
    DEFAULT_SCHEDULE_NIGHT_START,
    VEHICLE_SOC_ENTITY,
    CHARGE_MODE_SMART,
    SWITCH_ALLOW_DAY_CHARGING,
    NOTIFY_ACTION_USE_DAY,
    NOTIFY_ACTION_USE_NIGHT,
    NOTIFY_ACTION_DISMISS,
    NOTIFY_ACTION_SELECT_VEHICLE,
    PLANNER_ALGO_GREEDY,
    PLANNER_ALGO_CONTIGUOUS,
    CONF_SOC_UNIT,
    VEHICLE_SOC_UNIT,
    SOC_UNIT_KWH,
    SOC_UNIT_PERCENT,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_CHARGE_EFFICIENCY,
    DEFAULT_VOLTAGE,
    DOMAIN,
    MQTT_COMMAND_TOPIC,
    MQTT_METER_TOPIC,
    MQTT_RESPONSE_TOPIC,
    MQTT_SOC_TOPIC,
    MQTT_STATUS_TOPIC,
    SCAN_INTERVAL_SECONDS,
    SMART_CHARGE_PRICE_THRESHOLD_PERCENTILE,
)
from .ocpp_client import ChargerState, OCPPClient
from .smart_charge import SmartChargeController
from .current_schedule import CurrentSchedule
from .rest_client import ChargerRestClient
from .charge_planner import ChargePlan, plan_cheapest_window, _to_utc
from .notifier import ChargerNotifier
from .vehicle_detection import identify_vehicle

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.NUMBER,
    Platform.BUTTON,
    Platform.SELECT,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up OCPP EV Charger from a config entry."""
    from logging.handlers import RotatingFileHandler

    _ocpp_file_handler = RotatingFileHandler(
        "/config/ocpp_charger_debug.log",
        maxBytes=5 * 1024 * 1024,  # 5 MB per fil
        backupCount=3,              # 3 rotationer = max 20 MB totalt
    )
    _ocpp_file_handler.setLevel(logging.DEBUG)
    _ocpp_file_handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s – %(message)s")
    )
    ocpp_logger = logging.getLogger("custom_components.ocpp_charger")
    if not any(isinstance(h, RotatingFileHandler) for h in ocpp_logger.handlers):
        ocpp_logger.addHandler(_ocpp_file_handler)

    hass.data.setdefault(DOMAIN, {})

    coordinator = OCPPCoordinator(hass, entry)
    try:
        await coordinator.async_start()
    except OSError as err:
        raise ConfigEntryNotReady(
            f"Could not start OCPP server on port {entry.data.get(CONF_PORT, 9000)}: {err}"
        ) from err

    hass.data[DOMAIN][entry.entry_id] = coordinator

    # Listen for user response to day/night actionable notification
    async def _handle_notification_action(event) -> None:
        action = event.data.get("action", "")
        if action == NOTIFY_ACTION_USE_DAY:
            _LOGGER.info("[Notify] User chose DAY charging")
            coordinator.set_allow_day_charging(True)
            coordinator._force_day_plan = True
            coordinator._update_charge_plan()
            coordinator.async_set_updated_data(coordinator.ocpp.state)
            coordinator.notifier.dismiss_day_night_notification()
        elif action == NOTIFY_ACTION_USE_NIGHT:
            _LOGGER.info("[Notify] User chose NIGHT charging")
            coordinator.set_allow_day_charging(False)
            coordinator._force_day_plan = False
            coordinator._update_charge_plan()
            coordinator.async_set_updated_data(coordinator.ocpp.state)
            coordinator.notifier.dismiss_day_night_notification()
        elif action == NOTIFY_ACTION_DISMISS:
            _LOGGER.info("[Notify] User dismissed day/night choice")
            coordinator._day_charging_dismissed = True
            coordinator.set_allow_day_charging(False)
            coordinator._update_charge_plan()
            coordinator.async_set_updated_data(coordinator.ocpp.state)
            coordinator.notifier.dismiss_day_night_notification()
        elif action.startswith(NOTIFY_ACTION_SELECT_VEHICLE):
            idx_str = action[len(NOTIFY_ACTION_SELECT_VEHICLE):]
            try:
                idx = int(idx_str)
                vehicles = coordinator._vehicles
                if 0 <= idx < len(vehicles):
                    vehicle = vehicles[idx]
                    _LOGGER.info("[Notify] User selected vehicle: %s", vehicle.get(VEHICLE_NAME, idx))
                    coordinator.set_active_vehicle(vehicle)
                    coordinator._update_charge_plan()
                    coordinator.async_set_updated_data(coordinator.ocpp.state)
            except ValueError:
                _LOGGER.warning("[Notify] Invalid vehicle index in action: %s", action)

    entry.async_on_unload(
        hass.bus.async_listen("mobile_app_notification_action", _handle_notification_action)
    )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    # Register REST call service
    async def _handle_rest_call(call) -> None:
        coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
        result = await coordinator.async_rest_call(
            method   = call.data.get("method", "GET"),
            endpoint = call.data.get("endpoint", ""),
            params   = call.data.get("params"),
            body     = call.data.get("body"),
        )
        # Fire event so Lovelace card can pick it up
        hass.bus.async_fire(
            f"{DOMAIN}_rest_response",
            {**result, "entry_id": entry.entry_id},
        )

    hass.services.async_register(DOMAIN, SERVICE_REST_CALL, _handle_rest_call)

    async def _handle_change_configuration(call) -> None:
        coord: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
        key   = call.data.get("key", "")
        value = call.data.get("value", "")
        result = await coord.ocpp.change_configuration(key, str(value))
        hass.bus.async_fire(f"{DOMAIN}_ocpp_response", {**result, "action": "ChangeConfiguration", "entry_id": entry.entry_id})

    async def _handle_get_configuration(call) -> None:
        coord: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
        key = call.data.get("key") or None
        result = await coord.ocpp.get_configuration(key)
        hass.bus.async_fire(f"{DOMAIN}_ocpp_response", {**result, "action": "GetConfiguration", "entry_id": entry.entry_id})

    hass.services.async_register(DOMAIN, "change_configuration", _handle_change_configuration)
    hass.services.async_register(DOMAIN, "get_configuration", _handle_get_configuration)

    # Re-subscribe to MQTT if config entry data changes (e.g. topic prefix)
    entry.async_on_unload(
        entry.add_update_listener(_async_update_listener)
    )
    return True


async def _async_update_listener(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Handle options/data update – reload MQTT subscriptions if prefix changed."""
    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    new_prefix = entry.data.get(CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_PREFIX)
    if new_prefix != coordinator.mqtt_prefix:
        _LOGGER.info(
            "[MQTT] Prefix changed %s → %s, re-subscribing",
            coordinator.mqtt_prefix, new_prefix,
        )
        # Unsubscribe old topics
        for unsub in coordinator._mqtt_unsubscribers:
            unsub()
        coordinator._mqtt_unsubscribers.clear()
        # Apply new prefix and re-subscribe
        coordinator.mqtt_prefix = new_prefix
        await coordinator._setup_mqtt()

    # Update notifier config
    coordinator.notifier.notify_target = entry.data.get(CONF_NOTIFY_TARGET, "")
    coordinator.notifier.enabled = entry.data.get(CONF_NOTIFY_ENABLED, False)
    coordinator._notify_on_connect = entry.data.get(CONF_NOTIFY_ON_CONNECT, True)
    coordinator._notify_on_start   = entry.data.get(CONF_NOTIFY_ON_START, True)
    coordinator._notify_on_stop    = entry.data.get(CONF_NOTIFY_ON_STOP, True)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    from logging.handlers import RotatingFileHandler

    coordinator: OCPPCoordinator = hass.data[DOMAIN][entry.entry_id]
    await coordinator.async_stop()

    ocpp_logger = logging.getLogger("custom_components.ocpp_charger")
    for h in list(ocpp_logger.handlers):
        if isinstance(h, RotatingFileHandler):
            ocpp_logger.removeHandler(h)
            h.close()

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)
        # Unregister services if no more entries remain
        if not hass.data[DOMAIN]:
            for svc in (SERVICE_REST_CALL, "change_configuration", "get_configuration"):
                hass.services.async_remove(DOMAIN, svc)
    return unload_ok


class OCPPCoordinator(DataUpdateCoordinator):
    """Central coordinator managing OCPP client, MQTT, and smart charging."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=60),  # idle default; adjusted dynamically
        )
        self.entry = entry
        self.config = entry.data

        self.charger_id: str = self.config[CONF_CHARGER_ID]
        self.host: str = self.config[CONF_HOST]
        self.port: int = self.config[CONF_PORT]
        self.mqtt_prefix: str = self.config.get(CONF_MQTT_TOPIC_PREFIX, "ocpp")
        self.max_current: float = float(self.config.get(CONF_MAX_CURRENT, 16))
        self.num_phases: int = int(self.config.get(CONF_NUM_PHASES, 3))
        self.price_entity: str = self.config.get(CONF_ELECTRICITY_PRICE_ENTITY, "")

        # Vehicle registry – pick first vehicle as default active vehicle
        vehicles = self.config.get(CONF_VEHICLES, [])
        self._vehicles: list[dict] = vehicles
        self.active_vehicle: dict | None = vehicles[0] if vehicles else None

        self.auto_vehicle_detection: bool = True   # can be toggled via switch
        self._last_connector_status: str = ""
        self._last_connector_status_notify: str = ""  # separate tracker for notifications
        self._last_detection_reason: str = ""
        self.adhoc_vehicle_active: bool = False

        # Day/night current schedule
        import zoneinfo as _zi
        try:
            _local_tz = _zi.ZoneInfo(hass.config.time_zone)
        except Exception:
            _local_tz = None
        self.schedule = CurrentSchedule(
            day_start=self.config.get(CONF_SCHEDULE_DAY_START, DEFAULT_SCHEDULE_DAY_START),
            night_start=self.config.get(CONF_SCHEDULE_NIGHT_START, DEFAULT_SCHEDULE_NIGHT_START),
            day_current_a=float(self.config.get(CONF_SCHEDULE_DAY_CURRENT, DEFAULT_SCHEDULE_DAY_CURRENT)),
            night_current_a=float(self.config.get(CONF_SCHEDULE_NIGHT_CURRENT, DEFAULT_SCHEDULE_NIGHT_CURRENT)),
            local_tz=_local_tz,
        )

        # These two properties follow the active_vehicle and can be
        # overridden via the BatteryCapacityNumber entity for legacy setups
        self.soc_entity: str = (
            self.active_vehicle.get(VEHICLE_SOC_ENTITY, "") if self.active_vehicle else
            self.config.get(CONF_SOC_ENTITY, "")
        )
        self.soc_unit: str = (
            self.active_vehicle.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT) if self.active_vehicle else
            SOC_UNIT_PERCENT
        )
        self.battery_capacity_kwh: float = float(
            self.active_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH)
            if self.active_vehicle else
            self.config.get(CONF_BATTERY_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH)
        )
        # SOC estimation (used when OCPP/HA entity does not report SOC)
        self._session_start_soc: float | None = None   # SOC captured at session start
        self._soc_source: str = "none"  # "ocpp", "entity", "estimated"

        # Runtime state
        self.charge_mode: str = CHARGE_MODE_SMART
        self.planner_algorithm: str = PLANNER_ALGO_GREEDY
        self.target_soc: float = 80.0
        self.target_kwh: float = 0.0
        self.current_price: float | None = None
        self.current_limit_a: float = self.max_current
        self.session_start: datetime | None = None
        self.estimated_completion: datetime | None = None
        self.estimated_remaining_minutes: int | None = None
        self.charge_plan: ChargePlan | None = None
        self._alt_plan: ChargePlan | None = None
        self._last_plan_update: datetime | None = None
        self._day_charging_manual_override: bool = False  # True = user toggled manually
        self._force_day_plan: bool = False   # True after user picks day via notification
        self.allow_day_charging: bool = self._compute_allow_day_charging()

        # Notifications
        self.notifier = ChargerNotifier(
            hass=hass,
            notify_target=self.config.get(CONF_NOTIFY_TARGET, ""),
            enabled=self.config.get(CONF_NOTIFY_ENABLED, False),
        )
        self._notify_on_connect: bool = self.config.get(CONF_NOTIFY_ON_CONNECT, True)
        self._notify_on_start:   bool = self.config.get(CONF_NOTIFY_ON_START, True)
        self._notify_on_stop:    bool = self.config.get(CONF_NOTIFY_ON_STOP, True)
        self._was_charging: bool = False
        self._preparing_timestamp: datetime | None = None  # for Finishing-after-Preparing guard
        self._last_connect_notify_time: datetime | None = None  # debounce duplicate Preparing
        self._last_transaction_start: datetime | None = None  # for grace period after start
        self._last_remote_start: datetime | None = None       # for plan freeze after RemoteStart
        self._last_remote_stop: datetime | None = None        # Fix 8: debounce double RemoteStop
        self._session_total_kwh: float = 0.0                  # Fix 7: accumulated energy since cable-in
        self._cable_connect_time: datetime | None = None       # Fix 4: when cable was plugged in
        self._soc_at_connect: float | None = None              # Fix 4: SOC at cable connect
        self._soc_reread_done: bool = False                    # Fix 4: True when reread period over
        self._manual_start_requested: bool = False             # set when user manually starts charging
        self._manual_stop_requested: bool = False              # set when user manually stops charging
        self._last_cost_energy_kwh: float = 0.0  # energy at last cost update
        self._notified_connect_session: str | None = None  # avoid duplicate connect notifs
        self._cable_session_notified_connect: bool = False   # Fix 9: one connect-notif per cable session
        self._notified_start_session: str | None = None   # avoid duplicate start notifs
        self._notified_stop_session: str | None = None    # avoid duplicate stop notifs
        self._start_notified_this_connection: bool = False  # Bug 2: prevent notification storms
        self._day_charging_dismissed: bool = False  # Bug 3: user dismissed day/night choice
        self._suspended_ev_since: datetime | None = None  # Bug 5: SuspendedEV tracking
        # Cable session tracking (Bug 6): spans cable-in → cable-out
        self._cable_session_energy_kwh: float = 0.0
        self._cable_session_cost_sek: float = 0.0
        self._cable_session_start_notified: bool = False
        self._cable_session_stop_notified: bool = False
        self._cable_session_start_time: datetime | None = None
        self._disconnect_since: datetime | None = None   # when WS disconnect started
        self._notified_disconnect: bool = False            # avoid repeat disconnect notifs
        self._tomorrow_prices_seeded: bool = False         # re-seed when tomorrow prices arrive
        self._store = Store(hass, 1, f"ocpp_charger_{entry.entry_id}")
        self.smart_controller = SmartChargeController(
            threshold_percentile=SMART_CHARGE_PRICE_THRESHOLD_PERCENTILE,
            local_tz=_local_tz,
        )

        # OCPP client (acts as Central System)
        self.ocpp = OCPPClient(
            host="0.0.0.0",  # Listen on all interfaces
            port=self.port,
            charger_id=self.charger_id,
            state_callback=self._on_charger_state_update,
            hass=hass,
        )
        self.ocpp.state.max_current_a = self.max_current

        # MQTT subscriptions
        self._mqtt_unsubscribers: list[Any] = []

    # ------------------------------------------------------------------ #
    #  Lifecycle                                                            #
    # ------------------------------------------------------------------ #

    async def async_start(self) -> None:
        """Start OCPP server and MQTT subscriptions."""
        await self.ocpp.start()
        await self._setup_mqtt()
        _LOGGER.info("OCPPCoordinator started for charger %s on port %d", self.charger_id, self.port)

        async def _delayed_soc_refresh(_now=None) -> None:
            """Read SOC from HA entity once HA has finished loading all entities."""
            _LOGGER.debug("[SOC] Delayed refresh fired – entity=%r unit=%r vehicle=%s",
                self.soc_entity, self.soc_unit,
                self.active_vehicle.get("name") if self.active_vehicle else "None")
            ha_state = self.hass.states.get(self.soc_entity) if self.soc_entity else None
            _LOGGER.debug("[SOC] Raw HA state: %s", ha_state.state if ha_state else "not found")
            self._update_soc_from_ha()
            _LOGGER.debug("[SOC] After refresh: soc_percent=%s source=%s", self.ocpp.state.soc_percent, self._soc_source)
            self._seed_price_history()
            # Restore persisted state before asking charger for StatusNotification
            await self._load_state()
            # Ask charger to resend StatusNotification so cable_connected is correct after HA restart
            if self.ocpp.state.connected:
                _LOGGER.info("[SOC] Skickar TriggerMessage vid startup")
                await self.ocpp.trigger_status_notification()
            if self.ocpp.state.soc_percent is not None:
                self.async_set_updated_data(self.ocpp.state)

        async_call_later(self.hass, 10, _delayed_soc_refresh)

    async def async_stop(self) -> None:
        """Stop everything cleanly."""
        for unsub in self._mqtt_unsubscribers:
            unsub()
        self._mqtt_unsubscribers.clear()
        await self.ocpp.stop()

    # ------------------------------------------------------------------ #
    #  Data update                                                          #
    # ------------------------------------------------------------------ #

    async def _async_update_data(self) -> ChargerState:
        """Fetch latest state – called by HA every SCAN_INTERVAL_SECONDS."""
        _LOGGER.debug("Update cycle – status=%s charging=%s power=%sW soc=%s price=%s",
            self.ocpp.state.connector_status,
            self.ocpp.state.charging,
            f"{self.ocpp.state.power_w:.0f}" if self.ocpp.state.power_w is not None else "N/A",
            self.ocpp.state.soc_percent,
            self.current_price,
        )
        self._update_price_from_ha()
        self._apply_current_schedule()
        self._check_vehicle_auto_detect()
        self._update_soc_from_ha()
        self._check_soc_reread()  # Fix 4: periodic SOC re-read after cable connect
        if not self.ocpp.state.charging:
            self._update_charge_plan()
        self._update_smart_charging()
        self._update_cost()
        self._update_eta()
        self._adjust_update_interval()
        self._sync_allow_day_charging()

        # Disconnect notification (>5 min)
        self._check_disconnect_notify()

        # Publish state to MQTT
        await self._publish_mqtt_status()
        await self._save_state()

        return self.ocpp.state

    async def _save_state(self) -> None:
        """Persist session state to HA storage for recovery after restart."""
        state = self.ocpp.state if self.ocpp else None
        data = {
            "cable_connected": state.cable_connected if state else False,
            "transaction_id": state.transaction_id if state else None,
            "accumulated_cost": state.accumulated_cost if state else 0.0,
            "energy_kwh": state.energy_kwh if state else 0.0,
            "session_energy_start": state.session_energy_start if state else None,
            "session_id": state.session_id if state else None,
            "accumulated_charging_seconds": state.accumulated_charging_seconds if state else 0,
            "total_cost": state.total_cost if state else 0.0,
            "cable_session_energy_kwh": self._cable_session_energy_kwh,
            "cable_session_cost_sek": self._cable_session_cost_sek,
            "charge_mode": self.charge_mode,
            "target_soc": self.target_soc,
            "target_kwh": self.target_kwh,
            "active_vehicle_name": self.active_vehicle.get(VEHICLE_NAME) if self.active_vehicle else None,
        }
        if state and state.session_start:
            data["session_start"] = state.session_start.isoformat()
        await self._store.async_save(data)
        _LOGGER.debug("[Store] Sparade state: cable_connected=%s tx=%s cost=%.2f",
                       data["cable_connected"], data["transaction_id"],
                       data["accumulated_cost"])

    async def _load_state(self) -> None:
        """Restore session state from HA storage after restart."""
        data = await self._store.async_load()
        if data and self.ocpp:
            self.ocpp.state.cable_connected = data.get("cable_connected", False)
            self.ocpp.state.transaction_id = data.get("transaction_id")
            self.ocpp.state.accumulated_cost = data.get("accumulated_cost", 0.0)
            self.ocpp.state.energy_kwh = data.get("energy_kwh", 0.0)
            self.ocpp.state.session_energy_start = data.get("session_energy_start")
            self.ocpp.state.session_id = data.get("session_id")
            self.ocpp.state.accumulated_charging_seconds = data.get("accumulated_charging_seconds", 0)
            self.ocpp.state.total_cost = data.get("total_cost", 0.0)
            self._cable_session_energy_kwh = data.get("cable_session_energy_kwh", 0.0)
            self._cable_session_cost_sek = data.get("cable_session_cost_sek", 0.0)
            self._last_cost_energy_kwh = data.get("energy_kwh", 0.0)
            if data.get("session_start"):
                try:
                    self.ocpp.state.session_start = datetime.fromisoformat(data["session_start"])
                except (ValueError, TypeError):
                    pass
            if data.get("charge_mode"):
                self.charge_mode = data["charge_mode"]
            if data.get("target_soc") is not None:
                self.target_soc = float(data["target_soc"])
            if data.get("target_kwh") is not None:
                self.target_kwh = float(data["target_kwh"])
            saved_vehicle = data.get("active_vehicle_name")
            if saved_vehicle:
                match = next((v for v in self._vehicles if v.get(VEHICLE_NAME) == saved_vehicle), None)
                if match:
                    self.set_active_vehicle(match)
                    _LOGGER.info("[Store] Återställde aktivt fordon: %s", saved_vehicle)
                else:
                    _LOGGER.warning("[Store] Sparat fordon '%s' finns inte längre i konfigurationen", saved_vehicle)
            _LOGGER.info("[Store] Laddade state: cable=%s tx=%s cost=%.2f energy=%.3f mode=%s",
                         self.ocpp.state.cable_connected, self.ocpp.state.transaction_id,
                         self.ocpp.state.accumulated_cost, self.ocpp.state.energy_kwh,
                         self.charge_mode)

    def _apply_current_schedule(self) -> None:
        """Update max_current from schedule unless smart charging already handles it."""
        new_limit = self.schedule.current_limit()
        # Always keep ocpp_client default limit in sync so auto-started transactions
        # get the correct limit even before we send RemoteStart.
        self.ocpp._default_limit_a = new_limit
        if new_limit != self.max_current:
            self.max_current = new_limit
            # Clear _pending_limit_a so StartTransaction handler uses the new
            # _default_limit_a instead of a stale value from previous period.
            self.ocpp._pending_limit_a = None
            _LOGGER.info(
                "Charging schedule changed: period=%s limit=%.0f A override=%s",
                self.schedule.period_name(), new_limit, self.schedule.override_active,
            )
            # Send limit to charger hardware even when idle so Garo has the
            # correct GaroOwnerMaxCurrent before the next transaction starts.
            if self.ocpp.state.connected:
                self.hass.async_create_task(self.ocpp.set_charging_limit(new_limit))

    def _check_vehicle_auto_detect(self) -> None:
        """Trigger vehicle identification when connector status changes to Preparing."""
        if not self.auto_vehicle_detection:
            return
        if len(self._vehicles) < 2:
            return

        current_status = self.ocpp.state.connector_status
        if (
            current_status == "Preparing"
            and self._last_connector_status == "Available"  # only on fresh cable connection
        ):
            # Force SOC refresh from HA entity on cable connect.
            # Keep the previous SOC value so we can sanity-check the new one.
            _prev_soc = self.ocpp.state.soc_percent
            self._session_start_soc = None
            self._soc_source = "none"
            self._update_soc_from_ha()
            _new_soc = self.ocpp.state.soc_percent
            # Sanity check: if the new value deviates more than 20 pp from the
            # previously known value, it is likely a bogus OCPP reading – revert.
            if (
                _prev_soc is not None
                and _new_soc is not None
                and abs(_new_soc - _prev_soc) > 20
            ):
                _LOGGER.warning(
                    "[SOC] Implausible SOC on cable connect: %.1f%% → %.1f%% "
                    "(delta >20 pp) – keeping previous value %.1f%%",
                    _prev_soc, _new_soc, _prev_soc,
                )
                self.ocpp.state.soc_percent = _prev_soc
                self._soc_source = "entity" if self.soc_entity else "estimated"
            else:
                _LOGGER.debug(
                    "[SOC] Refreshed on cable connect: %.1f%% → %s",
                    _prev_soc if _prev_soc is not None else 0.0,
                    f"{_new_soc:.1f}%" if _new_soc is not None else "unknown",
                )
            ocpp_soc = self.ocpp.state.soc_percent
            vehicle, reason = identify_vehicle(
                self._vehicles, ocpp_soc, self.hass
            )
            if vehicle and vehicle is not self.active_vehicle:
                _LOGGER.info("[AutoDetect] %s", reason)
                self.set_active_vehicle(vehicle)
                # Persist detection reason as attribute on the select entity
                self._last_detection_reason = reason
            elif vehicle:
                _LOGGER.debug("Auto-detection: ingen ändring (%s)", reason)

        self._last_connector_status = current_status

    def _update_soc_from_ha(self) -> None:
        """Update SOC using a three-level priority chain.

        Priority order:
          1. OCPP SoC measurand (laddboxen rapporterar direkt) – högst prioritet.
          2. HA-entitet (t.ex. bilintegration) – läses vid sessionstart och används
             som start-SOC för estimering.
          3. Beräknad SOC = start_soc + laddad_energi * verkningsgrad / batterikapacitet
             – används när varken OCPP eller entitet levererar ett värde.
        """
        state = self.ocpp.state

        # 1. OCPP har SOC – spara som källa och avsluta
        if state.soc_percent is not None and self._soc_source == "ocpp":
            return
        # Om OCPP precis började rapportera, uppdatera källa
        if state.soc_percent is not None and self._soc_source != "ocpp":
            self._soc_source = "ocpp"
            return

        # 2. Läs HA-entitet
        entity_soc: float | None = None
        if self.soc_entity:
            ha_state = self.hass.states.get(self.soc_entity)
            if ha_state and ha_state.state not in ("unavailable", "unknown", ""):
                try:
                    val = float(ha_state.state)
                    if self.soc_unit == SOC_UNIT_KWH:
                        # Convert kWh to % using battery capacity
                        if self.battery_capacity_kwh > 0:
                            val = (val / self.battery_capacity_kwh) * 100.0
                            _LOGGER.debug(
                                "[SOC] kWh→%%: %.2f kWh / %.1f kWh = %.1f%%",
                                float(ha_state.state), self.battery_capacity_kwh, val,
                            )
                        else:
                            val = None
                    if val is not None and 0.0 <= val <= 100.0:
                        entity_soc = val
                except ValueError:
                    _LOGGER.warning(
                        "Could not parse SOC value from %s: %s",
                        self.soc_entity,
                        ha_state.state,
                    )

        # Fånga start-SOC när en ny session börjar
        if state.charging and self._session_start_soc is None:
            if entity_soc is not None:
                self._session_start_soc = entity_soc
                self._soc_source = "entity"
                _LOGGER.info(
                    "Session start SOC from HA entity: %.1f %%", entity_soc
                )
            else:
                self._session_start_soc = 0.0
                self._soc_source = "estimated"
                _LOGGER.info(
                    "No SOC source available – starting estimation from 0%%"
                )

        # Nollställ session-SOC när ingen kabel är inkopplad
        if not state.charging and not state.cable_connected:
            self._session_start_soc = None
            # Sätt SOC från entitet även i idle-läge (t.ex. vid omstart)
            if entity_soc is not None:
                state.soc_percent = entity_soc
                self._soc_source = "entity"
            else:
                self._soc_source = "none"
            return

        # 2b. Entitet tillgänglig under session → använd direkt (t.ex. live från bil-app)
        if entity_soc is not None and self._soc_source in ("entity", "none"):
            state.soc_percent = entity_soc
            self._soc_source = "entity"
            return

        # 3. Estimera SOC från energimätaren
        if self._session_start_soc is not None and self.battery_capacity_kwh > 0:
            added_kwh = state.energy_kwh * DEFAULT_CHARGE_EFFICIENCY
            estimated = self._session_start_soc + (
                added_kwh / self.battery_capacity_kwh * 100.0
            )
            state.soc_percent = min(100.0, round(estimated, 1))
            self._soc_source = "estimated"

    @staticmethod
    def _to_ore_per_kwh(raw_value: float, unit: str) -> float:
        """Convert a price value to öre/kWh.

        Checks the entity's unit_of_measurement first; falls back to a
        heuristic (values < 10 are assumed to be in SEK/kWh).
        """
        unit_lower = unit.lower()
        if "öre" in unit_lower or "ore" in unit_lower:
            return raw_value
        if any(s in unit_lower for s in ("sek", "kr/kwh", "eur", "€")):
            return raw_value * 100
        # Heuristic fallback
        return raw_value * 100 if raw_value < 10 else raw_value

    def _update_price_from_ha(self) -> None:
        """Read current electricity price from HA entity."""
        if not self.price_entity:
            return
        state = self.hass.states.get(self.price_entity)
        if state and state.state not in ("unavailable", "unknown", ""):
            try:
                raw = float(state.state)
                unit = state.attributes.get("unit_of_measurement", "")
                price = self._to_ore_per_kwh(raw, unit)
                self.current_price = price
                self.smart_controller.update_price(price)
            except ValueError:
                pass
            # Re-seed when tomorrow prices first appear (~13:00)
            tomorrow = state.attributes.get("tomorrow_interval_prices") or []
            if tomorrow and not self._tomorrow_prices_seeded:
                self._tomorrow_prices_seeded = True
                self._seed_price_history()
                _LOGGER.info("[SmartCharge] Tomorrow prices arrived, re-seeded price history")
            elif not tomorrow:
                self._tomorrow_prices_seeded = False

    def _seed_price_history(self) -> None:
        """Populate smart controller price history from today_interval_prices at startup.

        This ensures the threshold is available immediately instead of waiting
        for 4+ update cycles.
        """
        if not self.price_entity:
            return
        state_obj = self.hass.states.get(self.price_entity)
        if state_obj is None:
            return
        today_prices = state_obj.attributes.get("today_interval_prices", []) or []
        tomorrow_prices = state_obj.attributes.get("tomorrow_interval_prices", []) or []
        all_intervals = today_prices + tomorrow_prices
        if not all_intervals:
            return
        unit = state_obj.attributes.get("unit_of_measurement", "")
        count = 0
        for interval in all_intervals:
            try:
                val = self._to_ore_per_kwh(float(interval["value"]), unit)
                self.smart_controller.update_price(val)
                count += 1
            except (KeyError, TypeError, ValueError):
                pass
        _LOGGER.debug("[SmartCharge] Seeded price history with %d interval prices", count)

    def _update_smart_charging(self) -> None:
        """Apply smart charging logic."""
        # Debounce: avoid running from both callback and update cycle within 2s
        now_ts = datetime.now(timezone.utc)
        last = getattr(self, "_last_smart_charge_run", None)
        if last and (now_ts - last).total_seconds() < 2:
            return
        self._last_smart_charge_run = now_ts

        state = self.ocpp.state
        _LOGGER.debug(
            "[SmartCharge] check: cable=%s charging=%s mode=%s plan=%s now=%s",
            state.cable_connected,
            state.charging,
            self.charge_mode,
            f"{self.charge_plan.start.astimezone().strftime('%H:%M')}–{self.charge_plan.end.astimezone().strftime('%H:%M')}" if self.charge_plan else "None",
            datetime.now(timezone.utc).astimezone().strftime("%H:%M:%S"),
        )

        if not self.ocpp.state.connected:
            return

        # Fix E – charging=True implies cable must be connected
        if state.charging and not state.cable_connected:
            _LOGGER.warning("[SmartCharge] charging=True men cable_connected=False – korrigerar")
            state.cable_connected = True

        # ── Plan-based auto-start check (log before cable guard so it's always visible) ──
        _auto_start_in_window = False
        if not state.charging and self.charge_mode == CHARGE_MODE_SMART:
            plan = self.charge_plan
            if plan and plan.feasible and plan.active_intervals:
                now_utc = datetime.now(timezone.utc)
                _auto_start_in_window = plan.is_in_window(now_utc)
                _LOGGER.info(
                    "[SmartCharge] Auto-start check: now=%s windows=%d in_window=%s cable=%s charging=%s",
                    now_utc.astimezone().strftime("%H:%M"),
                    len(plan.active_intervals),
                    _auto_start_in_window,
                    state.cable_connected,
                    state.charging,
                )

        if not self.ocpp.state.cable_connected:
            return

        # ── Plan-based auto-start (act only when cable is connected) ─────
        if _auto_start_in_window:
            # Bug 5: Don't start a new transaction if one is already active
            if self.ocpp.state.transaction_id is not None:
                _LOGGER.debug(
                    "[SmartCharge] Transaktion redan aktiv (%s), hoppar över auto-start",
                    self.ocpp.state.transaction_id,
                )
                return
            # Guard: don't spam RemoteStart – only send if we haven't tried recently
            now_utc = datetime.now(timezone.utc)
            if self._last_remote_start and (now_utc - self._last_remote_start).total_seconds() < 300:
                _LOGGER.debug("[SmartCharge] RemoteStart already sent %.0fs ago, skipping",
                    (now_utc - self._last_remote_start).total_seconds())
                return
            plan = self.charge_plan
            _LOGGER.info("[SmartCharge] Plan window active (%s–%s), starting charge",
                plan.start.astimezone().strftime("%H:%M"), plan.end.astimezone().strftime("%H:%M"))
            self._last_remote_start = now_utc
            self._manual_start_requested = False   # auto-start takes over control
            self._manual_stop_requested = False    # next connect should notify normally
            # Set the correct current limit BEFORE starting so the charger
            # uses the night limit from the very first second.
            self.hass.async_create_task(
                self._auto_start_with_limit(self.max_current)
            )
            return

        if not self.ocpp.state.charging:
            return

        # ── Bug 5: SuspendedEV guard – car satisfied, end transaction ────
        now = datetime.now(timezone.utc)
        if state.connector_status == "SuspendedEV" and state.power_w < 100:
            if self._suspended_ev_since is None:
                self._suspended_ev_since = now
            elif (now - self._suspended_ev_since).total_seconds() >= 60:
                if state.charging:
                    _LOGGER.info("[SmartCharge] SuspendedEV i >60s – bilen nöjd, avslutar")
                    self._guarded_remote_stop(now)
                    self._send_stop_notification()
                return
        else:
            self._suspended_ev_since = None

        # ── Plan-based stop/start (primary logic) ────────────────────────
        # If a feasible charge plan exists, use it to gate charging:
        # charge only within the planned window.
        plan = self.charge_plan
        now_utc = datetime.now(timezone.utc)

        if self.charge_mode == CHARGE_MODE_SMART and plan and plan.feasible and plan.active_intervals:
            # ── Goal reached check (Bug 1) – stop regardless of window ──
            soc = state.soc_percent
            soc_reached = soc is not None and self.target_soc > 0 and soc >= self.target_soc
            kwh_reached = self.target_kwh > 0 and state.energy_kwh >= self.target_kwh
            plan_energy_reached = plan.energy_kwh > 0 and state.energy_kwh >= plan.energy_kwh

            if soc_reached or kwh_reached or plan_energy_reached:
                if state.charging:
                    if soc_reached:
                        reason = f"SOC {soc:.0f}% >= mål {self.target_soc:.0f}%"
                    elif kwh_reached:
                        reason = f"Energi {state.energy_kwh:.2f} kWh >= mål {self.target_kwh:.2f} kWh"
                    else:
                        reason = f"Energi {state.energy_kwh:.2f} kWh >= planens {plan.energy_kwh:.2f} kWh"
                    _LOGGER.info("[SmartCharge] Mål nått (%s), stoppar", reason)
                    self._guarded_remote_stop(now_utc)
                return

            in_window = plan.is_in_window(now_utc)
            if not in_window and self.ocpp.state.charging:
                if self._manual_start_requested:
                    _LOGGER.info("[SmartCharge] Manual override aktiv, stoppar inte")
                    return
                if self._last_transaction_start is not None:
                    elapsed = (now_utc - self._last_transaction_start).total_seconds()
                    if elapsed < 90:
                        _LOGGER.debug("[SmartCharge] Grace period active (%.0fs < 90s), not stopping", elapsed)
                        return
                _LOGGER.info("[SmartCharge] Outside plan window (%d intervals), stopping",
                    len(plan.active_intervals))
                self._guarded_remote_stop(now_utc)
            return

        # ── Fallback: price-threshold logic (no feasible plan) ────────────
        should, reason = self.smart_controller.should_charge(
            mode=self.charge_mode,
            current_price=self.current_price,
            target_soc=self.target_soc if self.target_soc > 0 else None,
            current_soc=self.ocpp.state.soc_percent,
            target_kwh=self.target_kwh if self.target_kwh > 0 else None,
            session_kwh=self.ocpp.state.energy_kwh,
        )

        _LOGGER.debug("[SmartCharge] Decision: should=%s reason=%s", should, reason)
        if not should and self.ocpp.state.charging:
            if self._manual_start_requested:
                _LOGGER.info("[SmartCharge] Manual override aktiv, stoppar inte")
                return
            if self._last_transaction_start is not None:
                elapsed = (now_utc - self._last_transaction_start).total_seconds()
                if elapsed < 90:
                    _LOGGER.debug("[SmartCharge] Grace period active (%.0fs < 90s), not stopping", elapsed)
                    return
            _LOGGER.info("[SmartCharge] Stopping – %s", reason)
            self._guarded_remote_stop(now_utc)

    def _check_soc_reread(self) -> None:
        """Fix 4: Re-read SOC entity periodically for 30 min after cable connect.

        The car app may update SOC with a delay after driving. If SOC changes
        by >=5 pp, update the plan with the corrected starting point.
        """
        if self._soc_reread_done:
            return
        if self._cable_connect_time is None:
            return
        if self.ocpp.state.charging:
            return  # don't interfere during active charging

        elapsed = (datetime.now(timezone.utc) - self._cable_connect_time).total_seconds()

        if elapsed > 1800:  # 30 minutes – give up
            _LOGGER.debug("[SOC] Omläsningsperiod avslutad (30 min)")
            self._soc_reread_done = True
            return

        # Read entity directly
        if not self.soc_entity:
            self._soc_reread_done = True
            return

        ha_state = self.hass.states.get(self.soc_entity)
        if not ha_state or ha_state.state in ("unavailable", "unknown", ""):
            return

        try:
            val = float(ha_state.state)
            if self.soc_unit == SOC_UNIT_KWH and self.battery_capacity_kwh > 0:
                val = (val / self.battery_capacity_kwh) * 100.0
            if not (0.0 <= val <= 100.0):
                return
        except ValueError:
            return

        prev_soc = self._soc_at_connect
        if prev_soc is None:
            self._soc_at_connect = val
            return

        delta = abs(val - prev_soc)
        if delta >= 5.0:
            _LOGGER.info(
                "[SOC] Fördröjd SOC-uppdatering detekterad: %.1f%% → %.1f%% (Δ%.1f pp) – uppdaterar plan",
                prev_soc, val, delta,
            )
            self.ocpp.state.soc_percent = val
            self._soc_at_connect = val
            self._soc_source = "entity"
            self._last_plan_update = None  # force plan recalculation
            self._update_charge_plan()

    def _guarded_remote_stop(self, now: datetime) -> None:
        """Fix 8: debounce RemoteStop – ignore if <15s since last stop."""
        if self._last_remote_stop and (now - self._last_remote_stop).total_seconds() < 15:
            _LOGGER.debug("[SmartCharge] Dubbel-stop guardad (%.1fs sedan senaste)",
                          (now - self._last_remote_stop).total_seconds())
            return
        self._last_remote_stop = now
        self.hass.async_create_task(self.ocpp.remote_stop_transaction())

    def _update_cost(self) -> None:
        """Update accumulated session cost incrementally.

        Each update adds the cost of energy consumed since the last update,
        using the price that was valid at that moment.
        """
        current_energy = self.ocpp.state.energy_kwh
        if self.current_price is None or current_energy <= 0:
            return

        # Reset cost tracker when a new session starts
        if current_energy < self._last_cost_energy_kwh:
            self._last_cost_energy_kwh = 0.0

        delta_kwh = current_energy - self._last_cost_energy_kwh
        if delta_kwh > 0:
            self.ocpp.state.accumulated_cost += delta_kwh * (self.current_price / 100.0)
            self._last_cost_energy_kwh = current_energy

        _LOGGER.debug("[Cost] energy=%.3f kWh (+%.3f) price=%s öre/kWh cost=%.2f SEK",
            current_energy, delta_kwh,
            f"{self.current_price:.2f}" if self.current_price is not None else "N/A",
            self.ocpp.state.accumulated_cost)

    def _update_eta(self) -> None:
        """Recalculate estimated completion time.

        Uses power_w < 100 as the primary idle check instead of the
        charging flag, which can hang after reconnect/Unknown status.
        """
        if self.ocpp.state.power_w < 100:
            # Not actively charging – use plan end if available
            if self.charge_plan and self.charge_plan.feasible:
                self.estimated_completion = self.charge_plan.end
                self.estimated_remaining_minutes = self.charge_plan.duration_minutes
                return
            self.estimated_completion = None
            self.estimated_remaining_minutes = None
            return

        # Actively charging with measurable power – estimate from current power_w
        self.estimated_completion = self.smart_controller.estimate_completion_time(
            session_kwh=self.ocpp.state.energy_kwh,
            target_kwh=self.target_kwh if self.target_kwh > 0 else None,
            target_soc=self.target_soc if self.target_soc > 0 else None,
            current_soc=self.ocpp.state.soc_percent,
            power_w=self.ocpp.state.power_w,
        )
        if self.estimated_completion:
            remaining = self.estimated_completion - datetime.now(timezone.utc)
            self.estimated_remaining_minutes = max(0, int(remaining.total_seconds() // 60))
        else:
            self.estimated_remaining_minutes = None

    # ------------------------------------------------------------------ #
    #  MQTT                                                                 #
    # ------------------------------------------------------------------ #

    def _topic(self, subtopic: str) -> str:
        return f"{self.mqtt_prefix}/{self.charger_id}/{subtopic}"

    async def _setup_mqtt(self) -> None:
        """Subscribe to relevant MQTT topics from the charger."""
        topics = {
            MQTT_STATUS_TOPIC: self._on_mqtt_status,
            MQTT_METER_TOPIC: self._on_mqtt_meter,
            MQTT_SOC_TOPIC: self._on_mqtt_soc,
            MQTT_RESPONSE_TOPIC: self._on_mqtt_response,
        }
        for subtopic, handler in topics.items():
            full_topic = self._topic(subtopic)
            try:
                unsubscribe = await mqtt.async_subscribe(
                    self.hass, full_topic, handler
                )
                self._mqtt_unsubscribers.append(unsubscribe)
                _LOGGER.debug("Subscribed to MQTT topic: %s", full_topic)
            except Exception as err:
                _LOGGER.warning("Could not subscribe to %s: %s", full_topic, err)

    @callback
    def _on_mqtt_status(self, msg) -> None:
        """Handle MQTT status message from charger."""
        import json
        try:
            payload = json.loads(msg.payload)
            if "status" in payload:
                self.ocpp.state.connector_status = payload["status"]
                self.ocpp.state.cable_connected = payload["status"] in {
                    "Preparing", "Charging", "SuspendedEV", "SuspendedEVSE", "Finishing"
                }
            self.async_set_updated_data(self.ocpp.state)
        except Exception:
            pass

    @callback
    def _on_mqtt_meter(self, msg) -> None:
        """Handle MQTT meter values."""
        import json
        try:
            payload = json.loads(msg.payload)
            if "power_w" in payload:
                self.ocpp.state.power_w = float(payload["power_w"])
            if "current_a" in payload:
                self.ocpp.state.current_a = float(payload["current_a"])
            if "energy_kwh" in payload:
                self.ocpp.state.energy_kwh = float(payload["energy_kwh"])
            self.async_set_updated_data(self.ocpp.state)
        except Exception:
            pass

    @callback
    def _on_mqtt_soc(self, msg) -> None:
        """Handle MQTT state-of-charge message."""
        try:
            self.ocpp.state.soc_percent = float(msg.payload)
            self.async_set_updated_data(self.ocpp.state)
        except ValueError:
            pass

    @callback
    def _on_mqtt_response(self, msg) -> None:
        """Handle MQTT command response."""
        _LOGGER.debug("MQTT response: %s", msg.payload)

    async def _publish_mqtt_status(self) -> None:
        """Publish current state to MQTT for external consumers."""
        import json
        state = self.ocpp.state
        payload = {
            "status": state.connector_status,
            "charging": state.charging,
            "power_w": round(state.power_w, 1),
            "current_a": round(state.current_a, 2),
            "energy_kwh": round(state.energy_kwh, 3),
            "cost_sek": round(state.accumulated_cost, 2),
            "soc": state.soc_percent,
            "cable": state.cable_connected,
            "mode": self.charge_mode,
            "price_ore": self.current_price,
        }
        try:
            await mqtt.async_publish(
                self.hass,
                self._topic("state"),
                json.dumps(payload),
                retain=True,
            )
        except Exception as err:
            _LOGGER.debug("[MQTT] Publish failed (non-critical): %s", err)

    # ------------------------------------------------------------------ #
    #  User commands                                                        #
    # ------------------------------------------------------------------ #

    async def _auto_start_with_limit(self, limit_a: float) -> None:
        """Set current limit on charger hardware, then send RemoteStart."""
        _LOGGER.info("[SmartCharge] Setting limit %.0f A before auto-start", limit_a)
        await self.ocpp.set_charging_limit(limit_a)
        await self.ocpp.remote_start_transaction()

    async def async_start_charging(self) -> None:
        """Manually start charging."""
        if not self.ocpp.state.connected:
            _LOGGER.warning("Cannot start: charger not connected")
            return
        new_limit = self.smart_controller.recommended_current(
            self.max_current, self.current_price, self.charge_mode
        )
        self.current_limit_a = new_limit
        await self.ocpp.set_charging_limit(new_limit)
        self._manual_start_requested = True
        await self.ocpp.remote_start_transaction()
        await self.async_refresh()

    async def async_stop_charging(self) -> None:
        """Manually stop charging."""
        self._manual_start_requested = False
        self._manual_stop_requested = True
        await self.ocpp.remote_stop_transaction()
        await self.async_refresh()

    async def async_set_max_current(self, current_a: float) -> None:
        """Update max allowed current."""
        self.max_current = min(current_a, float(self.config.get(CONF_MAX_CURRENT, 32)))
        self.current_limit_a = self.max_current
        if self.ocpp.state.charging:
            await self.ocpp.set_charging_limit(self.max_current)
        await self.async_refresh()

    def set_active_vehicle(self, vehicle: dict) -> None:
        """Switch the active vehicle, updating capacity and SOC entity immediately."""
        prev_name = self.active_vehicle.get(VEHICLE_NAME) if self.active_vehicle else None
        new_name = vehicle.get(VEHICLE_NAME)
        if prev_name and prev_name != new_name:
            _LOGGER.info(
                "[Vehicle] Switching %s → %s, resetting session_total_kwh (was %.2f kWh)",
                prev_name, new_name, self._session_total_kwh,
            )
            self._session_total_kwh = 0.0
        self.active_vehicle = vehicle
        self.battery_capacity_kwh = float(vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
        self.soc_entity = vehicle.get(VEHICLE_SOC_ENTITY, "")
        self.soc_unit = vehicle.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT)
        # Reset SOC estimation so next session starts fresh
        self._session_start_soc = None
        self._soc_source = "none"
        self.ocpp.state.soc_percent = None
        _LOGGER.info(
            "[Vehicle] Switched to %s (%.1f kWh, SOC entity: %s)",
            vehicle.get(VEHICLE_NAME, "?"),
            self.battery_capacity_kwh,
            self.soc_entity or "–",
        )
        self._update_soc_from_ha()
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_charge_mode(self, mode: str) -> None:
        """Update charge mode."""
        self.charge_mode = mode
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_target_soc(self, soc: float) -> None:
        self.target_soc = soc
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_target_kwh(self, kwh: float) -> None:
        self.target_kwh = kwh
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def set_allow_day_charging(self, value: bool) -> None:
        """Set day charging flag and mark as manually overridden for this session."""
        self.allow_day_charging = value
        self._day_charging_manual_override = True
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def _compute_allow_day_charging(self, now: datetime | None = None) -> bool:
        """Return True if day charging is allowed based on week schedule.

        Default rule: OFF from Sunday 18:00 to Friday 18:00 (weekdays + commute).
        ON during weekend (Fri 18:00 – Sun 18:00).
        """
        import zoneinfo
        try:
            local_tz = zoneinfo.ZoneInfo(self.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc
        t = (now or datetime.now(local_tz)).astimezone(local_tz)
        wd = t.weekday()   # Mon=0 … Sun=6
        h  = t.hour + t.minute / 60.0
        # Friday 18:00 → Sunday 18:00 = weekend window = day charging OK
        if wd == 4 and h >= 18:   # Friday evening
            return True
        if wd == 5:               # Saturday all day
            return True
        if wd == 6 and h < 18:   # Sunday until 18:00
            return True
        return False

    def _sync_allow_day_charging(self) -> None:
        """Re-evaluate auto-schedule unless user manually overrode it this session."""
        if self._day_charging_manual_override:
            return
        self.allow_day_charging = self._compute_allow_day_charging()

    def _check_notify_events(self) -> None:
        """Fire notifications on cable connect, charge start and charge stop."""
        state = self.ocpp.state
        status = state.connector_status or ""
        is_charging = state.charging

        # ── Cable disconnected → send stop-notif if not already sent (Bug 6) ──
        if status == "Available" and self._cable_session_energy_kwh > 0:
            self._send_stop_notification()

        # ── Reset _was_charging when cable is disconnected ───────────────
        if status == "Available":
            self._was_charging = False
            self._session_total_kwh = 0.0  # Fix 7: reset accumulated energy
            self._cable_session_notified_connect = False  # Fix 9: reset connect-notif flag
            self._cable_connect_time = None  # Fix 4: reset SOC reread
            self._soc_at_connect = None
            self._soc_reread_done = True
            self._start_notified_this_connection = False  # Bug 2: reset for next connection
            self._day_charging_dismissed = False  # Bug 3: reset for next connection

        # ── Cable connected (Preparing) ──────────────────────────────────
        if status == "Preparing" and self._manual_stop_requested:
            _LOGGER.debug("[Notify] Skippar Inkopplad – manuellt stopp")
            self._manual_stop_requested = False
            return

        if (
            self._notify_on_connect
            and status == "Preparing"
            and self._last_connector_status_notify != "Preparing"
            and not self._cable_session_notified_connect
            and (
                self._last_connect_notify_time is None
                or (datetime.now(timezone.utc) - self._last_connect_notify_time).total_seconds() > 10
            )
        ):
            self._notified_connect_session = state.session_id
            self._cable_session_notified_connect = True  # Fix 9: mark connect-notif sent
            self._preparing_timestamp = datetime.now(timezone.utc)
            self._cable_connect_time = datetime.now(timezone.utc)  # Fix 4: start SOC reread window
            self._soc_at_connect = self.ocpp.state.soc_percent
            self._soc_reread_done = False
            self._last_connect_notify_time = datetime.now(timezone.utc)
            self._was_charging = False
            self._start_notified_this_connection = False  # Bug 2: reset for new connection
            self._notified_start_session = None  # allow new start-notif for coming session
            # Reset cost tracking for new session at cable connect
            self._session_total_kwh += self.ocpp.state.energy_kwh  # Fix 7: save previous sub-session energy
            self.ocpp.state.accumulated_cost = 0.0
            self._last_cost_energy_kwh = 0.0
            # Bug 6: Reset cable session accumulators
            self._cable_session_energy_kwh = 0.0
            self._cable_session_cost_sek = 0.0
            self._cable_session_start_notified = False
            self._cable_session_stop_notified = False
            self._cable_session_start_time = datetime.now(timezone.utc)
            _LOGGER.debug("[Session] Ny kabelsession – nollställer ackumulatorer")
            plan = self.charge_plan
            _veh = self.active_vehicle
            self.notifier.on_cable_connected(
                soc_pct=state.soc_percent,
                plan_start=plan.start if plan and plan.feasible else None,
                plan_end=plan.end if plan and plan.feasible else None,
                energy_kwh=plan.energy_kwh if plan else None,
                estimated_cost_sek=plan.estimated_cost_sek if plan else None,
                vehicle_name=_veh.get(VEHICLE_NAME, "") if _veh else "",
                detection_reason=self._last_detection_reason,
                vehicles=self._vehicles,
            )

        # ── Charging started ─────────────────────────────────────────────
        _LOGGER.debug(
            "[Notify] start-check: notify_on_start=%s is_charging=%s was_charging=%s "
            "notified_start=%s session_id=%s",
            self._notify_on_start, is_charging, self._was_charging,
            self._notified_start_session, state.session_id
        )
        # Bug 2: Use _cable_session_start_notified – one start-notif per cable session
        if (
            self._notify_on_start
            and not self._cable_session_start_notified
            and is_charging
            and state.power_w > 100
        ):
            self._cable_session_start_notified = True
            self._notified_start_session = state.session_id
            self._start_notified_this_connection = True
            self._last_cost_energy_kwh = 0.0
            self._last_transaction_start = datetime.now(timezone.utc)
            plan = self.charge_plan
            self.notifier.on_charging_started(
                soc_pct=state.soc_percent,
                current_a=state.current_a,
                power_kw=state.power_w / 1000,
                plan_end=plan.end if plan and plan.feasible else None,
                estimated_end=self.estimated_completion,
            )

        # ── Charging stopped (Bug 4: delayed 15s for fresh SOC) ─────────
        if (
            self._notify_on_stop
            and not is_charging
            and self._was_charging
            and self._notified_stop_session != state.session_id
            and self._notified_start_session == state.session_id
            and not (
                self._preparing_timestamp is not None
                and (datetime.now(timezone.utc) - self._preparing_timestamp).total_seconds() < 5
            )
        ):
            self._notified_stop_session = state.session_id
            elapsed = self.elapsed_seconds or 0
            # Capture values now (energy/cost), but delay sending so SOC can update
            _stop_energy = state.energy_kwh
            _stop_cost = state.accumulated_cost
            _stop_elapsed = elapsed

            async def _send_stop_notif(_now=None):
                self._update_soc_from_ha()  # refresh SOC one more time
                self.notifier.on_charging_stopped(
                    soc_pct=self.ocpp.state.soc_percent,
                    energy_kwh=_stop_energy,
                    actual_cost_sek=_stop_cost,
                    duration_minutes=_stop_elapsed // 60,
                )

            async_call_later(self.hass, 15, _send_stop_notif)

        if self._was_charging and not is_charging:
            self._manual_start_requested = False  # charging ended, clear manual override
        self._was_charging = is_charging and state.power_w > 100
        self._last_connector_status_notify = status

    def _check_disconnect_notify(self) -> None:
        """Send notification if charger has been disconnected for >5 minutes."""
        if self.ocpp.state.connected:
            self._disconnect_since = None
            self._notified_disconnect = False
            return
        now = datetime.now(timezone.utc)
        if self._disconnect_since is None:
            self._disconnect_since = now
            return
        elapsed_min = int((now - self._disconnect_since).total_seconds() / 60)
        if elapsed_min >= 5 and not self._notified_disconnect:
            self._notified_disconnect = True
            self.notifier.on_charger_disconnected(elapsed_min)

    def _update_charge_plan(self) -> None:
        """Recalculate the optimal charge window using forecast prices."""
        # Freeze plan for 5 minutes after RemoteStart to avoid oscillation
        now = datetime.now(timezone.utc)
        if self._last_remote_start is not None:
            elapsed = (now - self._last_remote_start).total_seconds()
            if elapsed < 300:
                _LOGGER.debug("[ChargePlanner] Frozen after RemoteStart (%.0fs < 300s), skipping recalc", elapsed)
                return
        # ── Goal already reached → skip planning (Bug 1) ──
        soc = self.ocpp.state.soc_percent
        soc_reached = soc is not None and self.target_soc > 0 and soc >= self.target_soc
        kwh_reached = self.target_kwh > 0 and self.ocpp.state.energy_kwh >= self.target_kwh
        if soc_reached or kwh_reached:
            _LOGGER.debug("[ChargePlanner] Mål redan nått, hoppar över planering")
            return

        # Throttle: only recalculate every 5 minutes
        if self._last_plan_update is not None and (now - self._last_plan_update).total_seconds() < 300:
            return
        self._last_plan_update = now
        from datetime import date, time as dtime
        import math

        forecast_entity = self.config.get(CONF_PRICE_FORECAST_ENTITY, "")
        if not forecast_entity:
            return

        # Read forecast intervals from entity attributes
        state_obj = self.hass.states.get(forecast_entity)
        if state_obj is None:
            _LOGGER.debug("[ChargePlanner] Forecast entity %s not found", forecast_entity)
            return

        today_prices   = state_obj.attributes.get("today_interval_prices", []) or []
        tomorrow_prices = state_obj.attributes.get("tomorrow_interval_prices", []) or []
        all_prices = today_prices + tomorrow_prices

        if not all_prices:
            _LOGGER.debug("[ChargePlanner] No interval prices available")
            return

        # Deadline: tomorrow at DEFAULT_CHARGE_DEADLINE_HOUR local time
        from datetime import timezone as tz
        import zoneinfo
        try:
            local_tz = zoneinfo.ZoneInfo(self.hass.config.time_zone)
        except Exception:
            local_tz = tz.utc

        now_local = datetime.now(local_tz)
        # Use the next upcoming deadline: today's if it's still in the future,
        # otherwise tomorrow's. This handles the case where the car is connected
        # at e.g. 03:00 and should charge before 06:00 the same day.
        today_deadline = datetime.combine(
            now_local.date(),
            dtime(DEFAULT_CHARGE_DEADLINE_HOUR, 0),
            tzinfo=local_tz,
        )
        if today_deadline > now_local:
            deadline_local = today_deadline
        else:
            deadline_local = datetime.combine(
                now_local.date() + timedelta(days=1),
                dtime(DEFAULT_CHARGE_DEADLINE_HOUR, 0),
                tzinfo=local_tz,
            )

        # Energy needed – use vehicle with lowest SoC if multiple vehicles configured
        if len(self._vehicles) > 1:
            # Find vehicle with lowest current SoC from HA entities
            best_vehicle = None
            lowest_soc = float("inf")
            for v in self._vehicles:
                soc_ent = v.get(VEHICLE_SOC_ENTITY, "")
                soc_st = self.hass.states.get(soc_ent) if soc_ent else None
                try:
                    v_soc = float(soc_st.state) if soc_st else float("inf")
                except (ValueError, TypeError):
                    v_soc = float("inf")
                if v_soc < lowest_soc:
                    lowest_soc = v_soc
                    best_vehicle = v
            if best_vehicle:
                current_soc = lowest_soc if lowest_soc != float("inf") else 0.0
                target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                battery_capacity = float(best_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
                _LOGGER.debug("[ChargePlanner] Multi-vehicle: planning for %s soc=%.0f%%",
                    best_vehicle.get("name", "?"), current_soc)
            else:
                current_soc = self.ocpp.state.soc_percent or 0.0
                target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                battery_capacity = self.battery_capacity_kwh
        else:
            current_soc = self.ocpp.state.soc_percent
            if current_soc is None:
                current_soc = 0.0
            target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
            battery_capacity = self.battery_capacity_kwh
        soc_needed = max(0.0, target_soc - current_soc)
        # Fix 7: subtract already-charged energy in this cable session.
        # Only include active-transaction energy if a transaction is actually running,
        # otherwise state.energy_kwh may be a stale value from the previous session.
        active_tx_energy = self.ocpp.state.energy_kwh if self.ocpp.state.transaction_id is not None else 0.0
        already_charged_kwh = self._session_total_kwh + active_tx_energy
        energy_needed = max(0.0, (soc_needed / 100.0) * battery_capacity / DEFAULT_CHARGE_EFFICIENCY - already_charged_kwh)

        # Power in kW: use schedule current, capped by vehicle's max current if set
        voltage = DEFAULT_VOLTAGE
        schedule_current = self.schedule.current_limit()
        vehicle_max_a = int((self.active_vehicle or {}).get(VEHICLE_MAX_CURRENT_A, 0))
        effective_current = min(schedule_current, vehicle_max_a) if vehicle_max_a > 0 else schedule_current
        power_kw = (effective_current * voltage * self.num_phases) / 1000.0

        _LOGGER.debug(
            "[ChargePlanner] Planning: soc=%.0f%%→%.0f%% energy=%.2f kWh power=%.1f kW deadline=%s",
            current_soc, target_soc, energy_needed, power_kw,
            deadline_local.strftime("%Y-%m-%d %H:%M"),
        )

        # Build a schedule_fn that maps a local datetime -> current limit in A
        schedule = self.schedule

        def _schedule_fn(local_dt: datetime) -> float:
            return schedule.current_limit_at(local_dt)

        # Filter out daytime intervals if day charging is not allowed
        # _force_day_plan is set when user explicitly picks day via notification
        if not self.allow_day_charging and not self._force_day_plan:
            filtered_prices = [
                iv for iv in all_prices
                if not schedule.is_day_time(_to_utc(iv["time"]).astimezone(local_tz).time())
            ]
            if not filtered_prices:
                _LOGGER.debug("[ChargePlanner] No night intervals available, using all")
                filtered_prices = all_prices
        else:
            filtered_prices = all_prices

        prev_plan = self.charge_plan

        _use_contiguous = self.planner_algorithm == PLANNER_ALGO_CONTIGUOUS
        _common_kwargs = dict(
            interval_prices=filtered_prices,
            energy_needed_kwh=energy_needed,
            power_kw=power_kw,
            deadline=deadline_local,
            now=now_local,
            schedule_fn=_schedule_fn,
            voltage=DEFAULT_VOLTAGE,
            num_phases=self.num_phases,
            local_tz=local_tz,
        )

        self.charge_plan = plan_cheapest_window(
            **_common_kwargs, contiguous=_use_contiguous,
        )

        # Calculate the alternative algorithm's cost for comparison sensor
        alt_plan = plan_cheapest_window(
            **_common_kwargs, contiguous=not _use_contiguous,
        )
        self._alt_plan = alt_plan

        # Notify if day charging allowed and plan lands in daytime
        if (
            self.allow_day_charging
            and self.charge_plan
            and self.charge_plan.feasible
        ):
            plan_start_local = self.charge_plan.start.astimezone(local_tz)
            plan_end_local   = self.charge_plan.end.astimezone(local_tz)
            if schedule.is_day_time(plan_start_local.time()):
                # Only notify if plan changed significantly (new session or start shifted)
                notify = (
                    prev_plan is None
                    or not prev_plan.feasible
                    or abs((self.charge_plan.start - prev_plan.start).total_seconds()) > 900
                )
                if notify and not self._day_charging_dismissed:  # Bug 3: respect dismiss
                    # Calculate what night-only plan would cost for comparison
                    night_prices = [
                        iv for iv in all_prices
                        if not schedule.is_day_time(
                            _to_utc(iv["time"]).astimezone(local_tz).time()
                        )
                    ]
                    night_plan = plan_cheapest_window(
                        interval_prices=night_prices or all_prices,
                        energy_needed_kwh=energy_needed,
                        power_kw=power_kw,
                        deadline=deadline_local,
                        now=now_local,
                        schedule_fn=_schedule_fn,
                        voltage=DEFAULT_VOLTAGE,
                        num_phases=self.num_phases,
                        local_tz=local_tz,
                    ) if night_prices else None

                    self.notifier.on_day_charging_chosen(
                        day_start=plan_start_local,
                        day_end=plan_end_local,
                        day_cost=self.charge_plan.estimated_cost_sek,
                        day_avg_ore=self.charge_plan.avg_price_ore_kwh,
                        night_start=night_plan.start.astimezone(local_tz) if night_plan and night_plan.feasible else None,
                        night_end=night_plan.end.astimezone(local_tz) if night_plan and night_plan.feasible else None,
                        night_cost=night_plan.estimated_cost_sek if night_plan and night_plan.feasible else None,
                        night_avg_ore=night_plan.avg_price_ore_kwh if night_plan and night_plan.feasible else None,
                    )

    def _adjust_update_interval(self) -> None:
        """Speed up or slow down polling based on current charger state."""
        status = self.ocpp.state.connector_status or ""
        charging = self.ocpp.state.charging

        if charging:
            seconds = 10          # Active charging – fast updates
        elif status in ("Preparing", "Finishing", "SuspendedEV", "SuspendedEVSE"):
            seconds = 20          # Cable connected but not charging
        elif status == "Available":
            seconds = 60          # Idle and ready
        else:
            seconds = 60          # Unknown / offline

        new_interval = timedelta(seconds=seconds)
        if self.update_interval != new_interval:
            _LOGGER.debug(
                "[Coordinator] Update interval changed to %ds (status=%s charging=%s)",
                seconds, status, charging,
            )
            self.update_interval = new_interval

    def _build_rest_client(self) -> ChargerRestClient | None:
        """Build a REST client from current config, or None if not configured."""
        base_url = self.config.get(CONF_REST_BASE_URL, "").strip()
        if not base_url:
            return None
        return ChargerRestClient(
            base_url=base_url,
            auth_type=self.config.get(CONF_REST_AUTH_TYPE, REST_AUTH_NONE),
            username=self.config.get(CONF_REST_USERNAME, ""),
            password=self.config.get(CONF_REST_PASSWORD, ""),
            bearer_token=self.config.get(CONF_REST_TOKEN, ""),
        )

    async def async_rest_call(
        self,
        method: str,
        endpoint: str,
        params: dict | None = None,
        body=None,
    ) -> dict:
        """Execute a REST call to the charger and return the result dict."""
        _LOGGER.debug("[REST] %s %s params=%s body=%s", method, endpoint, params, body)
        client = self._build_rest_client()
        if client is None:
            return {
                "status_code": None,
                "ok": False,
                "body": "REST API not configured. Add a base URL via Settings → Integrations → Configure → Edit REST API settings.",
                "headers": {},
                "url": "",
            }
        return await client.call(method=method, endpoint=endpoint,
                                 params=params, body=body)

    @property
    def elapsed_seconds(self) -> int | None:
        """Return accumulated active charging time in seconds (pauses when not charging)."""
        total = self.ocpp.state.accumulated_charging_seconds
        if self.ocpp.state.charging and self.ocpp.state._charging_start:
            # Add current active segment
            delta = datetime.now(timezone.utc) - self.ocpp.state._charging_start
            total += int(delta.total_seconds())
        if self.ocpp.state.transaction_id is None:
            return None
        return total

    def _cable_session_elapsed_minutes(self) -> int:
        """Return elapsed minutes since cable was connected."""
        if self._cable_session_start_time is None:
            return 0
        from homeassistant.util import dt as dt_util
        return int((dt_util.utcnow() - self._cable_session_start_time).total_seconds() / 60)

    def _send_stop_notification(self) -> None:
        """Send a delayed stop notification with fresh SOC (Bug 4).

        Used by both SuspendedEV handling (Bug 5) and cable-out (Bug 6).
        Triggers kia_uvo.force_update first, then waits 60s for SOC to sync.
        """
        if self._cable_session_stop_notified:
            return
        self._cable_session_stop_notified = True

        # Trigger vehicle cloud sync for fresh SOC
        self.hass.async_create_task(
            self.hass.services.async_call("kia_uvo", "force_update", {})
        )

        energy_kwh = self._cable_session_energy_kwh
        cost_sek = self._cable_session_cost_sek

        async def _delayed(_now=None):
            self._update_soc_from_ha()
            self.notifier.on_charging_stopped(
                soc_pct=self.ocpp.state.soc_percent,
                energy_kwh=energy_kwh,
                actual_cost_sek=cost_sek,
                duration_minutes=self._cable_session_elapsed_minutes(),
            )

        async_call_later(self.hass, 60, _delayed)

    def _on_charger_state_update(self, state: ChargerState) -> None:
        """Called from OCPPClient when state changes (already on HA event loop)."""
        if self.hass:
            self._on_charger_state_update_async(state)

    @callback
    def _on_charger_state_update_async(self, state: ChargerState) -> None:
        """Main-thread handler for charger state changes."""
        self._update_price_from_ha()
        self._apply_current_schedule()
        self._update_soc_from_ha()
        self._update_smart_charging()
        self._check_notify_events()
        self.async_set_updated_data(state)
