"""Config flow for OCPP EV Charger – with multi-step vehicle registration."""
from __future__ import annotations

import copy
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv
from homeassistant.helpers.selector import (
    NumberSelector,
    NumberSelectorConfig,
    NumberSelectorMode,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    CONF_BATTERY_CAPACITY,
    CONF_CHARGER_ID,
    CONF_ELECTRICITY_PRICE_ENTITY,
    CONF_HOST,
    CONF_MAX_CURRENT,
    CONF_MQTT_TOPIC_PREFIX,
    CONF_PRICE_FORECAST_ENTITY,
    CONF_NOTIFY_ENABLED,
    CONF_NOTIFY_TARGET,
    CONF_NOTIFY_ON_CONNECT,
    CONF_NOTIFY_ON_START,
    CONF_NOTIFY_ON_STOP,
    CONF_REST_AUTH_TYPE,
    CONF_REST_BASE_URL,
    CONF_REST_PASSWORD,
    CONF_REST_TOKEN,
    CONF_REST_USERNAME,
    REST_AUTH_BASIC,
    REST_AUTH_BEARER,
    REST_AUTH_NONE,
    REST_AUTH_TYPES,
    CONF_NUM_PHASES,
    CONF_PORT,
    CONF_SCHEDULE_DAY_CURRENT,
    CONF_SCHEDULE_DAY_START,
    CONF_SCHEDULE_NIGHT_CURRENT,
    CONF_SCHEDULE_NIGHT_START,
    CONF_SOC_ENTITY,
    CONF_SOC_UNIT,
    VEHICLE_SOC_UNIT,
    VEHICLE_MAX_CURRENT_A,
    SOC_UNITS,
    SOC_UNIT_PERCENT,
    CONF_VEHICLES,
    DEFAULT_BATTERY_CAPACITY_KWH,
    DEFAULT_MAX_CURRENT,
    DEFAULT_MQTT_PREFIX,
    DEFAULT_NUM_PHASES,
    DEFAULT_PORT,
    DEFAULT_SCHEDULE_DAY_CURRENT,
    DEFAULT_SCHEDULE_DAY_START,
    DEFAULT_SCHEDULE_NIGHT_CURRENT,
    DEFAULT_SCHEDULE_NIGHT_START,
    DOMAIN,
    VEHICLE_CAPACITY,
    VEHICLE_NAME,
    VEHICLE_SOC_ENTITY,
)

_LOGGER = logging.getLogger(__name__)

# Sentinel shown in the "add another car?" select
_ADD_ANOTHER = "➕ Add another vehicle"
_DONE        = "✅ Done – save configuration"


# No outbound connection test needed – HA acts as the OCPP server.
# The charger connects to HA, not the other way around.


def _get_ha_ip(hass) -> str:
    """Return HA's best-guess local IP address for use as default in the config form."""
    import socket
    try:
        # Use HA's network helper if available (2023.6+)
        from homeassistant.components.network import async_get_adapters
        # Fall through to socket method if not yet loaded
    except ImportError:
        pass

    try:
        # Reliable cross-platform trick: open a UDP socket to a public IP
        # (no actual data is sent) to determine the outbound interface IP.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except Exception:
        return ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _vehicle_label(v: dict) -> str:
    """Return display label 'Name – XX kWh'."""
    return f"{v[VEHICLE_NAME]} – {v[VEHICLE_CAPACITY]} kWh"


def _vehicle_schema(defaults: dict | None = None) -> vol.Schema:
    d = defaults or {}
    return vol.Schema({
        vol.Required(VEHICLE_NAME, default=d.get(VEHICLE_NAME, "")): str,
        vol.Required(
            VEHICLE_CAPACITY, default=d.get(VEHICLE_CAPACITY, DEFAULT_BATTERY_CAPACITY_KWH)
        ): NumberSelector(
            NumberSelectorConfig(min=5.0, max=200.0, step=0.5,
                unit_of_measurement="kWh", mode=NumberSelectorMode.BOX)),
        vol.Optional(VEHICLE_SOC_ENTITY, default=d.get(VEHICLE_SOC_ENTITY, "")): str,
        vol.Optional(VEHICLE_SOC_UNIT, default=d.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT)): vol.In(SOC_UNITS),
        vol.Optional(VEHICLE_MAX_CURRENT_A, default=d.get(VEHICLE_MAX_CURRENT_A, 0)): NumberSelector(
            NumberSelectorConfig(min=0, max=32, step=1,
                unit_of_measurement="A", mode=NumberSelectorMode.BOX)),
    })


