"""Recorder-reproducible observations using the shared input normalisation.

Identical last_reported heartbeats are not retained by Recorder. Historical
recency therefore uses last_updated for both live collection and backfill.
Availability can persist independently under the recorded-state policy.
"""

import math
from functools import partial

from ..const import ROOM_MAX_AGE, SYSTEM_SOURCES
from ..observations import read

CONTEXT = {
    "outdoor_temperature": "outdoor",
    "flow_temperature": "supply",
    "return_temperature": "return",
    "heating_active": "heating_active",
    "dhw_active": "dhw_active",
}
ROOM_INTENT = (
    "comfort_target_sensor",
    "corrected_air_target_sensor",
    "estimated_operative_sensor",
    "decision_sensor",
)
INTENT_ATTRIBUTES = (
    "reason",
    "action",
    "mode",
    "window_override_active",
    "would_write",
    "requested_target",
    "sent_target",
    "pending_target",
    "confirmed_target",
    "write_status",
    "readback_status",
    "readback_timed_out",
    "requested_at",
    "sent_at",
    "pending_since",
    "confirmed_at",
    "readback_at",
    "room_correction",
    "room_error",
    "schedule_setpoint",
    "occupancy_offset",
    "schedule_source",
    "model_version",
)


def number(value):
    if isinstance(value, bool):
        return None
    try:
        value = float(value)
    except ValueError, TypeError:
        return None
    return value if math.isfinite(value) else None


def expiry(reading, age, now):
    return (
        reading.reported_at.timestamp() + age
        if reading.value is not None and reading.reported_at
        else now.timestamp()
    )


def intent(states, entity):
    state = states.get(entity)
    if state is None:
        return None
    attrs = {
        k: v[:255] if isinstance(v, str) else v
        for k in INTENT_ATTRIBUTES
        if isinstance(v := state.attributes.get(k), (str, bool, int, float))
        and (not isinstance(v, float) or math.isfinite(v))
    }
    return {
        "state": state.state[:255],
        "unit": state.attributes.get("unit_of_measurement"),
        "updated_at": state.last_updated.isoformat(),
        "attributes": attrs,
    }


def snapshot(states, at, config, climate_unit):
    available_states = config.get("history_state_policy", "recorded_state") == "recorded_state"
    read_at = partial(read, states, now=at, time_basis="last_updated", climate_unit=climate_unit)
    room_age = None if available_states else ROOM_MAX_AGE
    zones = {}
    decisions = {}
    for room in config["rooms"]:
        climate = room["climate"]
        air = read_at(
            room.get("air_sensor") or climate,
            max_age=room_age,
            attribute=None if room.get("air_sensor") else "current_temperature",
        )
        target = read_at(climate, attribute="temperature", max_age=None)
        demand = read_at(
            room.get("demand_sensor") or climate,
            kind="demand",
            max_age=room_age,
            attribute=None if room.get("demand_sensor") else "heat_demand",
        )
        zones[room["id"]] = {
            "temperature": air.value,
            "target": target.value,
            "active": demand.value > 0 if demand.value is not None else None,
            "demand": demand.value,
            "valid_until": at.timestamp() + ROOM_MAX_AGE
            if available_states and air.value is not None and target.value is not None
            else expiry(air, ROOM_MAX_AGE, at)
            if target.value is not None
            else at.timestamp(),
            "demand_valid_until": at.timestamp() + ROOM_MAX_AGE
            if available_states and demand.value is not None
            else expiry(demand, ROOM_MAX_AGE, at),
            "change_valid_until": expiry(air, ROOM_MAX_AGE, at),
            "demand_change_valid_until": expiry(demand, ROOM_MAX_AGE, at),
            "temperature_updated": air.reported_at.timestamp() if air.reported_at else None,
        }
        decisions[room["id"]] = {k: intent(states, room[k]) for k in ROOM_INTENT if room.get(k)}
    context, valid = {}, {}
    for key, name in CONTEXT.items():
        spec = SYSTEM_SOURCES[key]
        value = read_at(
            config.get(key), kind=spec.kind, max_age=None if available_states else spec.max_age
        )
        context[name] = value.value
        valid[name] = (
            at.timestamp() + ROOM_MAX_AGE
            if available_states and value.value is not None
            else expiry(value, spec.max_age, at)
        )
    return {
        "time": at.timestamp(),
        "zones": zones,
        "context": context,
        "context_valid_until": valid,
        "intent": {
            "rooms": decisions,
            "boiler": intent(states, config.get("boiler_decision_sensor")),
        },
    }
