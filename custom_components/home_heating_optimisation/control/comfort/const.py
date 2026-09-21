"""Constants, defaults, and room profiles for OT Thermostat Control."""

from __future__ import annotations

DOMAIN = "home_heating_optimisation"

# Supervision bounds (seconds). A wedged actuator service must never hold a
# control cycle, and a wedged cycle must never stop the coordinator scheduling.
ACTUATOR_CALL_TIMEOUT = 30.0
CYCLE_TIMEOUT = 120.0

# Entry types
ENTRY_TYPE_HUB = "hub"
ENTRY_TYPE_ROOM = "room"

# Hub config keys
CONF_GLOBAL_ENABLED = "global_enabled"

# Config keys
CONF_NAME = "name"
CONF_PRIMARY_CLIMATE = "primary_climate"
CONF_BACKUP_CLIMATE = "backup_climate"
CONF_AIR_TEMP_SENSOR = "air_temp_sensor"
CONF_WEATHER_ENTITY = "weather_entity"
CONF_ROOM_PROFILE = "room_profile"
CONF_ORIENTATION = "orientation"
CONF_SOLAR_SENSOR = "solar_sensor"
CONF_OUTDOOR_TEMP_SENSOR = "outdoor_temp_sensor"
CONF_OUTDOOR_HUMIDITY_SENSOR = "outdoor_humidity_sensor"
CONF_WIND_SPEED_SENSOR = "wind_speed_sensor"
CONF_CORRECTION_GAIN = "correction_gain"
CONF_MAX_SETPOINT = "max_setpoint"
CONF_OVERRIDE_DURATION = "override_duration"
CONF_RUN_INTERVAL = "run_interval"
CONF_AUTOMATION_DELAY = "automation_delay"
CONF_COAST_CYCLES = "coast_cycles"
CONF_MAX_STEP = "max_step"
# Smoothing
CONF_SMOOTHING_ENABLED = "smoothing_enabled"

# Enable/disable advanced room diagnostic sensors
CONF_ADVANCED_SENSORS = "advanced_sensors"
DEFAULT_ADVANCED_SENSORS = True
CONF_APPARENT_TEMP_ENTITY = "apparent_temp_entity"
CONF_WEATHER_K_BOOST = "weather_k_boost"
CONF_WEATHER_REF_TEMP = "weather_ref_temp"
CONF_WEATHER_SCALE = "weather_scale"
CONF_WEATHER_SEVERITY_EXPONENT = "weather_severity_exponent"
CONF_K_MAX = "k_max"

# K adaptation mode
CONF_K_ADAPTATION_MODE = "k_adaptation_mode"
CONF_GRADIENT_SCALE = "gradient_scale"
CONF_GRADIENT_EXPONENT = "gradient_exponent"

# K adaptation mode values
K_MODE_WEATHER_ONLY = "weather_only"
K_MODE_OT_REFERENCED = "ot_referenced"

# Occupancy offsets (optional, per-room)
CONF_OCCUPANCY_SENSOR = "occupancy_sensor"
CONF_UNOCCUPIED_DURATION = "unoccupied_duration"
CONF_WEEKDAY_MORNING_OFFSET = "weekday_morning_offset"
CONF_WEEKDAY_AFTERNOON_OFFSET = "weekday_afternoon_offset"
CONF_WEEKDAY_EVENING_OFFSET = "weekday_evening_offset"
CONF_WEEKEND_MORNING_OFFSET = "weekend_morning_offset"
CONF_WEEKEND_AFTERNOON_OFFSET = "weekend_afternoon_offset"
CONF_WEEKEND_EVENING_OFFSET = "weekend_evening_offset"

# Time window restriction (optional, per-room)
CONF_TIME_WINDOW_ENABLED = "time_window_enabled"
CONF_TIME_WINDOW_START = "time_window_start"
CONF_TIME_WINDOW_END = "time_window_end"

# Min setpoint (optional, per-room)
CONF_MIN_SETPOINT = "min_setpoint"

# Effective comfort-policy actuator bounds. Legacy min/max_setpoint keys are not used.
CONF_ZONE_SETPOINT_MIN = "zone_setpoint_min"
CONF_ZONE_SETPOINT_MAX = "zone_setpoint_max"
DEFAULT_ZONE_SETPOINT_MIN = 5.0
DEFAULT_ZONE_SETPOINT_MAX = 35.0

# Hardcoded global entities
ENTITY_AT_HOME_MODE = "input_boolean.at_home_mode"
ENTITY_HOLIDAY_MODE = "input_boolean.holiday_mode"

