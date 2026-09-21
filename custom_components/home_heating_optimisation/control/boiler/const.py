"""Constants and defaults for Boiler Flow Control (spec v0.1)."""

from __future__ import annotations

DOMAIN = "home_heating_optimisation"
VERSION = "0.3.0"

# Supervision bounds (seconds); see comfort/const.py for the rationale.
ACTUATOR_CALL_TIMEOUT = 30.0
CYCLE_TIMEOUT = 120.0

# ---------------------------------------------------------------------------
# Configured entities (§5) — required
# ---------------------------------------------------------------------------
CONF_FLOW_SETPOINT_ENTITY = "flow_setpoint_entity"  # number.boiler_selflowtemp
CONF_OUTDOOR_TEMP_ENTITY = "outdoor_temp_entity"  # sensor.outdoor_temperature

# Optional entities — each missing one disables the feature that needs it
CONF_CURRENT_FLOW_ENTITY = "current_flow_entity"  # sensor.boiler_curflowtemp
CONF_RETURN_TEMP_ENTITY = "return_temp_entity"  # sensor.boiler_return_temp_temperature
CONF_HEATING_ACTIVE_ENTITY = "heating_active_entity"  # binary_sensor.boiler_heatingactive
CONF_BURNER_POWER_ENTITY = "burner_power_entity"  # sensor.boiler_curburnpow
CONF_HEAT_DEMAND_ENTITY = "heat_demand_entity"  # sensor.heating_demand
CONF_HW_RELAY_DEMAND_ENTITY = "hw_relay_demand_entity"  # sensor.hot_water_relay_demand
CONF_CYLINDER_TEMP_ENTITY = "cylinder_temp_entity"  # sensor.cylinder_temperature
CONF_MAX_FLOW_ENTITY = "max_flow_entity"  # number.boiler_heatingtemp
CONF_ZONE_DEMAND_ENTITIES = "zone_demand_entities"  # sensor.zone_heat_demand (multiple)

OPTIONAL_ENTITY_KEYS = (
    CONF_CURRENT_FLOW_ENTITY,
    CONF_RETURN_TEMP_ENTITY,
    CONF_HEATING_ACTIVE_ENTITY,
    CONF_BURNER_POWER_ENTITY,
    CONF_HEAT_DEMAND_ENTITY,
    CONF_HW_RELAY_DEMAND_ENTITY,
    CONF_CYLINDER_TEMP_ENTITY,
    CONF_MAX_FLOW_ENTITY,
    CONF_ZONE_DEMAND_ENTITIES,
)

# ---------------------------------------------------------------------------
# Options-flow tunables (§3, "remaining tunables")
# ---------------------------------------------------------------------------
CONF_FLOW_MIN = "flow_min"
DEFAULT_FLOW_MIN = 35.0
CONF_FLOW_MAX = "flow_max"
DEFAULT_FLOW_MAX = 65.0
CONF_DHW_FLOW_MIN = "dhw_flow_min"
DEFAULT_DHW_FLOW_MIN = 55.0
CONF_DHW_FLOW_MAX = "dhw_flow_max"
DEFAULT_DHW_FLOW_MAX = 70.0
CONF_DHW_RETURN_CEILING = "dhw_return_ceiling"
DEFAULT_DHW_RETURN_CEILING = 60.0
CONF_MIN_HOLD_MINUTES = "min_hold_minutes"
DEFAULT_MIN_HOLD_MINUTES = 10.0
CONF_MANUAL_HOLD_MINUTES = "manual_hold_minutes"
DEFAULT_MANUAL_HOLD_MINUTES = 30.0

OPTIONS_TUNABLE_KEYS = (
    CONF_FLOW_MIN,
    CONF_FLOW_MAX,
    CONF_DHW_FLOW_MIN,
    CONF_DHW_FLOW_MAX,
    CONF_DHW_RETURN_CEILING,
    CONF_MIN_HOLD_MINUTES,
    CONF_MANUAL_HOLD_MINUTES,
)

# ---------------------------------------------------------------------------
# Number entities (§4) — live tunables, defaults from §3
# ---------------------------------------------------------------------------
CONF_DESIGN_FLOW = "design_flow"
DEFAULT_DESIGN_FLOW = 55.0
CONF_DESIGN_OUTDOOR = "design_outdoor"
DEFAULT_DESIGN_OUTDOOR = -3.0
CONF_RETURN_CEILING = "return_ceiling"
DEFAULT_RETURN_CEILING = 50.0
CONF_DHW_DELTA = "dhw_delta"
DEFAULT_DHW_DELTA = 20.0

ROOM_DESIGN_TEMP = 20.0  # fixed per §3.2.1; not user-configurable in phase 1

# ---------------------------------------------------------------------------
# Mode override select (§4)
# ---------------------------------------------------------------------------
OVERRIDE_AUTO = "auto"
OVERRIDE_SHADOW = "shadow"
OVERRIDE_HOLD = "hold"
DEFAULT_OVERRIDE = OVERRIDE_SHADOW

# ---------------------------------------------------------------------------
# Cycle interval and freshness
# ---------------------------------------------------------------------------
UPDATE_INTERVAL_SECONDS = 60
RETURN_FRESHNESS_MINUTES = 10.0
CYCLING_WINDOW_MINUTES = 10.0
CYCLING_TOGGLE_THRESHOLD = 3

# v0.3 completion, input validity and diagnostic feedback.
CONF_CYLINDER_TARGET_ENTITY = "cylinder_target_entity"
CONF_ROOM_CLIMATE_ENTITIES = "room_climate_entities"
CONF_BOILER_RELAY_ENTITY = "boiler_relay_entity"
CONF_DHW_FALLBACK_FLOW = "dhw_fallback_flow"
CONF_DHW_TARGET = "dhw_target"
CONF_DHW_PROGRESS_MINUTES = "dhw_progress_minutes"
CONF_DHW_TIMEOUT_MINUTES = "dhw_timeout_minutes"
CONF_INPUT_FRESHNESS_MINUTES = "input_freshness_minutes"
CONF_OUTDOOR_FRESHNESS_MINUTES = "outdoor_freshness_minutes"
DEFAULT_DHW_TARGET = 60.0
DEFAULT_DHW_PROGRESS_MINUTES = 30.0
DEFAULT_DHW_TIMEOUT_MINUTES = 120.0
DEFAULT_INPUT_FRESHNESS_MINUTES = 30.0
DEFAULT_OUTDOOR_FRESHNESS_MINUTES = 120.0
OPTIONAL_ENTITY_KEYS += (
    CONF_CYLINDER_TARGET_ENTITY,
    CONF_ROOM_CLIMATE_ENTITIES,
    CONF_BOILER_RELAY_ENTITY,
)