# ---------------------------------------------------------------------------
# Config flow
# ---------------------------------------------------------------------------

class OCPPChargerConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """
    Steps:
      user        → charger connection details
      add_vehicle → register one vehicle (loops back via 'add_another')
    """
    VERSION = 1

    def __init__(self) -> None:
        self._data: dict[str, Any] = {}
        self._vehicles: list[dict] = []

    # ── Step 1: charger details ──────────────────────────────────────────
    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            host       = user_input[CONF_HOST]
            port       = user_input[CONF_PORT]
            charger_id = user_input[CONF_CHARGER_ID]

            await self.async_set_unique_id(f"{host}:{port}:{charger_id}")
            self._abort_if_unique_id_configured()

            self._data = user_input
            return await self.async_step_add_vehicle()

        # Try to suggest HA's own IP address as default
        ha_ip = _get_ha_ip(self.hass)

        schema = vol.Schema({
            vol.Required(CONF_HOST, default=ha_ip): str,
            vol.Required(CONF_PORT, default=DEFAULT_PORT): cv.port,
            vol.Required(CONF_CHARGER_ID, default="CP001"): str,
            vol.Optional(CONF_MQTT_TOPIC_PREFIX, default=DEFAULT_MQTT_PREFIX): str,
            vol.Optional(
                CONF_ELECTRICITY_PRICE_ENTITY,
                default="",
            ): str,
            vol.Optional(CONF_MAX_CURRENT, default=DEFAULT_MAX_CURRENT): NumberSelector(
                NumberSelectorConfig(min=6, max=32, step=1, unit_of_measurement="A",
                                     mode=NumberSelectorMode.BOX)
            ),
            vol.Optional(CONF_NUM_PHASES, default=str(DEFAULT_NUM_PHASES)): SelectSelector(
                SelectSelectorConfig(options=["1", "3"], mode=SelectSelectorMode.LIST,
                                     translation_key="num_phases")
            ),
        })

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    # ── Step 2: add vehicle (loops) ──────────────────────────────────────
    async def async_step_add_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}

        if user_input is not None:
            name = user_input.get(VEHICLE_NAME, "").strip()
            if not name:
                errors[VEHICLE_NAME] = "vehicle_name_required"
            else:
                vehicle = {
                    VEHICLE_NAME:         name,
                    VEHICLE_CAPACITY:     float(user_input[VEHICLE_CAPACITY]),
                    VEHICLE_SOC_ENTITY:   user_input.get(VEHICLE_SOC_ENTITY, "").strip(),
                    VEHICLE_SOC_UNIT:     user_input.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT),
                    VEHICLE_MAX_CURRENT_A: int(user_input.get(VEHICLE_MAX_CURRENT_A, 0)),
                }
                self._vehicles.append(vehicle)

                if user_input.get("add_another") == _ADD_ANOTHER:
                    # Loop: show form again for next vehicle
                    return await self.async_step_add_vehicle()
                else:
                    # Done – proceed to schedule config
                    return await self.async_step_schedule()

        # Build list of already-added vehicles to show as context
        added = "\n".join(f"  • {_vehicle_label(v)}" for v in self._vehicles)
        description = (
            f"Registered vehicles:\n{added}\n" if self._vehicles
            else "No vehicle registered yet.\n"
        )

        schema = _vehicle_schema().extend({
            vol.Required("add_another", default=_DONE): vol.In([_ADD_ANOTHER, _DONE]),
        })

        return self.async_show_form(
            step_id="add_vehicle",
            data_schema=schema,
            errors=errors,
            description_placeholders={"added_vehicles": description},
        )

    # ── Step 3: schedule ────────────────────────────────────────────────
    async def async_step_schedule(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._data.update(user_input)
            return await self.async_step_initial_notify()

        schema = vol.Schema({
            vol.Optional(CONF_SCHEDULE_DAY_START,
                         default=DEFAULT_SCHEDULE_DAY_START): str,
            vol.Optional(CONF_SCHEDULE_NIGHT_START,
                         default=DEFAULT_SCHEDULE_NIGHT_START): str,
            vol.Optional(CONF_SCHEDULE_DAY_CURRENT,
                         default=DEFAULT_SCHEDULE_DAY_CURRENT): NumberSelector(
                             NumberSelectorConfig(min=6, max=32, step=1,
                                 unit_of_measurement="A", mode=NumberSelectorMode.BOX)),
            vol.Optional(CONF_SCHEDULE_NIGHT_CURRENT,
                         default=DEFAULT_SCHEDULE_NIGHT_CURRENT): NumberSelector(
                             NumberSelectorConfig(min=6, max=32, step=1,
                                 unit_of_measurement="A", mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(
            step_id="schedule", data_schema=schema
        )

    # ── Step 4: notifications & pricing ─────────────────────────────────
    async def async_step_initial_notify(
        self, user_input: dict | None = None
    ) -> FlowResult:
        """Configure push notifications and price forecast entity."""
        if user_input is not None:
            return self._create_entry(extra={
                CONF_PRICE_FORECAST_ENTITY: user_input.get(CONF_PRICE_FORECAST_ENTITY, "").strip(),
                CONF_NOTIFY_ENABLED:    user_input.get(CONF_NOTIFY_ENABLED, False),
                CONF_NOTIFY_TARGET:     user_input.get(CONF_NOTIFY_TARGET, ""),
                CONF_NOTIFY_ON_CONNECT: user_input.get(CONF_NOTIFY_ON_CONNECT, True),
                CONF_NOTIFY_ON_START:   user_input.get(CONF_NOTIFY_ON_START, True),
                CONF_NOTIFY_ON_STOP:    user_input.get(CONF_NOTIFY_ON_STOP, True),
            })

        notify_services = []
        try:
            services = self.hass.services.async_services().get("notify", {})
            notify_services = sorted(
                f"notify.{n}" for n in services
                if n not in ("persistent_notification", "send_message")
            )
        except Exception:
            pass

        schema = vol.Schema({
            vol.Optional(CONF_PRICE_FORECAST_ENTITY, default=""): str,
            vol.Optional(CONF_NOTIFY_ENABLED, default=False): bool,
            vol.Optional(
                CONF_NOTIFY_TARGET,
                default=notify_services[0] if notify_services else "",
            ): vol.In(notify_services) if notify_services else str,
            vol.Optional(CONF_NOTIFY_ON_CONNECT, default=True): bool,
            vol.Optional(CONF_NOTIFY_ON_START,   default=True): bool,
            vol.Optional(CONF_NOTIFY_ON_STOP,    default=True): bool,
        })
        return self.async_show_form(
            step_id="initial_notify",
            data_schema=schema,
        )

    def _create_entry(self, extra: dict | None = None) -> FlowResult:
        data = dict(self._data)
        data[CONF_VEHICLES] = self._vehicles
        if extra:
            data.update(extra)
        charger_id = data[CONF_CHARGER_ID]
        return self.async_create_entry(
            title=f"OCPP EV Charger ({charger_id})",
            data=data,
        )

    # ── Options flow entry point ─────────────────────────────────────────
    @staticmethod
    def async_get_options_flow(config_entry):  # noqa: D401
        return OCPPChargerOptionsFlow(config_entry)


# ---------------------------------------------------------------------------
# Options flow  (Inställningar → Integrationer → Konfigurera)
# ---------------------------------------------------------------------------

class OCPPChargerOptionsFlow(config_entries.OptionsFlow):
    """
    Steps:
      init        → pick action
      edit_vehicle   → edit existing vehicle
      add_vehicle    → add new vehicle
      remove_vehicle → confirm removal
    """

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        self._config_entry = config_entry
        # Work on a deep copy so we don't mutate the live config
        self._schedule_data: dict = {}
        self._mqtt_data: dict = {}
        self._rest_data: dict = {}
        self._planner_data: dict = {}
        self._notify_data: dict = {}
        self._vehicles: list[dict] = copy.deepcopy(
            config_entry.data.get(CONF_VEHICLES, [])
        )
        self._edit_index: int | None = None

    # ── Menu ────────────────────────────────────────────────────────────
    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        if user_input is not None:
            action = user_input["action"]
            if action == "add":
                return await self.async_step_add_vehicle()
            elif action.startswith("edit:"):
                self._edit_index = int(action.split(":")[1])
                return await self.async_step_edit_vehicle()
            elif action.startswith("remove:"):
                self._edit_index = int(action.split(":")[1])
                return await self.async_step_remove_vehicle()
            elif action == "schedule":
                return await self.async_step_edit_schedule()
            elif action == "mqtt":
                return await self.async_step_edit_mqtt()
            elif action == "rest":
                return await self.async_step_edit_rest()
            elif action == "planner":
                return await self.async_step_edit_planner()
            elif action == "notify":
                return await self.async_step_edit_notify()
            elif action == "done":
                return self._save()

        # Build action choices
        choices: dict[str, str] = {}
        for i, v in enumerate(self._vehicles):
            choices[f"edit:{i}"]   = f"✏️  Edit: {_vehicle_label(v)}"
            choices[f"remove:{i}"] = f"🗑️  Remove: {v[VEHICLE_NAME]}"
        choices["add"]      = "➕ Add new vehicle"
        choices["schedule"] = "🕐 Edit charging schedule"
        choices["mqtt"]     = "📡 Edit MQTT topic prefix"
        choices["rest"]     = "🔌 Edit REST API settings"
        choices["planner"]  = "📅 Edit charge planner settings"
        choices["notify"]   = "🔔 Edit notification settings"
        choices["done"]     = "✅ Save and close"

        schema = vol.Schema({vol.Required("action"): vol.In(choices)})
        return self.async_show_form(
            step_id="init",
            data_schema=schema,
            description_placeholders={
                "vehicle_list": "\n".join(
                    f"  {i+1}. {_vehicle_label(v)}" for i, v in enumerate(self._vehicles)
                ) or "  (no vehicles registered)"
            },
        )

    # ── Add ─────────────────────────────────────────────────────────────
    async def async_step_add_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            name = user_input.get(VEHICLE_NAME, "").strip()
            if not name:
                errors[VEHICLE_NAME] = "vehicle_name_required"
            else:
                self._vehicles.append({
                    VEHICLE_NAME:         name,
                    VEHICLE_CAPACITY:     float(user_input[VEHICLE_CAPACITY]),
                    VEHICLE_SOC_ENTITY:   user_input.get(VEHICLE_SOC_ENTITY, "").strip(),
                    VEHICLE_SOC_UNIT:     user_input.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT),
                    VEHICLE_MAX_CURRENT_A: int(user_input.get(VEHICLE_MAX_CURRENT_A, 0)),
                })
                return self._save()

        return self.async_show_form(
            step_id="add_vehicle",
            data_schema=_vehicle_schema(),
            errors=errors,
        )

    # ── Edit ─────────────────────────────────────────────────────────────
    async def async_step_edit_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        idx = self._edit_index

        if user_input is not None:
            name = user_input.get(VEHICLE_NAME, "").strip()
            if not name:
                errors[VEHICLE_NAME] = "vehicle_name_required"
            else:
                self._vehicles[idx] = {
                    VEHICLE_NAME:         name,
                    VEHICLE_CAPACITY:     float(user_input[VEHICLE_CAPACITY]),
                    VEHICLE_SOC_ENTITY:   user_input.get(VEHICLE_SOC_ENTITY, "").strip(),
                    VEHICLE_SOC_UNIT:     user_input.get(VEHICLE_SOC_UNIT, SOC_UNIT_PERCENT),
                    VEHICLE_MAX_CURRENT_A: int(user_input.get(VEHICLE_MAX_CURRENT_A, 0)),
                }
                return self._save()

        return self.async_show_form(
            step_id="edit_vehicle",
            data_schema=_vehicle_schema(defaults=self._vehicles[idx]),
            errors=errors,
        )

    # ── Remove ───────────────────────────────────────────────────────────
    async def async_step_remove_vehicle(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        idx = self._edit_index
        vehicle = self._vehicles[idx]

        if user_input is not None:
            if user_input.get("confirm"):
                self._vehicles.pop(idx)
            return self._save()

        schema = vol.Schema({vol.Required("confirm", default=False): bool})
        return self.async_show_form(
            step_id="remove_vehicle",
            data_schema=schema,
            description_placeholders={"vehicle": _vehicle_label(vehicle)},
        )

    async def async_step_edit_schedule(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._schedule_data = user_input
            return await self.async_step_init()

        cfg = self._config_entry.data
        schema = vol.Schema({
            vol.Optional(CONF_SCHEDULE_DAY_START,
                default=cfg.get(CONF_SCHEDULE_DAY_START, DEFAULT_SCHEDULE_DAY_START)): str,
            vol.Optional(CONF_SCHEDULE_NIGHT_START,
                default=cfg.get(CONF_SCHEDULE_NIGHT_START, DEFAULT_SCHEDULE_NIGHT_START)): str,
            vol.Optional(CONF_SCHEDULE_DAY_CURRENT,
                default=cfg.get(CONF_SCHEDULE_DAY_CURRENT, DEFAULT_SCHEDULE_DAY_CURRENT)): NumberSelector(
                    NumberSelectorConfig(min=6, max=32, step=1,
                        unit_of_measurement="A", mode=NumberSelectorMode.BOX)),
            vol.Optional(CONF_SCHEDULE_NIGHT_CURRENT,
                default=cfg.get(CONF_SCHEDULE_NIGHT_CURRENT, DEFAULT_SCHEDULE_NIGHT_CURRENT)): NumberSelector(
                    NumberSelectorConfig(min=6, max=32, step=1,
                        unit_of_measurement="A", mode=NumberSelectorMode.BOX)),
        })
        return self.async_show_form(step_id="edit_schedule", data_schema=schema)

    async def async_step_edit_mqtt(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._mqtt_data = user_input
            return await self.async_step_init()

        cfg = self._config_entry.data
        schema = vol.Schema({
            vol.Optional(
                CONF_MQTT_TOPIC_PREFIX,
                default=cfg.get(CONF_MQTT_TOPIC_PREFIX, DEFAULT_MQTT_PREFIX),
            ): str,
        })
        return self.async_show_form(step_id="edit_mqtt", data_schema=schema)

    async def async_step_edit_rest(
        self, user_input: dict | None = None
    ) -> FlowResult:
        errors: dict[str, str] = {}
        if user_input is not None:
            auth = user_input.get(CONF_REST_AUTH_TYPE, REST_AUTH_NONE)
            if auth == REST_AUTH_BASIC and not user_input.get(CONF_REST_USERNAME):
                errors[CONF_REST_USERNAME] = "rest_username_required"
            elif auth == REST_AUTH_BEARER and not user_input.get(CONF_REST_TOKEN):
                errors[CONF_REST_TOKEN] = "rest_token_required"
            else:
                self._rest_data = user_input
                return await self.async_step_init()

        cfg = self._config_entry.data
        schema = vol.Schema({
            vol.Optional(CONF_REST_BASE_URL,
                default=cfg.get(CONF_REST_BASE_URL, "")): str,
            vol.Optional(CONF_REST_AUTH_TYPE,
                default=cfg.get(CONF_REST_AUTH_TYPE, REST_AUTH_NONE)): vol.In(REST_AUTH_TYPES),
            vol.Optional(CONF_REST_USERNAME,
                default=cfg.get(CONF_REST_USERNAME, "")): str,
            vol.Optional(CONF_REST_PASSWORD,
                default=cfg.get(CONF_REST_PASSWORD, "")): str,
            vol.Optional(CONF_REST_TOKEN,
                default=cfg.get(CONF_REST_TOKEN, "")): str,
        })
        return self.async_show_form(
            step_id="edit_rest", data_schema=schema, errors=errors
        )

    async def async_step_edit_planner(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            self._planner_data = user_input
            return await self.async_step_init()

        cfg = self._config_entry.data
        schema = vol.Schema({
            vol.Optional(
                CONF_PRICE_FORECAST_ENTITY,
                default=cfg.get(CONF_PRICE_FORECAST_ENTITY, "sensor.gespot_current_price_se3"),
            ): str,
        })
        return self.async_show_form(step_id="edit_planner", data_schema=schema)

    async def async_step_edit_notify(
        self, user_input: dict | None = None
    ) -> FlowResult:
        if user_input is not None:
            send_test = user_input.pop("send_test_notification", False)
            self._notify_data = user_input
            if send_test:
                target = user_input.get(CONF_NOTIFY_TARGET, "")
                if target:
                    try:
                        domain, service = target.split(".", 1)
                        await self.hass.services.async_call(
                            domain, service,
                            {"title": "🔌 OCPP Laddare – Test",
                             "message": "Testnotifikation från OCPP EV Charger. Notiser fungerar!"},
                            blocking=False,
                        )
                    except Exception as err:
                        import logging
                        logging.getLogger(__name__).warning("[Notify] Test notification failed: %s", err)
            return await self.async_step_init()

        cfg = self._config_entry.data

        # Discover available notify services from HA
        notify_services = []
        try:
            services = self.hass.services.async_services().get("notify", {})
            notify_services = sorted(
                f"notify.{name}" for name in services
                if name not in ("persistent_notification", "send_message")
            )
        except Exception:
            pass

        if not notify_services:
            notify_services = ["notify.mobile_app_your_phone"]

        schema = vol.Schema({
            vol.Optional(CONF_NOTIFY_ENABLED,
                default=cfg.get(CONF_NOTIFY_ENABLED, False)): bool,
            vol.Optional(CONF_NOTIFY_TARGET,
                default=cfg.get(CONF_NOTIFY_TARGET, notify_services[0] if notify_services else "")
            ): vol.In(notify_services) if notify_services else str,
            vol.Optional(CONF_NOTIFY_ON_CONNECT,
                default=cfg.get(CONF_NOTIFY_ON_CONNECT, True)): bool,
            vol.Optional(CONF_NOTIFY_ON_START,
                default=cfg.get(CONF_NOTIFY_ON_START, True)): bool,
            vol.Optional(CONF_NOTIFY_ON_STOP,
                default=cfg.get(CONF_NOTIFY_ON_STOP, True)): bool,
            vol.Optional("send_test_notification", default=False): bool,
        })
        return self.async_show_form(step_id="edit_notify", data_schema=schema)

    def _save(self) -> FlowResult:
        """Persist updated vehicle list and schedule back into config entry data."""
        new_data = dict(self._config_entry.data)
        new_data[CONF_VEHICLES] = self._vehicles
        if hasattr(self, "_schedule_data"):
            new_data.update(self._schedule_data)
        if hasattr(self, "_mqtt_data"):
            new_data.update(self._mqtt_data)
        if hasattr(self, "_rest_data"):
            new_data.update(self._rest_data)
        if hasattr(self, "_planner_data"):
            new_data.update(self._planner_data)
        if hasattr(self, "_notify_data"):
            new_data.update(self._notify_data)
        self.hass.config_entries.async_update_entry(
            self._config_entry, data=new_data
        )
        self.hass.async_create_task(
            self.hass.config_entries.async_reload(self._config_entry.entry_id)
        )
        return self.async_create_entry(title="", data={})