# Defaults
DEFAULT_CORRECTION_GAIN = 0.6
DEFAULT_MAX_SETPOINT = 22.0
DEFAULT_OVERRIDE_DURATION = 60
DEFAULT_RUN_INTERVAL = 5
DEFAULT_AUTOMATION_DELAY = 5
DEFAULT_COAST_CYCLES = 3.0
COAST_STABLE_CYCLES_THRESHOLD = (
    3  # 3 cycles × 5-min interval = 15 min between each coast decay step
)
DEFAULT_MAX_STEP = 0.5
DEFAULT_SMOOTHING_ENABLED = True
DEFAULT_THERMAL_ALPHA = 0.3
DEFAULT_MRT_BASELINE_ALPHA = (
    0.02  # Very slow EMA for equilibrium target (~2.5hr half-life at 5min intervals)
)
DEFAULT_WEATHER_K_BOOST = 0.6
DEFAULT_WEATHER_REF_TEMP = 10.0
DEFAULT_WEATHER_SCALE = 20.0
DEFAULT_WEATHER_SEVERITY_EXPONENT = 1.5
DEFAULT_K_MAX = 1.3
DEFAULT_K_ADAPTATION_MODE = "weather_only"
DEFAULT_GRADIENT_SCALE = 15.0
DEFAULT_GRADIENT_EXPONENT = 1.5
DEFAULT_ROOM_PROFILE = "one_wall_large_window"
DEFAULT_ORIENTATION = "S"

# Occupancy defaults
DEFAULT_UNOCCUPIED_DURATION = 5  # minutes
DEFAULT_WEEKDAY_MORNING_OFFSET = -0.5
DEFAULT_WEEKDAY_AFTERNOON_OFFSET = -0.5
DEFAULT_WEEKDAY_EVENING_OFFSET = -0.5
DEFAULT_WEEKEND_MORNING_OFFSET = -0.5
DEFAULT_WEEKEND_AFTERNOON_OFFSET = -0.5
DEFAULT_WEEKEND_EVENING_OFFSET = -0.5

# Time window defaults
DEFAULT_TIME_WINDOW_ENABLED = False
DEFAULT_TIME_WINDOW_START = "06:30:00"
DEFAULT_TIME_WINDOW_END = "22:30:00"

# Min setpoint default
DEFAULT_MIN_SETPOINT = 10.0

# Window/door override (optional, per-room)
CONF_WINDOW_SENSORS = "window_sensors"
CONF_WINDOW_SETPOINT = "window_setpoint"
CONF_WINDOW_DELAY = "window_delay"
CONF_WINDOW_OPEN_DELAY = "window_open_delay"

# Adjacent room impact sensors (optional, per-room)
CONF_ADJACENT_SENSORS = "adjacent_sensors"

DEFAULT_WINDOW_SETPOINT = 10.0  # °C — frost/setback setpoint
DEFAULT_WINDOW_DELAY = 15  # minutes — re-enable delay after close
DEFAULT_WINDOW_OPEN_DELAY = 5  # minutes — must be open this long before override activates

# Time period boundaries (minutes from midnight)
MORNING_START = 390  # 06:30
MORNING_END = 720  # 12:00
AFTERNOON_END = 990  # 16:30
EVENING_END = 1320  # 22:00

