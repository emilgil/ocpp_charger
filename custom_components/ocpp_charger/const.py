"""Constants for the OCPP EV Charger integration."""

DOMAIN = "ocpp_charger"
DEFAULT_PORT = 9000
DEFAULT_MQTT_PREFIX = "ocpp"
DEFAULT_MAX_CURRENT = 16
DEFAULT_NUM_PHASES = 3
DEFAULT_VOLTAGE = 230  # Volt per phase

# OCPP Status
OCPP_STATUS_AVAILABLE = "Available"
OCPP_STATUS_PREPARING = "Preparing"
OCPP_STATUS_CHARGING = "Charging"
OCPP_STATUS_SUSPENDED_EV = "SuspendedEV"
OCPP_STATUS_SUSPENDED_EVSE = "SuspendedEVSE"
OCPP_STATUS_FINISHING = "Finishing"
OCPP_STATUS_RESERVED = "Reserved"
OCPP_STATUS_UNAVAILABLE = "Unavailable"
OCPP_STATUS_FAULTED = "Faulted"

CABLE_CONNECTED_STATUSES = {
    OCPP_STATUS_PREPARING,
    OCPP_STATUS_CHARGING,
    OCPP_STATUS_SUSPENDED_EV,
    OCPP_STATUS_SUSPENDED_EVSE,
    OCPP_STATUS_FINISHING,
}

# Config keys
CONF_HOST = "host"
CONF_PORT = "port"
CONF_CHARGER_ID = "charger_id"
CONF_MQTT_TOPIC_PREFIX = "mqtt_topic_prefix"
CONF_ELECTRICITY_PRICE_ENTITY = "electricity_price_entity"
CONF_SOC_ENTITY = "soc_entity"
CONF_BATTERY_CAPACITY = "battery_capacity_kwh"

DEFAULT_BATTERY_CAPACITY_KWH = 64.0
DEFAULT_CHARGE_EFFICIENCY = 0.92  # 92% AC-to-battery efficiency
CONF_MAX_CURRENT = "max_current"
CONF_NUM_PHASES = "num_phases"

# Sensor unique id suffixes
SENSOR_STATUS = "status"
SENSOR_POWER = "power"
SENSOR_CURRENT = "current"
SENSOR_ENERGY = "energy"
SENSOR_SOC = "soc"
SENSOR_CABLE = "cable_connected"
SENSOR_ELAPSED = "elapsed_time"
SENSOR_ETA = "estimated_completion"
SENSOR_PRICE = "current_price"
SENSOR_SESSION_ID = "session_id"

# Number unique id suffixes
NUMBER_MAX_CURRENT = "max_current_limit"
NUMBER_TARGET_SOC = "target_soc"
NUMBER_TARGET_KWH = "target_kwh"
NUMBER_BATTERY_CAPACITY = "battery_capacity"

# Button unique id suffixes
BUTTON_START = "start_charging"
BUTTON_STOP = "stop_charging"

# Select unique id suffixes
SELECT_CHARGE_MODE = "charge_mode"

# Charge modes
CHARGE_MODE_IMMEDIATE = "Immediate"
CHARGE_MODE_SMART = "Smart (price-optimised)"
CHARGE_MODE_SCHEDULED = "Scheduled"

CHARGE_MODES = [
    CHARGE_MODE_IMMEDIATE,
    CHARGE_MODE_SMART,
    CHARGE_MODE_SCHEDULED,
]

# MQTT topics (relative to prefix/charger_id)
MQTT_STATUS_TOPIC = "status"
MQTT_METER_TOPIC = "meter"
MQTT_SOC_TOPIC = "soc"
MQTT_COMMAND_TOPIC = "command"
MQTT_RESPONSE_TOPIC = "response"

# Update intervals
SCAN_INTERVAL_SECONDS = 10
PRICE_UPDATE_INTERVAL = 300  # seconds

# Smart charging: only charge if price below this percentile of 24h forecast
SMART_CHARGE_PRICE_THRESHOLD_PERCENTILE = 0.4

# Vehicle registry
CONF_VEHICLES = "vehicles"          # list of vehicle dicts stored in config entry
CONF_ACTIVE_VEHICLE = "active_vehicle"  # index into CONF_VEHICLES list

# Keys inside each vehicle dict
VEHICLE_NAME = "name"
VEHICLE_CAPACITY = "capacity_kwh"
VEHICLE_SOC_ENTITY = "soc_entity"

