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
from homeassistant.helpers.event import async_call_later, async_track_state_change_event
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
    CONF_NOTIFY_DASHBOARD_URL,
    DEFAULT_CHARGE_DEADLINE_HOUR,
    INPUT_DATETIME_DEADLINE,
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
    DAY_OFFER_EARLIEST_HOUR,
    PRESENCE_ENTITIES,
    PRESENCE_HOME_STATES,
    NOTIFY_ACTION_USE_DAY,
    NOTIFY_ACTION_USE_NIGHT,
    NOTIFY_ACTION_DISMISS,
    NOTIFY_ACTION_SELECT_VEHICLE,
    PLANNER_ALGO_GREEDY,
    PLANNER_ALGO_CONTIGUOUS,
    SELECT_PLANNER_ALGORITHM,
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
from .charge_planner import ChargePlan, plan_cheapest_window, _to_utc, INTERVAL_MINUTES, INTERVAL_HOURS
from .price_cap import select_price_cap_slots
from .charge_windows import build_charge_windows, update_windows_actual
from .deadline import compute_deadline, helper_state_to_hhmm
from .soc_estimate import estimate_soc
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
            # Bug 21: håll dismissad till nästa lokala midnatt
            import zoneinfo
            local_tz = zoneinfo.ZoneInfo(coordinator.hass.config.time_zone)
            now_local = datetime.now(local_tz)
            midnight = (now_local + timedelta(days=1)).replace(
                hour=0, minute=0, second=0, microsecond=0,
            )
            coordinator._day_charging_dismissed_until = midnight
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

    # Bug 39: input_datetime.charger_target_time saknade en state-lyssnare — deadline
    # lästes bara på begäran (_get_manual_deadline_str), så en ändring syntes i
    # Laddfönster-grafen/sensorn först vid nästa oberoende _update_charge_plan()-anrop
    # (periodisk poll, OCPP-event etc.), inte omedelbart som pristaket (set_price_cap).
    @callback
    def _handle_deadline_change(event) -> None:
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        if new_state is None or (old_state is not None and old_state.state == new_state.state):
            return
        _LOGGER.info(
            "[Deadline] %s ändrad %s → %s, planerar om",
            coordinator._deadline_entity_id,
            old_state.state if old_state else None,
            new_state.state,
        )
        coordinator._last_plan_update = None  # bypass throttle, samma mönster som set_price_cap
        coordinator._update_charge_plan()
        coordinator.async_set_updated_data(coordinator.ocpp.state)

    entry.async_on_unload(
        async_track_state_change_event(
            hass, [coordinator._deadline_entity_id], _handle_deadline_change,
        )
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
    coordinator.notifier.dashboard_url = entry.data.get(CONF_NOTIFY_DASHBOARD_URL, "")
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
        self.planner_algorithm: str = self.config.get(
            SELECT_PLANNER_ALGORITHM, PLANNER_ALGO_GREEDY
        )
        self.target_soc: float = 80.0
        self.target_kwh: float = 0.0
        self.current_price: float | None = None
        self.current_limit_a: float = self.max_current
        self.session_start: datetime | None = None
        self.estimated_completion: datetime | None = None
        self.estimated_remaining_minutes: int | None = None
        self.charge_plan: ChargePlan | None = None
        self._alt_plan: ChargePlan | None = None
        # Feature 3: Charge Windows sensor state
        self._charge_windows: list[dict] = []                       # per-slot plan + actual
        self._charge_windows_meta: dict = {}                        # plan metadata
        self._charge_windows_energy_at_slot_start: dict[str, float] = {}  # start-ISO → kWh baseline
        self._charge_windows_plan_ref: "ChargePlan | None" = None   # identity guard for rebuilds
        self._last_plan_update: datetime | None = None
        self._day_charging_manual_override: bool = False  # True = user toggled manually
        self._force_day_plan: bool = False   # True after user picks day via notification
        self.allow_day_charging: bool = self._compute_allow_day_charging()
        # Feature 6: manual deadline now comes from the input_datetime helper
        # (read on demand via _get_manual_deadline_str), not an in-memory string.
        self._deadline_entity_id: str = INPUT_DATETIME_DEADLINE

        # Feature 5: price cap charging (0 = disabled → ordinary Smart planning)
        self.price_cap_ore_kwh: float = 0.0
        self._price_cap_intervals: list[tuple[datetime, datetime]] = []  # merged active windows
        self._price_cap_raw_slots: list[dict] = []  # [{time, price_kwh, energy_kwh}] for the sensor

        # Notifications
        self.notifier = ChargerNotifier(
            hass=hass,
            notify_target=self.config.get(CONF_NOTIFY_TARGET, ""),
            enabled=self.config.get(CONF_NOTIFY_ENABLED, False),
            dashboard_url=self.config.get(CONF_NOTIFY_DASHBOARD_URL, ""),
        )
        self._notify_on_connect: bool = self.config.get(CONF_NOTIFY_ON_CONNECT, True)
        self._notify_on_start:   bool = self.config.get(CONF_NOTIFY_ON_START, True)
        self._notify_on_stop:    bool = self.config.get(CONF_NOTIFY_ON_STOP, True)
        self._was_charging: bool = False
        self._charging_started_at: datetime | None = None  # Bug 34: fryst laddstartstid för PlannedChargeStartSensor
        # Bug 28: plan windows frozen at session start; None = no active frozen session.
        # An active session is gated by this list, not by a plan recalculated mid-charge.
        self._session_plan_intervals: list[tuple[datetime, datetime]] | None = None
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
        self._day_charging_dismissed_until: datetime | None = None  # Bug 21: midnight reset
        self._day_offer_notified_date = None  # date of last presence-based day-charging offer
        self._charging_seen_this_session: bool = False  # Bug 10: guard stop-notif at restart
        self._suspended_ev_since: datetime | None = None  # Bug 5: SuspendedEV tracking
        # Bug 38: init False – efter HA-omstart krävs en äkta Available innan Preparing
        # tolkas som genuin inkoppling. True-init gjorde att en omstart under pågående
        # kabelsession armerade en falsk "genuin inkoppling" som fyrade vid nästa
        # transaktionspaus (RemoteStop → Finishing → Preparing) och raderade
        # _session_total_kwh, förfalskade SoC-estimatet och skickade falsk Inkopplad-notis.
        self._cable_was_available: bool = False  # Bug 13A/38: True only after genuine Available status
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
        # Bug 16: always replan so new tomorrow prices are picked up mid-charge.
        # Pingpong protection lives in _update_smart_charging via _last_remote_start.
        self._update_charge_plan()
        self._rebuild_charge_windows()          # Feature 3
        self._update_charge_windows_actual()    # Feature 3
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
            "session_start_soc": self._session_start_soc,   # Bug 30: SOC estimation baseline
            "session_total_kwh": self._session_total_kwh,   # Bug 30: energy paired with that baseline
            "session_plan_intervals": (   # Bug 31: persist Bug 28 frozen plan (was in-memory only)
                [[s.isoformat(), e.isoformat()] for s, e in self._session_plan_intervals]
                if self._session_plan_intervals is not None else None
            ),
            "charge_mode": self.charge_mode,
            "price_cap_ore_kwh": self.price_cap_ore_kwh,   # Feature 5
            "target_soc": self.target_soc,
            "target_kwh": self.target_kwh,
            "active_vehicle_name": self.active_vehicle.get(VEHICLE_NAME) if self.active_vehicle else None,
            "allow_day_charging": self.allow_day_charging,
            "day_charging_manual_override": self._day_charging_manual_override,
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
            # Feature 6: manual deadline lives in the input_datetime helper now;
            # any legacy "manual_deadline" key in old Store data is ignored.
            self.price_cap_ore_kwh = float(data.get("price_cap_ore_kwh", 0.0))  # Feature 5
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
            # Bug 26: restore the manual day-charging override so it survives restart.
            # Only restore when the user actually toggled it; otherwise leave
            # _day_charging_manual_override=False so _sync_allow_day_charging() keeps
            # following the weekday/weekend auto-schedule.
            if data.get("day_charging_manual_override"):
                self._day_charging_manual_override = True
                self.allow_day_charging = bool(data.get("allow_day_charging", False))
                _LOGGER.info(
                    "[Store] Återställde allow_day_charging=%s (manuell override)",
                    self.allow_day_charging,
                )
            saved_vehicle = data.get("active_vehicle_name")
            if saved_vehicle:
                match = next((v for v in self._vehicles if v.get(VEHICLE_NAME) == saved_vehicle), None)
                if match:
                    self.set_active_vehicle(match)
                    _LOGGER.info("[Store] Återställde aktivt fordon: %s", saved_vehicle)
                else:
                    _LOGGER.warning("[Store] Sparat fordon '%s' finns inte längre i konfigurationen", saved_vehicle)
            # Bug 30: restore the SOC estimation baseline + its paired energy LAST, so a
            # mid-session restart can't desync them. Must come AFTER set_active_vehicle()
            # above, which resets _session_start_soc/_session_total_kwh. Previously
            # _session_start_soc was in-memory only: a restart wiped it, it was re-captured
            # at a post-charge value (e.g. 82%) while energy_kwh persisted (~12.3 kWh) →
            # the estimate double-counted and stopped charging far too early. Restoring a
            # non-None value also stops the capture guard in _update_soc_from_ha
            # (state.charging and _session_start_soc is None) from overwriting it.
            if data.get("session_start_soc") is not None:
                self._session_start_soc = data.get("session_start_soc")
                self._session_total_kwh = data.get("session_total_kwh", 0.0)
                _LOGGER.info(
                    "[Store] Återställde session-baslinje: start_soc=%.1f%% total_kwh=%.2f",
                    self._session_start_soc, self._session_total_kwh,
                )
            # Bug 31: restore Bug 28's frozen plan windows so a mid-charge restart doesn't
            # re-expose the "Outside plan window" abort (the windows were in-memory only).
            _spi = data.get("session_plan_intervals")
            if _spi:
                try:
                    self._session_plan_intervals = [
                        (datetime.fromisoformat(s), datetime.fromisoformat(e)) for s, e in _spi
                    ]
                    _LOGGER.info(
                        "[Store] Återställde fryst planfönster (%d intervall)",
                        len(self._session_plan_intervals),
                    )
                except (ValueError, TypeError):
                    self._session_plan_intervals = None
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

        # 1. OCPP satte SOC i förra cykeln – behåll värdet, återställ källa så HA-entitet
        #    kan ta över nästa cykel (Bug 9: förhindrar permanent "ocpp"-låsning)
        if self._soc_source == "ocpp":
            self._soc_source = "entity" if self.soc_entity else "none"
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

        # 2b. Entitet tillgänglig → använd alltid, även under laddning (Bug 9)
        if entity_soc is not None:
            if state.soc_percent != entity_soc:
                _LOGGER.debug("[SOC] Uppdaterar från HA-entitet: %.1f%% → %.1f%%",
                    state.soc_percent or 0.0, entity_soc)
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

    def _charging_goal_reached(self) -> tuple[bool, str]:
        """Return (reached, reason) when the charging goal is met.

        The goal counts as reached when either of these hold:
          • (estimated) SOC has reached the configured target_soc
          • delivered energy has reached the user target_kwh

        SOC uses the same estimate as the planner (Bug 8) so vehicles whose SOC
        entity doesn't refresh during charging (Kia/Skoda) stop at the real target,
        not the stale reported value.

        Bug 29: the old "delivered energy >= plan.energy_kwh" condition was dropped.
        Since the plan recomputes mid-charge (Bug 16) from an estimated SOC that
        itself includes the delivered energy, plan.energy_kwh is *remaining* energy
        (≈ TOTAL − delivered). Comparing delivered against it tripped at delivered ≈
        TOTAL/2 → charging stopped at the SOC midpoint. Estimated-SOC ≥ target is the
        correct, non-circular completion criterion.

        Used in two places so auto-start and auto-stop stay symmetric: the stop
        branch ends an active session, and the auto-start branch suppresses a
        fresh RemoteStart within an open plan window. Without the latter the two
        ping-pong every ~5 min once SOC hits target while the window is still
        open (Bug 23).
        """
        state = self.ocpp.state
        active_tx_energy = state.energy_kwh if state.transaction_id is not None else 0.0
        already_charged_kwh = self._session_total_kwh + active_tx_energy
        soc = estimate_soc(
            self._session_start_soc,
            already_charged_kwh,
            self.battery_capacity_kwh,
            DEFAULT_CHARGE_EFFICIENCY,
            state.soc_percent,
        )
        if soc is not None and self.target_soc > 0 and soc >= self.target_soc:
            return True, f"SOC {soc:.0f}% >= mål {self.target_soc:.0f}%"
        if self.target_kwh > 0 and state.energy_kwh >= self.target_kwh:
            return True, f"Energi {state.energy_kwh:.2f} kWh >= mål {self.target_kwh:.2f} kWh"
        return False, ""

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

        # ── Bug 13B: SuspendedEV guard – car refuses charging, skip auto-start ──
        connector_status = self.ocpp.state.connector_status or ""
        if connector_status == "SuspendedEV":
            _LOGGER.debug("[SmartCharge] SuspendedEV – bilen nekar laddning, skippar auto-start")
            return

        # ── Plan-based auto-start (act only when cable is connected) ─────
        if _auto_start_in_window:
            # Bug 23: don't auto-start if the charging goal is already reached.
            # Otherwise auto-start and the goal-reached stop ping-pong every
            # ~5 min (300s RemoteStart guard) while the plan window stays open.
            _goal_reached, _goal_reason = self._charging_goal_reached()
            if _goal_reached:
                _LOGGER.info("[SmartCharge] Auto-start undertryckt – mål redan nått (%s)", _goal_reason)
                return
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
            # Bug 28: freeze the plan windows for this session so a later
            # recalculation (new prices mid-charge) can't shift the window
            # out from under the active session.
            self._session_plan_intervals = list(plan.active_intervals)
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
            goal_reached, goal_reason = self._charging_goal_reached()
            if goal_reached:
                if state.charging:
                    # Bug 19: respect manual override (Immediate / user-started session).
                    # plan.energy_kwh is a planning artifact, not a user-set goal.
                    if self._manual_start_requested:
                        _LOGGER.info("[SmartCharge] Manual override aktiv, stoppar inte (mål nått men override aktiv)")
                        return
                    _LOGGER.info("[SmartCharge] Mål nått (%s), stoppar", goal_reason)
                    self._guarded_remote_stop(now_utc)
                return

            # Bug 28: an active session is gated by the plan frozen at its start,
            # not by a plan that may have been recalculated mid-session.
            if self.ocpp.state.charging and self._session_plan_intervals is not None:
                in_window = any(
                    iv_start <= now_utc <= iv_end
                    for iv_start, iv_end in self._session_plan_intervals
                )
                # Bug 28: log when the frozen plan averts a stop the recalculated plan would cause
                if in_window and not plan.is_in_window(now_utc):
                    _LOGGER.debug(
                        "[SmartCharge] Bug28: behåller aktiv session i fryst planfönster "
                        "trots att omräknad plan ligger utanför fönster"
                    )
            else:
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

        # Actively charging with measurable power – estimate from current power_w.
        # Bug 36: pass the vehicle's real battery_kwh + charge efficiency (previously
        # missing, so the function silently fell back to its 64.0 kWh default and no
        # efficiency correction), and use the Bug-29 corrected SOC estimate so this
        # can't drift from charge_planner's energy_needed calculation.
        active_tx_energy = self.ocpp.state.energy_kwh if self.ocpp.state.transaction_id is not None else 0.0
        already_charged_kwh = self._session_total_kwh + active_tx_energy
        eta_current_soc = estimate_soc(
            self._session_start_soc, already_charged_kwh, self.battery_capacity_kwh,
            DEFAULT_CHARGE_EFFICIENCY, self.ocpp.state.soc_percent,
        )
        self.estimated_completion = self.smart_controller.estimate_completion_time(
            session_kwh=self.ocpp.state.energy_kwh,
            target_kwh=self.target_kwh if self.target_kwh > 0 else None,
            target_soc=self.target_soc if self.target_soc > 0 else None,
            current_soc=eta_current_soc,
            power_w=self.ocpp.state.power_w,
            battery_kwh=self.battery_capacity_kwh,
            efficiency=DEFAULT_CHARGE_EFFICIENCY,
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
        # Bug 28: freeze current plan windows for this manually-started session.
        if self.charge_plan and self.charge_plan.active_intervals:
            self._session_plan_intervals = list(self.charge_plan.active_intervals)
        await self.ocpp.remote_start_transaction()
        await self.async_refresh()

    async def async_stop_charging(self) -> None:
        """Manually stop charging."""
        self._manual_start_requested = False
        self._manual_stop_requested = True
        await self.ocpp.remote_stop_transaction()
        await self.async_refresh()

    async def async_start_if_ready(self) -> None:
        """Start charging if cable is connected and charger is idle.

        Called when switching to Immediate mode. Guard around async_start_charging
        so it only fires if the charger is in a startable state (Preparing or
        SuspendedEVSE) and not already charging.
        """
        state = self.ocpp.state
        startable = state.connector_status in ("Preparing", "SuspendedEVSE")
        if not startable or state.charging:
            _LOGGER.debug(
                "[Immediate] auto-start skipped (status=%s charging=%s)",
                state.connector_status, state.charging,
            )
            return
        _LOGGER.info("[Immediate] Kabeln inkopplad – startar laddning automatiskt")
        await self.async_start_charging()

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
        self._last_plan_update = None  # Bug 5: bypass throttle
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_charge_mode(self, mode: str) -> None:
        """Update charge mode."""
        self.charge_mode = mode
        self._last_plan_update = None  # Bug 5: bypass throttle
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_target_soc(self, soc: float) -> None:
        self.target_soc = soc
        self._last_plan_update = None  # Bug 5: bypass throttle
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    def set_target_kwh(self, kwh: float) -> None:
        self.target_kwh = kwh
        self._last_plan_update = None  # Bug 5: bypass throttle
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)

    async def set_price_cap(self, cap_ore_kwh: float) -> None:
        """Feature 5: update the price cap and immediately re-plan.

        cap_ore_kwh > 0 activates price-cap mode in Smart charging; 0 restores
        ordinary Smart planning.
        """
        self.price_cap_ore_kwh = max(0.0, cap_ore_kwh)
        self._last_plan_update = None  # bypass throttle
        self._update_charge_plan()
        self.async_set_updated_data(self.ocpp.state)
        await self._save_state()

    # ------------------------------------------------------------------ #
    #  Helpers                                                             #
    # ------------------------------------------------------------------ #

    def set_allow_day_charging(self, value: bool) -> None:
        """Set day charging flag and mark as manually overridden for this session."""
        _LOGGER.debug(
            "[DayCharging] set_allow_day_charging(%s) (was %s, manuell override aktiveras)",
            value, self.allow_day_charging,
        )
        self.allow_day_charging = value
        self._day_charging_manual_override = True
        self._last_plan_update = None  # Bug 5: bypass throttle
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

    def _get_manual_deadline_str(self) -> str:
        """Feature 6: read the manual deadline from the input_datetime helper.

        Returns 'HH:MM' when the helper is set to a non-midnight time, otherwise
        '' (= automatic deadline). 00:00 / missing / unknown all mean "unset".
        """
        state = self.hass.states.get(self._deadline_entity_id)
        return helper_state_to_hhmm(state.state if state is not None else None)

    def _reset_deadline_helper(self) -> None:
        """Feature 6: reset the input_datetime helper to 00:00:00 (= automatic).

        Guarded: only fires the service call if the helper exists, so a missing
        helper can't spam errors. Called on cable disconnect (Available).
        """
        if self.hass.states.get(self._deadline_entity_id) is None:
            return
        self.hass.async_create_task(
            self.hass.services.async_call(
                "input_datetime",
                "set_datetime",
                {"entity_id": self._deadline_entity_id, "time": "00:00:00"},
                blocking=False,
            )
        )

    def _compute_deadline(
        self,
        now_local: datetime,
        local_tz,
        all_prices: list,
    ) -> datetime:
        """Return the charging deadline (Feature 4 / Feature 6).

        Delegates to deadline.compute_deadline: a manual HH:MM value (now read
        from the input_datetime helper) wins; otherwise allow_day_charging /
        weekend → end of available price data, weekday → 06:00 (Bug 27).
        """
        return compute_deadline(
            now_local,
            local_tz,
            all_prices,
            manual_deadline_str=self._get_manual_deadline_str(),  # Feature 6
            deadline_hour=DEFAULT_CHARGE_DEADLINE_HOUR,
            allow_day_charging=self.allow_day_charging,
        )

    def _someone_home(self) -> bool:
        """Return True if any tracked person/vehicle is currently home."""
        home = False
        seen: list[str] = []
        for entity_id in PRESENCE_ENTITIES:
            st = self.hass.states.get(entity_id)
            state = st.state if st is not None else "<missing>"
            seen.append(f"{entity_id}={state}")
            if st is not None and st.state.lower() in PRESENCE_HOME_STATES:
                home = True
        _LOGGER.debug("[Presence] someone_home=%s (%s)", home, ", ".join(seen))
        return home

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
            self._charging_started_at = None  # Bug 34: nollställ fryst starttid vid urkoppling
            self._session_plan_intervals = None  # Bug 28: clear frozen plan on cable disconnect
            self._session_total_kwh = 0.0  # Fix 7: reset accumulated energy
            self._cable_session_notified_connect = False  # Fix 9: reset connect-notif flag
            self._cable_connect_time = None  # Fix 4: reset SOC reread
            self._soc_at_connect = None
            self._soc_reread_done = True
            self._start_notified_this_connection = False  # Bug 2: reset for next connection
            self._day_charging_dismissed = False  # Bug 3: reset for next connection
            self._day_charging_dismissed_until = None  # Bug 21
            self._charging_seen_this_session = False  # Bug 10: reset for next connection
            self._cable_was_available = True  # Bug 13A: genuine cable disconnect
            self._reset_deadline_helper()    # Feature 6: clear manual deadline on disconnect
            self.price_cap_ore_kwh = 0.0     # Feature 5: clear price cap on disconnect
            self._price_cap_intervals = []
            self._price_cap_raw_slots = []
            self._last_plan_update = None    # Feature 5/6: force re-plan with automatic deadline
            self.hass.async_create_task(self._save_state())  # Feature 5: persist the clear
            _LOGGER.debug("[Bug13A] Available → cable_was_available=True")

        # ── Cable connected (Preparing) ──────────────────────────────────
        if status == "Preparing" and self._manual_stop_requested:
            _LOGGER.debug("[Notify] Skippar Inkopplad – manuellt stopp")
            self._manual_stop_requested = False
            return

        # Bug 13A: Log when Preparing is skipped due to Garo internal reset
        if (
            status == "Preparing"
            and self._last_connector_status_notify != "Preparing"
            and not self._cable_was_available
        ):
            # Bug 33 / Fix 7: a Garo 15-min internal reset ends one transaction and
            # starts another within the same cable session. Save the just-completed
            # sub-session's energy before the new StartTransaction resets
            # state.energy_kwh to 0, so the SOC estimate doesn't lose it. Edge-
            # triggered (_last_connector_status_notify != "Preparing") → fires once
            # per reset, and energy_kwh here is current-session data (not the cross-
            # session stale value that plagued the genuine-connect branch).
            self._session_total_kwh += self.ocpp.state.energy_kwh
            _LOGGER.debug(
                "[Bug13A] Preparing utan föregående Available – Garo-reset, skippar "
                "Inkopplad-notis (sparar %.3f kWh, _session_total_kwh=%.3f)",
                self.ocpp.state.energy_kwh, self._session_total_kwh,
            )

        if (
            self._notify_on_connect
            and status == "Preparing"
            and self._last_connector_status_notify != "Preparing"
            and not self._cable_session_notified_connect
            and self._cable_was_available  # Bug 13A: require genuine Available before
            and (
                self._last_connect_notify_time is None
                or (datetime.now(timezone.utc) - self._last_connect_notify_time).total_seconds() > 10
            )
        ):
            self._cable_was_available = False  # Bug 13A: consume the flag
            _LOGGER.debug("[Bug13A] Genuine connect: cable_was_available consumed")
            self._notified_connect_session = state.session_id
            self._cable_session_notified_connect = True  # Fix 9: mark connect-notif sent
            self._preparing_timestamp = datetime.now(timezone.utc)
            self._cable_connect_time = datetime.now(timezone.utc)  # Fix 4: start SOC reread window
            self._soc_at_connect = self.ocpp.state.soc_percent
            self._soc_reread_done = False
            self._last_connect_notify_time = datetime.now(timezone.utc)
            self._was_charging = False
            self._start_notified_this_connection = False  # Bug 2: reset for new connection
            self._charging_seen_this_session = False  # Bug 10: reset for new connection
            self._notified_start_session = None  # allow new start-notif for coming session
            # Reset cost tracking for new session at cable connect.
            # Bug 33: a genuine connect always starts fresh. The old `+=` captured
            # state.energy_kwh, which is NOT reset on disconnect and thus held a
            # stale positive value from the previous vehicle's session → a bogus
            # already_charged_kwh → _charging_goal_reached() estimated >100% →
            # auto-start was suppressed ("mål redan nått"). The accumulation that
            # Fix 7 intended belongs in the Garo-reset branch (see above), not here.
            self._session_total_kwh = 0.0  # Bug 33: genuine connect starts fresh
            self.ocpp.state.accumulated_cost = 0.0
            self._last_cost_energy_kwh = 0.0
            # Bug 6: Reset cable session accumulators
            self._cable_session_energy_kwh = 0.0
            self._cable_session_cost_sek = 0.0
            self._cable_session_start_notified = False  # Bug 13A: only reset on genuine connect
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
            self._charging_started_at = datetime.now(timezone.utc)  # Bug 34: frys starttid när laddning börjar
            self._notified_start_session = state.session_id
            self._start_notified_this_connection = True
            self._charging_seen_this_session = True  # Bug 10: mark that we saw charging start
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

        # ── Charging stopped (Bug 10: guard + delayed 60s for fresh SOC) ─────────
        if (
            self._notify_on_stop
            and not is_charging
            and self._was_charging
            and self._notified_stop_session != state.session_id
            and self._charging_seen_this_session  # Bug 10: only if we saw charging start in this instance
            and not (
                self._preparing_timestamp is not None
                and (datetime.now(timezone.utc) - self._preparing_timestamp).total_seconds() < 5
            )
        ):
            # Bug 11: Don't send stop notification if plan has more windows ahead
            # (e.g., charging paused at a price gap but will resume soon)
            plan = self.charge_plan
            now_utc = datetime.now(timezone.utc)
            if (
                plan and plan.feasible and plan.end
                and now_utc < plan.end
            ):
                _LOGGER.info(
                    "[Notify] Laddning pausad men plan aktiv till %s – håller inne stopp-notis",
                    plan.end.astimezone().strftime("%H:%M"),
                )
            else:
                self._notified_stop_session = state.session_id
                self._charging_seen_this_session = False  # Bug 10: reset so same session doesn't trigger again
                self._cable_session_stop_notified = True  # Prevent duplicate via _send_stop_notification()
                # Capture values now in closure variables (Bug 10)
                captured_session_id = state.session_id
                captured_energy = state.energy_kwh
                captured_cost = state.accumulated_cost
                captured_elapsed = self.elapsed_seconds or 0

                # Trigger car SOC sync immediately (Bug 10)
                try:
                    self.hass.async_create_task(
                        self.hass.services.async_call("kia_uvo", "force_update", {}, blocking=False)
                    )
                except Exception:
                    pass  # Service may not exist (e.g., Skoda Enyaq)

                async def _send_stop_notif(_now=None):
                    # Guard: abort if a new session has started since we scheduled this
                    if self.ocpp.state.session_id != captured_session_id:
                        _LOGGER.debug("[Notify] Stopp-notis avbryts – ny session startad")
                        return
                    self._update_soc_from_ha()  # refresh SOC one more time
                    self.notifier.on_charging_stopped(
                        soc_pct=self.ocpp.state.soc_percent,
                        energy_kwh=captured_energy,
                        actual_cost_sek=captured_cost,
                        duration_minutes=captured_elapsed // 60,
                    )

                async_call_later(self.hass, 60, _send_stop_notif)  # Bug 10: 60s delay

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
        # Bug 21: nollställ dismissed-flagga när midnatt passerats
        if (
            self._day_charging_dismissed
            and self._day_charging_dismissed_until is not None
            and now >= self._day_charging_dismissed_until
        ):
            _LOGGER.debug("[ChargePlanner] Återställer _day_charging_dismissed efter midnatt")
            self._day_charging_dismissed = False
            self._day_charging_dismissed_until = None
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

        # ── Feature 5: Price cap mode ──────────────────────────────────────
        # When a price cap is set in Smart mode, the ordinary cheapest-window
        # planner is replaced by a simple rule: charge every slot ≤ the cap
        # (still respecting deadline + allow_day_charging). SoC target is
        # enforced by _charging_goal_reached() in _update_smart_charging().
        if self.charge_mode == CHARGE_MODE_SMART and self.price_cap_ore_kwh > 0:
            self._update_price_cap_plan(all_prices, now_local, local_tz)
            return

        deadline_local = self._compute_deadline(now_local, local_tz, all_prices)

        # Energy needed – multi-vehicle: use active vehicle when cable connected (Bug 6)
        if len(self._vehicles) > 1:
            if self.ocpp.state.cable_connected and self.active_vehicle:
                # Cable connected – plan for the vehicle that is actually charging
                current_soc = self.ocpp.state.soc_percent or 0.0
                target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                battery_capacity = float(self.active_vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
                _LOGGER.debug("[ChargePlanner] Multi-vehicle: cable connected, planning for active vehicle %s soc=%.0f%%",
                    self.active_vehicle.get(VEHICLE_NAME, "?"), current_soc)
            else:
                # No cable – plan for the selected active vehicle (Bug 25)
                vehicle = self.active_vehicle or (self._vehicles[0] if self._vehicles else None)
                if vehicle:
                    soc_ent = vehicle.get(VEHICLE_SOC_ENTITY, "")
                    soc_st = self.hass.states.get(soc_ent) if soc_ent else None
                    try:
                        v_soc = float(soc_st.state) if soc_st else None
                    except (ValueError, TypeError):
                        v_soc = None
                    current_soc = v_soc if v_soc is not None else 0.0
                    target_soc = float(self.target_soc) if self.target_soc > 0 else 80.0
                    battery_capacity = float(vehicle.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH))
                    _LOGGER.debug("[ChargePlanner] Multi-vehicle: no cable, planning for active vehicle %s soc=%.0f%%",
                        vehicle.get(VEHICLE_NAME, "?"), current_soc)
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
        # Fix 7: total energy charged this cable session (completed txs + active tx)
        active_tx_energy = self.ocpp.state.energy_kwh if self.ocpp.state.transaction_id is not None else 0.0
        already_charged_kwh = self._session_total_kwh + active_tx_energy
        # Bug 8: when SOC entity doesn't update during charging (e.g. Kia Connect),
        # estimate current SOC from session start SOC + charged energy to avoid
        # underestimating remaining energy needed.
        if self._session_start_soc is not None and already_charged_kwh > 0:
            # Bug 29: shared estimator so the planner and goal check can't drift
            estimated_soc = estimate_soc(
                self._session_start_soc, already_charged_kwh, battery_capacity,
                DEFAULT_CHARGE_EFFICIENCY, current_soc,
            )
            current_soc = min(estimated_soc, target_soc)
            _LOGGER.debug(
                "[ChargePlanner] Estimerad SOC: start=%.1f%% +%.2f kWh → %.1f%%",
                self._session_start_soc, already_charged_kwh, current_soc,
            )
        soc_needed = max(0.0, target_soc - current_soc)
        # NOTE: already_charged_kwh NOT subtracted here – it's factored into estimated current_soc
        energy_needed = max(0.0, (soc_needed / 100.0) * battery_capacity / DEFAULT_CHARGE_EFFICIENCY)

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
        self._rebuild_charge_windows()  # Bug 24: synka charge-windows-sensorn direkt vid planändring

        # Notify if day charging allowed and plan lands in daytime
        if (
            self.allow_day_charging
            and self.charge_plan
            and self.charge_plan.feasible
        ):
            plan_start_local = self.charge_plan.start.astimezone(local_tz)
            plan_end_local   = self.charge_plan.end.astimezone(local_tz)
            if schedule.is_day_time(plan_start_local.time()):
                # Bug 21: undertryck notis om plan-starten redan ligger i det förflutna
                if plan_start_local <= now_local:
                    _LOGGER.debug(
                        "[ChargePlanner] Dag-notis undertryckt – plan_start %s redan passerad (now=%s)",
                        plan_start_local.strftime("%H:%M"),
                        now_local.strftime("%H:%M"),
                    )
                    notify = False
                else:
                    # Only notify if plan changed significantly (new session or start shifted)
                    notify = (
                        prev_plan is None
                        or not prev_plan.feasible
                        or abs((self.charge_plan.start - prev_plan.start).total_seconds()) > 7200  # Bug 21: 2h, was 900s
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

                    # Bug 3 + Bug 17: Only send notification if day is actually cheaper per kWh.
                    # Use avg_price_ore_kwh, not estimated_cost_sek — a partial night plan
                    # (e.g. tomorrow's prices not yet published) has artificially low total
                    # cost simply because it covers fewer kWh.
                    day_is_cheaper = (
                        night_plan is None
                        or not night_plan.feasible
                        or self.charge_plan.avg_price_ore_kwh < night_plan.avg_price_ore_kwh
                    )
                    if not day_is_cheaper:
                        _LOGGER.debug(
                            "[ChargePlanner] Dag-plan (%.1f öre/kWh) inte billigare än natt (%.1f öre/kWh), hoppar över notis",
                            self.charge_plan.avg_price_ore_kwh,
                            night_plan.avg_price_ore_kwh if night_plan else 0,
                        )
                        # Use night plan instead if it's cheaper and feasible
                        if night_plan and night_plan.feasible:
                            self.charge_plan = night_plan
                        self._rebuild_charge_windows()  # Bug 24: synka efter natt-switch
                        return

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

        # ── Presence-based day-charging offer ─────────────────────────────────
        # When day charging is OFF (weekday auto-schedule) but someone/the car is
        # home after 09:00, offer day charging *if* a day plan is actually cheaper
        # than the night plan. Sent at most once per calendar day.
        elif not self.allow_day_charging and not self._force_day_plan:
            someone_home = self._someone_home()
            skip_reason = None
            if self._day_charging_dismissed:
                skip_reason = "day-choice dismissed this cable session"
            elif not self.ocpp.state.cable_connected:
                skip_reason = "cable not connected"
            elif energy_needed <= 0:
                skip_reason = "no energy needed"
            elif not (self.charge_plan and self.charge_plan.feasible):
                skip_reason = "no feasible night plan"
            elif now_local.hour < DAY_OFFER_EARLIEST_HOUR:
                skip_reason = f"before {DAY_OFFER_EARLIEST_HOUR:02d}:00"
            elif self._day_offer_notified_date == now_local.date():
                skip_reason = "already offered today"
            elif not someone_home:
                skip_reason = "nobody home"

            if skip_reason is not None:
                _LOGGER.debug(
                    "[ChargePlanner] Day-charging offer skipped: %s (someone_home=%s)",
                    skip_reason, someone_home,
                )
            else:
                # self.charge_plan was built from night-only prices → it is the night plan.
                night_plan = self.charge_plan
                day_plan = plan_cheapest_window(
                    interval_prices=all_prices,
                    energy_needed_kwh=energy_needed,
                    power_kw=power_kw,
                    deadline=deadline_local,
                    now=now_local,
                    schedule_fn=_schedule_fn,
                    voltage=DEFAULT_VOLTAGE,
                    num_phases=self.num_phases,
                    local_tz=local_tz,
                    contiguous=_use_contiguous,
                )
                # Bug 17: compare avg_price_ore_kwh, not estimated_cost_sek. The night
                # plan is often partial early in the day (tomorrow's prices not yet
                # published), so its total cost is artificially low — comparing totals
                # would suppress the offer even when the day is actually cheaper per kWh.
                if (
                    day_plan.feasible
                    and day_plan.avg_price_ore_kwh < night_plan.avg_price_ore_kwh
                ):
                    _LOGGER.info(
                        "[ChargePlanner] Hemma efter %02d:00 och dag (%.1f öre/kWh) billigare än natt (%.1f öre/kWh) – skickar erbjudande",
                        DAY_OFFER_EARLIEST_HOUR,
                        day_plan.avg_price_ore_kwh,
                        night_plan.avg_price_ore_kwh,
                    )
                    self.notifier.on_day_charging_chosen(
                        day_start=day_plan.start.astimezone(local_tz),
                        day_end=day_plan.end.astimezone(local_tz),
                        day_cost=day_plan.estimated_cost_sek,
                        day_avg_ore=day_plan.avg_price_ore_kwh,
                        night_start=night_plan.start.astimezone(local_tz),
                        night_end=night_plan.end.astimezone(local_tz),
                        night_cost=night_plan.estimated_cost_sek,
                        night_avg_ore=night_plan.avg_price_ore_kwh,
                    )
                    self._day_offer_notified_date = now_local.date()
                else:
                    _LOGGER.debug(
                        "[ChargePlanner] Day-charging offer skipped: day plan not cheaper "
                        "(day=%.1f öre/kWh feasible=%s, night=%.1f öre/kWh)",
                        day_plan.avg_price_ore_kwh, day_plan.feasible,
                        night_plan.avg_price_ore_kwh,
                    )

    def _update_price_cap_plan(
        self,
        all_prices: list,
        now_local: datetime,
        local_tz,
    ) -> None:
        """Feature 5: build the charge plan from every slot ≤ the price cap.

        Thin HA-glue wrapper around price_cap.select_price_cap_slots. Respects
        the same deadline and allow_day_charging constraints as the ordinary
        planner; the SoC target is enforced by _charging_goal_reached().
        """
        deadline_local = self._compute_deadline(now_local, local_tz, all_prices)

        # Price unit for öre/kWh conversion (same source as the price sensor).
        unit = ""
        forecast_entity = self.config.get(CONF_PRICE_FORECAST_ENTITY, "")
        if forecast_entity:
            state_obj = self.hass.states.get(forecast_entity)
            if state_obj is not None:
                unit = state_obj.attributes.get("unit_of_measurement", "")

        prices_ore = [
            {"time": iv["time"],
             "ore_kwh": self._to_ore_per_kwh(float(iv["value"]), unit)}
            for iv in all_prices
        ]

        # Schedule-aware power per slot, capped by the active vehicle's max
        # current (identical to the ordinary planner).
        schedule = self.schedule
        vehicle_max_a = int((self.active_vehicle or {}).get(VEHICLE_MAX_CURRENT_A, 0))

        def _power_fn(local_dt: datetime) -> float:
            limit_a = schedule.current_limit_at(local_dt)
            if vehicle_max_a > 0:
                limit_a = min(limit_a, vehicle_max_a)
            return (limit_a * DEFAULT_VOLTAGE * self.num_phases) / 1000.0

        def _is_day_fn(local_dt: datetime) -> bool:
            return schedule.is_day_time(local_dt.time())

        result = select_price_cap_slots(
            prices_ore,
            self.price_cap_ore_kwh,
            now_local,
            deadline_local,
            power_fn=_power_fn,
            is_day_fn=_is_day_fn,
            allow_day_charging=self.allow_day_charging,
            local_tz=local_tz,
        )

        self._price_cap_intervals = result.active_intervals
        self._price_cap_raw_slots = result.qualifying_slots

        deadline_hhmm = deadline_local.astimezone(local_tz).strftime("%H:%M")
        if result.feasible:
            slots = result.qualifying_slots

            # Bug 35c: cap energy_kwh/estimated_cost_sek against the remaining
            # battery need so PlannedChargeEnergy/EstimatedChargeCost reflect
            # what will actually be charged, not the whole qualifying market.
            # slots carry AC-side energy, so divide the battery need by the
            # charge efficiency. None (unknown SoC, or already at target) =>
            # no capping, same as before.
            current_soc = self.ocpp.state.soc_percent
            target_soc = self.target_soc
            capacity = self.battery_capacity_kwh
            if (
                current_soc is not None
                and target_soc > 0
                and capacity > 0
                and target_soc > current_soc
            ):
                energy_needed_kwh = (
                    (target_soc - current_soc) / 100.0 * capacity / DEFAULT_CHARGE_EFFICIENCY
                )
            else:
                energy_needed_kwh = None

            capped_kwh = 0.0
            capped_cost = 0.0
            capped_slots = []
            for s in slots:
                if energy_needed_kwh is not None and capped_kwh >= energy_needed_kwh:
                    break
                capped_kwh += s["energy_kwh"]
                capped_cost += s["price_kwh"] * s["energy_kwh"]
                capped_slots.append(s)

            first_t = slots[0]["time"]
            if capped_slots:
                last_t = capped_slots[-1]["time"] + timedelta(minutes=INTERVAL_MINUTES)
            else:
                last_t = slots[-1]["time"] + timedelta(minutes=INTERVAL_MINUTES)
            active_minutes = sum(
                int((e - s).total_seconds() / 60) for s, e in result.active_intervals
            )
            self.charge_plan = ChargePlan(
                start=first_t,
                end=last_t,
                duration_minutes=active_minutes,
                energy_kwh=round(capped_kwh, 2),
                estimated_cost_sek=round(capped_cost, 2),
                avg_price_ore_kwh=round(result.avg_price_ore_kwh, 1),
                intervals=[
                    {
                        "time": s["time"].isoformat(),
                        "price_ore_kwh": round(s["price_kwh"] * 100, 2),
                        "power_kw": round(s["energy_kwh"] / INTERVAL_HOURS, 2),
                        "energy_kwh": round(s["energy_kwh"], 4),
                    }
                    for s in slots  # keep all slots in the schedule list
                ],
                active_intervals=result.active_intervals,
                feasible=True,
                message=(
                    f"Price cap {self.price_cap_ore_kwh:.0f} öre/kWh – "
                    f"{len(slots)} slots (deadline {deadline_hhmm})"
                ),
            )
            _LOGGER.info(
                "[PriceCap] %d slots ≤ %.0f öre/kWh → %.2f kWh / %.2f SEK "
                "(cappat mot SoC-mål: %.2f kWh / %.2f SEK, deadline %s, allow_day=%s)",
                len(slots), self.price_cap_ore_kwh, result.total_kwh,
                result.total_cost_sek, capped_kwh, capped_cost,
                deadline_hhmm, self.allow_day_charging,
            )
        else:
            self.charge_plan = None
            _LOGGER.info(
                "[PriceCap] Inga slots ≤ %.0f öre/kWh inom deadline %s "
                "(allow_day=%s) – laddning pausad",
                self.price_cap_ore_kwh, deadline_hhmm, self.allow_day_charging,
            )

        self._rebuild_charge_windows()  # Bug 24: sync charge-windows sensor immediately

    def _rebuild_charge_windows(self) -> None:
        """Feature 3: rebuild _charge_windows from the current charge_plan.

        Only rebuilds when charge_plan is a new object (identity guard), so
        `calculated_at` reflects the actual replan time rather than every
        ~10 s update cycle.  actual_energy_kwh is preserved across rebuilds by
        build_charge_windows() (matched on slot start-ISO).
        """
        plan = self.charge_plan
        if not plan or not plan.feasible:
            return
        if plan is self._charge_windows_plan_ref:
            return  # unchanged since last rebuild
        self._charge_windows_plan_ref = plan

        import zoneinfo
        try:
            local_tz = zoneinfo.ZoneInfo(self.hass.config.time_zone)
        except Exception:
            local_tz = timezone.utc

        self._charge_windows = build_charge_windows(
            plan.active_intervals,
            plan.intervals,
            self._charge_windows,
            datetime.now(timezone.utc),
            local_tz,
        )

        vehicle_name = (
            self.active_vehicle.get(VEHICLE_NAME, "Unknown")
            if self.active_vehicle else "Unknown"
        )
        self._charge_windows_meta = {
            "calculated_at": datetime.now(local_tz).isoformat(),
            "vehicle": vehicle_name,
            "soc_percent": self.ocpp.state.soc_percent,
            "charge_mode": self.charge_mode,
            "planned_energy_kwh": plan.energy_kwh,
            "estimated_cost_sek": plan.estimated_cost_sek,
            "avg_price_ore_kwh": plan.avg_price_ore_kwh,
            "feasible": plan.feasible,
            "partial": plan.partial,
        }
        _LOGGER.debug(
            "[ChargeWindows] Rebuilt %d slots vehicle=%s soc=%s",
            len(self._charge_windows), vehicle_name, self.ocpp.state.soc_percent,
        )

    def _update_charge_windows_actual(self) -> None:
        """Feature 3: fill actual_energy_kwh for completed slots via cable-session
        cumulative energy. Cheap; safe to call every update cycle."""
        if not self._charge_windows:
            return
        current_cumulative = self._cable_session_energy_kwh + (
            self.ocpp.state.energy_kwh
            if self.ocpp.state.transaction_id is not None else 0.0
        )
        update_windows_actual(
            self._charge_windows,
            self._charge_windows_energy_at_slot_start,
            current_cumulative,
            datetime.now(timezone.utc),
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
            # Bug 12: Guard mot omstart/stale state
            if not self.ocpp.state.cable_connected:
                _LOGGER.debug("[Notify] _delayed: cable ej ansluten, avbryter stopp-notis")
                return
            if self.ocpp.state.charging:
                _LOGGER.debug("[Notify] _delayed: laddning aktiv, avbryter stopp-notis")
                return
            if energy_kwh < 0.1:
                _LOGGER.debug("[Notify] _delayed: energy_kwh=%.3f för lågt, avbryter stopp-notis", energy_kwh)
                return

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