# Room profiles: each has f_out, f_win, k_loss, k_solar
ROOM_PROFILES: dict[str, dict[str, float]] = {
    "one_wall_large_window": {"f_out": 0.5, "f_win": 0.40, "k_loss": 0.14, "k_solar": 1.20},
    "two_wall_large_window": {"f_out": 0.8, "f_win": 0.50, "k_loss": 0.16, "k_solar": 1.40},
    "attic": {"f_out": 0.9, "f_win": 0.40, "k_loss": 0.20, "k_solar": 1.50},
    "topfloor_vert_small_window": {"f_out": 0.9, "f_win": 0.15, "k_loss": 0.23, "k_solar": 0.75},
    "topfloor_vert_medium_window": {"f_out": 0.9, "f_win": 0.30, "k_loss": 0.22, "k_solar": 1.00},
    "topfloor_two_walls_cavity": {"f_out": 0.95, "f_win": 0.25, "k_loss": 0.24, "k_solar": 0.95},
    "topfloor_cold_adjacent": {"f_out": 0.95, "f_win": 0.35, "k_loss": 0.23, "k_solar": 1.15},
    "two_wall_small_window": {"f_out": 0.7, "f_win": 0.30, "k_loss": 0.16, "k_solar": 1.00},
    "one_wall_small_window": {"f_out": 0.5, "f_win": 0.20, "k_loss": 0.12, "k_solar": 0.80},
    "basement": {"f_out": 0.4, "f_win": 0.20, "k_loss": 0.10, "k_solar": 0.60},
    "one_wall_cold_adjacent": {"f_out": 0.6, "f_win": 0.30, "k_loss": 0.18, "k_solar": 0.80},
    "corner_cold_adjacent": {"f_out": 0.8, "f_win": 0.40, "k_loss": 0.20, "k_solar": 1.00},
    "interior": {"f_out": 0.0, "f_win": 0.00, "k_loss": 0.08, "k_solar": 0.40},
    "interior_cold_adjacent": {"f_out": 0.3, "f_win": 0.00, "k_loss": 0.12, "k_solar": 0.40},
    "one_wall_medium_window": {"f_out": 0.5, "f_win": 0.30, "k_loss": 0.13, "k_solar": 1.00},
    "two_wall_medium_window": {"f_out": 0.8, "f_win": 0.35, "k_loss": 0.17, "k_solar": 1.10},
    "conservatory": {"f_out": 1.0, "f_win": 0.80, "k_loss": 0.28, "k_solar": 1.80},
    "extension_large_glazing": {"f_out": 0.7, "f_win": 0.60, "k_loss": 0.20, "k_solar": 1.50},
    "three_wall_large_window": {"f_out": 0.95, "f_win": 0.45, "k_loss": 0.22, "k_solar": 1.30},
    "three_wall_two_large_windows": {"f_out": 0.95, "f_win": 0.60, "k_loss": 0.24, "k_solar": 1.60},
}

# Orientation azimuth map (degrees from north)
ORIENTATION_AZIMUTHS: dict[str, float] = {
    "N": 0,
    "NE": 45,
    "E": 90,
    "SE": 135,
    "S": 180,
    "SW": 225,
    "W": 270,
    "NW": 315,
}

# Heat transfer coefficients W/(m^2*K)
H_R = 4.7  # radiant heat transfer coefficient
H_C_STILL = 3.1  # convective, still air

# ---------------------------------------------------------------------------
# v2 configuration keys (design note §10)
# ---------------------------------------------------------------------------
CONF_HOUSE_DIR = "house_dir"  # hub: optional override of the survey directory
DEFAULT_HOUSE_DIR = "house"  # bundled: custom_components/ot_thermostat_control/house/
CONF_ROOM_FILE = "room_file"  # room: override of <house_dir>/rooms/<area>.yaml
CONF_ROOM_ID = "room_id"  # room: survey room id (defaults from name)
CONF_MODE = "mode"  # room: shadow | active
MODE_SHADOW = "shadow"
MODE_ACTIVE = "active"
DEFAULT_MODE = MODE_SHADOW
CONF_TRUST_K = "trust_k"
DEFAULT_TRUST_K = 0.8
CONF_CAP_UP = "cap_up"
CONF_CAP_DOWN = "cap_down"
DEFAULT_CAP = 1.5
CONF_MANUAL_HOLD = "manual_hold_minutes"
DEFAULT_MANUAL_HOLD = 120
CONF_PREHEAT_RELEASE = "preheat_release_minutes"
DEFAULT_PREHEAT_RELEASE = 60
CONF_ASYMMETRY_ENABLED = "asymmetry_enabled"
CONF_IRRADIANCE_SENSOR = "irradiance_sensor"
CONF_FLOW_TEMP_ENTITY = "flow_temp_entity"
CONF_DHW_ACTIVE_ENTITY = "dhw_active_entity"
CONF_MANUAL_FLOW_TEMP = "manual_flow_temp"
DEFAULT_MANUAL_FLOW_TEMP = 50.0
CONF_ADAPTIVE_ENABLED = "adaptive_enabled"
DEFAULT_ADAPTIVE_ENABLED = False  # retired: old entries are ignored by the control path
CONF_ADAPTIVE_SLOPE = "adaptive_slope"
DEFAULT_ADAPTIVE_SLOPE = 0.05
CONF_ADAPTIVE_REF = "adaptive_reference"
DEFAULT_ADAPTIVE_REF = 10.0
CONF_GROUND_TEMP = "ground_temp"
DEFAULT_GROUND_TEMP = 10.0
THERMOSTAT_STEP = 0.1  # ramses accepts 0.1 °C setpoints (v1 wrote 0.1 steps for months)
SCHEDULE_FETCH_INTERVAL_H = 24  # how often to pull the zone schedule over RF for offline use
OUTDOOR_CACHE_MAX_AGE_H = 6  # how long a cached outdoor temperature may stand in for a dead source
