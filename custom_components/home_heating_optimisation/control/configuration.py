"""Standalone control configuration with import-safe editing."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from .boiler.const import (
    DEFAULT_DHW_FLOW_MAX,
    DEFAULT_DHW_FLOW_MIN,
    DEFAULT_DHW_PROGRESS_MINUTES,
    DEFAULT_DHW_RETURN_CEILING,
    DEFAULT_DHW_TARGET,
    DEFAULT_DHW_TIMEOUT_MINUTES,
    DEFAULT_FLOW_MAX,
    DEFAULT_FLOW_MIN,
    DEFAULT_INPUT_FRESHNESS_MINUTES,
    DEFAULT_MANUAL_HOLD_MINUTES,
    DEFAULT_MIN_HOLD_MINUTES,
    DEFAULT_OUTDOOR_FRESHNESS_MINUTES,
)
from .boiler.core.efficiency import CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED, PROFILES
from .comfort.const import (
    DEFAULT_CAP,
    DEFAULT_MANUAL_FLOW_TEMP,
    DEFAULT_MANUAL_HOLD,
    DEFAULT_PREHEAT_RELEASE,
    DEFAULT_TIME_WINDOW_ENABLED,
    DEFAULT_TIME_WINDOW_END,
    DEFAULT_TIME_WINDOW_START,
    DEFAULT_UNOCCUPIED_DURATION,
    DEFAULT_WEEKDAY_AFTERNOON_OFFSET,
    DEFAULT_WEEKDAY_EVENING_OFFSET,
    DEFAULT_WEEKDAY_MORNING_OFFSET,
    DEFAULT_WEEKEND_AFTERNOON_OFFSET,
    DEFAULT_WEEKEND_EVENING_OFFSET,
    DEFAULT_WEEKEND_MORNING_OFFSET,
    DEFAULT_WINDOW_DELAY,
    DEFAULT_WINDOW_OPEN_DELAY,
    DEFAULT_WINDOW_SETPOINT,
    DEFAULT_ZONE_SETPOINT_MAX,
    DEFAULT_ZONE_SETPOINT_MIN,
)

CONTROL_SCHEMA = 1

HUB_DEFAULTS: dict[str, Any] = {
    "global_enabled": True,
    "manual_flow_temp": DEFAULT_MANUAL_FLOW_TEMP,
    "ground_temp": 10.0,
}
BOILER_DEFAULTS: dict[str, Any] = {
    CONF_EFFICIENCY_PROFILE: PROFILE_DISABLED,
    "enabled": True,
    "flow_min": DEFAULT_FLOW_MIN,
    "flow_max": DEFAULT_FLOW_MAX,
    "dhw_flow_min": DEFAULT_DHW_FLOW_MIN,
    "dhw_flow_max": DEFAULT_DHW_FLOW_MAX,
    "dhw_return_ceiling": DEFAULT_DHW_RETURN_CEILING,
    "min_hold_minutes": DEFAULT_MIN_HOLD_MINUTES,
    "manual_hold_minutes": DEFAULT_MANUAL_HOLD_MINUTES,
    "dhw_target": DEFAULT_DHW_TARGET,
    "dhw_progress_minutes": DEFAULT_DHW_PROGRESS_MINUTES,
    "dhw_timeout_minutes": DEFAULT_DHW_TIMEOUT_MINUTES,
    "input_freshness_minutes": DEFAULT_INPUT_FRESHNESS_MINUTES,
    "outdoor_freshness_minutes": DEFAULT_OUTDOOR_FRESHNESS_MINUTES,
    "dhw_fallback_flow": DEFAULT_DHW_FLOW_MAX,
}
ROOM_DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "occupancy_enabled": True,
    "zone_setpoint_min": DEFAULT_ZONE_SETPOINT_MIN,
    "zone_setpoint_max": DEFAULT_ZONE_SETPOINT_MAX,
    "manual_hold_minutes": DEFAULT_MANUAL_HOLD,
    "preheat_release_minutes": DEFAULT_PREHEAT_RELEASE,
    "override_duration": 60,
    "window_open_delay": DEFAULT_WINDOW_OPEN_DELAY,
    "window_delay": DEFAULT_WINDOW_DELAY,
    "window_setpoint": DEFAULT_WINDOW_SETPOINT,
    "time_window_enabled": DEFAULT_TIME_WINDOW_ENABLED,
    "time_window_start": DEFAULT_TIME_WINDOW_START,
    "time_window_end": DEFAULT_TIME_WINDOW_END,
    "unoccupied_duration": DEFAULT_UNOCCUPIED_DURATION,
    "weekday_morning_offset": DEFAULT_WEEKDAY_MORNING_OFFSET,
    "weekday_afternoon_offset": DEFAULT_WEEKDAY_AFTERNOON_OFFSET,
    "weekday_evening_offset": DEFAULT_WEEKDAY_EVENING_OFFSET,
    "weekend_morning_offset": DEFAULT_WEEKEND_MORNING_OFFSET,
    "weekend_afternoon_offset": DEFAULT_WEEKEND_AFTERNOON_OFFSET,
    "weekend_evening_offset": DEFAULT_WEEKEND_EVENING_OFFSET,
}


@dataclass(frozen=True)
class ControlConfigError(ValueError):
    """A user-correctable control configuration error."""

    code: str


def _entity(value: Any, domains: tuple[str, ...], required: bool = False) -> str | None:
    value = str(value or "").strip()
    if not value:
        if required:
            raise ControlConfigError("control_required_source")
        return None
    if "." not in value or value.split(".", 1)[0] not in domains:
        raise ControlConfigError("invalid_source")
    return value


def _number(values: dict[str, Any], key: str, low: float, high: float, default: float) -> float:
    try:
        value = float(values.get(key, default))
    except (TypeError, ValueError) as err:
        raise ControlConfigError("control_invalid_limits") from err
    if not low <= value <= high:
        raise ControlConfigError("control_invalid_limits")
    return value


def _entities(values: dict[str, Any], key: str, domains: tuple[str, ...]) -> list[str]:
    entities = values.get(key) or []
    if not isinstance(entities, (list, tuple)):
        raise ControlConfigError("invalid_source")
    return [_entity(value, domains, required=True) for value in entities]


def _time(values: dict[str, Any], key: str, default: str) -> str:
    parts = str(values.get(key, default)).split(":")
    try:
        numbers = [int(part) for part in parts]
    except ValueError as err:
        raise ControlConfigError("control_invalid_time") from err
    if len(numbers) not in (2, 3):
        raise ControlConfigError("control_invalid_time")
    hour, minute = numbers[:2]
    second = numbers[2] if len(numbers) == 3 else 0
    if not (0 <= hour <= 23 and 0 <= minute <= 59 and 0 <= second <= 59):
        raise ControlConfigError("control_invalid_time")
    return f"{hour:02}:{minute:02}:{second:02}"


def _room_defaults(room: dict[str, Any], observation: dict[str, Any]) -> dict[str, Any]:
    survey_room = observation.get("survey_rooms", {}).get(room["id"], room["id"])
    return {
        "config": {
            "name": room["name"],
            "room_id": survey_room,
            "primary_climate": room["climate"],
            "air_temp_sensor": room.get("air_sensor"),
            "mode": "shadow",
            **{
                key: value
                for key, value in ROOM_DEFAULTS.items()
                if key not in ("enabled", "occupancy_enabled")
            },
        },
        "seed": {"cap_up": DEFAULT_CAP, "cap_down": DEFAULT_CAP},
        "enabled": True,
        "occupancy_enabled": True,
    }


def new_control(observation: dict[str, Any]) -> dict[str, Any]:
    """Build a complete shadow control without consulting legacy integrations."""
    demands = [room.get("demand_sensor") for room in observation.get("rooms", [])]
    boiler_config = {
        key: value
        for key, value in {
            "outdoor_temp_entity": observation.get("outdoor_temperature"),
            "current_flow_entity": observation.get("flow_temperature"),
            "return_temp_entity": observation.get("return_temperature"),
            "heating_active_entity": observation.get("heating_active"),
            "zone_demand_entities": [value for value in demands if value],
            "room_climate_entities": [room["climate"] for room in observation.get("rooms", [])],
            **{key: value for key, value in BOILER_DEFAULTS.items() if key != "enabled"},
        }.items()
        if value not in (None, [], "")
    }
    return {
        "schema": CONTROL_SCHEMA,
        "origin": "standalone",
        "hub": {
            "house_dir": observation.get("survey_directory", ""),
            "outdoor_temp_sensor": observation.get("outdoor_temperature"),
            "flow_temp_entity": observation.get("flow_temperature"),
            **HUB_DEFAULTS,
        },
        "rooms": {
            room["id"]: _room_defaults(room, observation) for room in observation.get("rooms", [])
        },
        "boiler": {"config": boiler_config, "seed": {"enabled": True}, "enabled": True},
        "global_enabled": True,
        "legacy_entries": [],
    }


def editable_control(observation: dict[str, Any]) -> dict[str, Any]:
    """Return an independent editable copy, retaining imported metadata and seeds."""
    control = deepcopy(observation.get("control") or new_control(observation))
    if control.get("schema") != CONTROL_SCHEMA:
        raise ControlConfigError("control_unsupported_schema")
    control.setdefault("origin", "imported" if control.get("legacy_entries") else "standalone")
    control.setdefault("legacy_entries", [])
    control.setdefault("hub", {})
    control.setdefault("boiler", {}).setdefault("config", {})
    control["boiler"].setdefault("seed", {})
    control["boiler"].setdefault("enabled", bool(control["boiler"]["seed"].get("enabled", True)))
    return control


def add_observation_rooms(control: dict[str, Any], observation: dict[str, Any]) -> None:
    """Stage missing room controllers only after explicit room-control selection."""
    rooms = control.setdefault("rooms", {})
    for room in observation.get("rooms", []):
        rooms.setdefault(room["id"], _room_defaults(room, observation))


def hub_values(control: dict[str, Any]) -> dict[str, Any]:
    return {**HUB_DEFAULTS, **control.get("hub", {})}


def update_hub(control: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate and replace user-editable hub settings, retaining imported extras."""
    house_dir = str(values.get("house_dir", "")).strip()
    if not house_dir:
        raise ControlConfigError("control_required_survey")
    hub = control.setdefault("hub", {})
    hub.update(
        {
            "house_dir": house_dir,
            "global_enabled": bool(values.get("global_enabled", True)),
            "outdoor_temp_sensor": _entity(
                values.get("outdoor_temp_sensor"), ("sensor",), required=True
            ),
            "flow_temp_entity": _entity(values.get("flow_temp_entity"), ("sensor",), required=True),
            "weather_entity": _entity(values.get("weather_entity"), ("weather",)),
            "irradiance_sensor": _entity(values.get("irradiance_sensor"), ("sensor",)),
            "manual_flow_temp": _number(
                values, "manual_flow_temp", 20.0, 90.0, DEFAULT_MANUAL_FLOW_TEMP
            ),
            "ground_temp": _number(values, "ground_temp", -10.0, 30.0, 10.0),
        }
    )
    control["global_enabled"] = hub["global_enabled"]