SELECT_ACTIVE_VEHICLE = "active_vehicle"

# Auto vehicle detection
CONF_AUTO_VEHICLE_DETECTION = "auto_vehicle_detection"
AUTO_DETECT_SOC_TOLERANCE = 5.0   # % – OCPP SOC måste vara inom ±5% av entitetsvärde
SWITCH_AUTO_VEHICLE = "auto_vehicle_detection"

# "Ad hoc" vehicle (nameless, capacity-only)
ADHOC_VEHICLE_NAME = "New Vehicle"

# Current schedule (day/night)
CONF_SCHEDULE_DAY_START   = "schedule_day_start"    # "HH:MM"
CONF_SCHEDULE_NIGHT_START = "schedule_night_start"  # "HH:MM"
CONF_SCHEDULE_DAY_CURRENT   = "schedule_day_current_a"
CONF_SCHEDULE_NIGHT_CURRENT = "schedule_night_current_a"

DEFAULT_SCHEDULE_DAY_START   = "06:00"
DEFAULT_SCHEDULE_NIGHT_START = "22:00"
DEFAULT_SCHEDULE_DAY_CURRENT   = 6    # A
DEFAULT_SCHEDULE_NIGHT_CURRENT = 16   # A

SWITCH_SCHEDULE_OVERRIDE = "schedule_override"
NUMBER_OVERRIDE_CURRENT  = "override_current"
SENSOR_SCHEDULE_PERIOD   = "schedule_period"

# REST client
CONF_REST_BASE_URL   = "rest_base_url"
CONF_REST_AUTH_TYPE  = "rest_auth_type"     # "none" | "basic" | "bearer"
CONF_REST_USERNAME   = "rest_username"
CONF_REST_PASSWORD   = "rest_password"
CONF_REST_TOKEN      = "rest_bearer_token"

REST_AUTH_NONE   = "none"
REST_AUTH_BASIC  = "basic"
REST_AUTH_BEARER = "bearer"
REST_AUTH_TYPES  = [REST_AUTH_NONE, REST_AUTH_BASIC, REST_AUTH_BEARER]

# HA service name for REST calls
SERVICE_REST_CALL = "rest_call"

# Charge planner
CONF_PRICE_FORECAST_ENTITY = "price_forecast_entity"
DEFAULT_CHARGE_DEADLINE_HOUR = 6          # imorgon kl 06:00
SENSOR_PLAN_START = "planned_charge_start"
SENSOR_PLAN_END   = "planned_charge_end"

# Planner algorithm
PLANNER_ALGO_GREEDY     = "Greedy (cheapest slots)"
PLANNER_ALGO_CONTIGUOUS = "Contiguous (cheapest block)"
PLANNER_ALGORITHMS = [PLANNER_ALGO_GREEDY, PLANNER_ALGO_CONTIGUOUS]
SELECT_PLANNER_ALGORITHM = "planner_algorithm"

# Notifications
CONF_NOTIFY_ENABLED        = "notify_enabled"
CONF_NOTIFY_TARGET         = "notify_target"
CONF_NOTIFY_ON_CONNECT     = "notify_on_connect"
CONF_NOTIFY_ON_START       = "notify_on_start"
CONF_NOTIFY_ON_STOP        = "notify_on_stop"
DEFAULT_NOTIFY_TARGET      = ""

# SOC entity unit
CONF_SOC_UNIT   = "soc_unit"
VEHICLE_SOC_UNIT = "soc_unit"
SOC_UNIT_PERCENT = "percent"
SOC_UNIT_KWH     = "kwh"
SOC_UNITS        = [SOC_UNIT_PERCENT, SOC_UNIT_KWH]

# Day charging allow switch
SWITCH_ALLOW_DAY_CHARGING  = "allow_day_charging"

# Actionable notification actions
NOTIFY_ACTION_USE_DAY        = "ocpp_use_day_charging"
NOTIFY_ACTION_USE_NIGHT      = "ocpp_use_night_charging"
NOTIFY_ACTION_DISMISS        = "ocpp_dismiss_day_charging"
NOTIFY_ACTION_SELECT_VEHICLE = "ocpp_select_vehicle_"  # prefix; append vehicle index

# Cumulative cost sensor
SENSOR_TOTAL_COST = "total_charging_cost"