def boiler_values(control: dict[str, Any]) -> dict[str, Any]:
    wrapper = control.get("boiler", {})
    return {
        **BOILER_DEFAULTS,
        **wrapper.get("config", {}),
        "enabled": wrapper.get(
            "enabled", wrapper.get("seed", {}).get("enabled", BOILER_DEFAULTS["enabled"])
        ),
    }


def update_boiler(control: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate boiler sources, bounds and manual-intervention policy."""
    profile = values.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED)
    if profile not in PROFILES:
        raise ControlConfigError("control_invalid_policy")
    flow_min = _number(values, "flow_min", 20, 80, DEFAULT_FLOW_MIN)
    flow_max = _number(values, "flow_max", 25, 90, DEFAULT_FLOW_MAX)
    dhw_min = _number(values, "dhw_flow_min", 30, 90, DEFAULT_DHW_FLOW_MIN)
    dhw_max = _number(values, "dhw_flow_max", 35, 90, DEFAULT_DHW_FLOW_MAX)
    dhw_return = _number(values, "dhw_return_ceiling", 20, 80, DEFAULT_DHW_RETURN_CEILING)
    if flow_min >= flow_max or dhw_min > dhw_max or dhw_return > dhw_max:
        raise ControlConfigError("control_invalid_limits")
    wrapper = control.setdefault("boiler", {})
    wrapper["enabled"] = bool(values.get("enabled", True))
    wrapper.setdefault("seed", {}).setdefault("enabled", wrapper["enabled"])
    config = wrapper.setdefault("config", {})
    entity_specs = {
        "flow_setpoint_entity": (("number",), True),
        "outdoor_temp_entity": (("sensor",), True),
        "current_flow_entity": (("sensor",), False),
        "return_temp_entity": (("sensor",), False),
        "heating_active_entity": (("binary_sensor",), False),
        "burner_power_entity": (("sensor",), False),
        "heat_demand_entity": (("sensor",), False),
        "hw_relay_demand_entity": (("sensor",), False),
        "cylinder_temp_entity": (("sensor",), False),
        "cylinder_target_entity": (
            ("number", "sensor", "climate", "water_heater"),
            False,
        ),
        "max_flow_entity": (("number", "sensor"), False),
        "boiler_relay_entity": (("switch", "binary_sensor"), False),
    }
    config.update(
        {
            key: _entity(values.get(key), domains, required)
            for key, (domains, required) in entity_specs.items()
        }
    )
    config.update(
        {
            "flow_min": flow_min,
            CONF_EFFICIENCY_PROFILE: profile,
            "flow_max": flow_max,
            "dhw_flow_min": dhw_min,
            "dhw_flow_max": dhw_max,
            "dhw_return_ceiling": dhw_return,
            "min_hold_minutes": _number(
                values, "min_hold_minutes", 0, 120, DEFAULT_MIN_HOLD_MINUTES
            ),
            "manual_hold_minutes": _number(
                values, "manual_hold_minutes", 0, 1440, DEFAULT_MANUAL_HOLD_MINUTES
            ),
            "dhw_target": _number(values, "dhw_target", 35, 80, DEFAULT_DHW_TARGET),
            "dhw_progress_minutes": _number(
                values, "dhw_progress_minutes", 5, 180, DEFAULT_DHW_PROGRESS_MINUTES
            ),
            "dhw_timeout_minutes": _number(
                values, "dhw_timeout_minutes", 15, 360, DEFAULT_DHW_TIMEOUT_MINUTES
            ),
            "input_freshness_minutes": _number(
                values, "input_freshness_minutes", 1, 120, DEFAULT_INPUT_FRESHNESS_MINUTES
            ),
            "outdoor_freshness_minutes": _number(
                values,
                "outdoor_freshness_minutes",
                5,
                360,
                DEFAULT_OUTDOOR_FRESHNESS_MINUTES,
            ),
            "dhw_fallback_flow": _number(values, "dhw_fallback_flow", 35, 90, dhw_max),
            "zone_demand_entities": _entities(values, "zone_demand_entities", ("sensor",)),
            "room_climate_entities": _entities(values, "room_climate_entities", ("climate",)),
        }
    )


def room_values(control: dict[str, Any], room: dict[str, Any]) -> dict[str, Any]:
    wrapper = control["rooms"][room["id"]]
    config = wrapper.get("config", {})
    values = {
        **ROOM_DEFAULTS,
        **config,
        "enabled": wrapper.get("enabled", True),
        "occupancy_enabled": wrapper.get("occupancy_enabled", True),
    }
    values["asymmetry_mode"] = (
        "survey_default"
        if "asymmetry_enabled" not in config
        else "enabled"
        if config["asymmetry_enabled"]
        else "disabled"
    )
    return values


def update_room(control: dict[str, Any], room: dict[str, Any], values: dict[str, Any]) -> None:
    """Validate one room while preserving learned state and imported tunables."""
    wrapper = control["rooms"][room["id"]]
    primary = _entity(values.get("primary_climate"), ("climate",), required=True)
    survey_room = str(values.get("room_id", "")).strip()
    if not survey_room:
        raise ControlConfigError("control_required_survey_room")
    minimum = _number(values, "zone_setpoint_min", 5, 34.9, DEFAULT_ZONE_SETPOINT_MIN)
    maximum = _number(values, "zone_setpoint_max", 5.1, 35, DEFAULT_ZONE_SETPOINT_MAX)
    if minimum >= maximum:
        raise ControlConfigError("control_invalid_limits")
    wrapper["enabled"] = bool(values.get("enabled", True))
    wrapper["occupancy_enabled"] = bool(values.get("occupancy_enabled", True))
    config = wrapper.setdefault("config", {})
    config.update(
        {
            "name": room["name"],
            "room_id": survey_room,
            "primary_climate": primary,
            "backup_climate": _entity(values.get("backup_climate"), ("climate",)),
            "air_temp_sensor": _entity(values.get("air_temp_sensor"), ("sensor",)),
            "occupancy_sensor": _entity(
                values.get("occupancy_sensor"), ("binary_sensor", "input_boolean")
            ),
            "zone_setpoint_min": minimum,
            "zone_setpoint_max": maximum,
            "manual_hold_minutes": int(
                _number(values, "manual_hold_minutes", 0, 1440, DEFAULT_MANUAL_HOLD)
            ),
            "preheat_release_minutes": int(
                _number(values, "preheat_release_minutes", 0, 360, DEFAULT_PREHEAT_RELEASE)
            ),
            "override_duration": int(_number(values, "override_duration", 0, 1440, 60)),
            "window_open_delay": int(
                _number(
                    values,
                    "window_open_delay",
                    0,
                    120,
                    DEFAULT_WINDOW_OPEN_DELAY,
                )
            ),
            "window_delay": int(_number(values, "window_delay", 0, 240, DEFAULT_WINDOW_DELAY)),
            "window_setpoint": _number(values, "window_setpoint", 5, 25, DEFAULT_WINDOW_SETPOINT),
            "time_window_enabled": bool(values.get("time_window_enabled", False)),
            "time_window_start": _time(values, "time_window_start", DEFAULT_TIME_WINDOW_START),
            "time_window_end": _time(values, "time_window_end", DEFAULT_TIME_WINDOW_END),
            "unoccupied_duration": int(
                _number(
                    values,
                    "unoccupied_duration",
                    0,
                    1440,
                    DEFAULT_UNOCCUPIED_DURATION,
                )
            ),
            **{
                key: _number(values, key, -5, 5, ROOM_DEFAULTS[key])
                for key in (
                    "weekday_morning_offset",
                    "weekday_afternoon_offset",
                    "weekday_evening_offset",
                    "weekend_morning_offset",
                    "weekend_afternoon_offset",
                    "weekend_evening_offset",
                )
            },
            "mode": "shadow",
        }
    )
    asymmetry = values.get("asymmetry_mode", "survey_default")
    if asymmetry == "survey_default":
        config.pop("asymmetry_enabled", None)
    elif asymmetry in ("enabled", "disabled"):
        config["asymmetry_enabled"] = asymmetry == "enabled"
    else:
        raise ControlConfigError("control_invalid_policy")


def validate_control_rooms(control: dict[str, Any]) -> None:
    """Require unambiguous actuator and survey bindings across all rooms."""
    rooms = control.get("rooms", {})
    if not rooms:
        raise ControlConfigError("control_no_rooms")
    climates = [spec.get("config", {}).get("primary_climate") for spec in rooms.values()]
    survey_rooms = [spec.get("config", {}).get("room_id") for spec in rooms.values()]
    if len(climates) != len(set(climates)) or len(survey_rooms) != len(set(survey_rooms)):
        raise ControlConfigError("control_duplicate_room")


def actuator_fingerprint(control: dict[str, Any] | None) -> tuple[Any, ...]:
    """Return mappings whose change invalidates an existing ownership decision."""
    if not control:
        return ()
    rooms = control.get("rooms", {})
    return (
        control.get("boiler", {}).get("config", {}).get("flow_setpoint_entity"),
        tuple(
            sorted(
                (room_id, spec.get("config", {}).get("primary_climate"))
                for room_id, spec in rooms.items()
            )
        ),
    )


def actuator_changed(old: dict[str, Any] | None, new: dict[str, Any]) -> bool:
    """A first setup and any later actuator rebind require explicit ownership."""
    return actuator_fingerprint(old) != actuator_fingerprint(new)
