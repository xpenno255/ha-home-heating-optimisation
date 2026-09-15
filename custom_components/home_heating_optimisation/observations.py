"""Timestamped measurements; unknown input never implies no heating demand."""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from homeassistant.core import State

from .const import ROOM_MAX_AGE, SYSTEM_SOURCES


@dataclass(frozen=True)
class Reading:
    value: float | bool | None
    quality: str
    source: str | None = None
    reported_at: datetime | None = None


@dataclass(frozen=True)
class RoomObservation:
    id: str
    name: str
    air: Reading
    target: Reading
    demand: Reading
    deficit: float | None
    enabled: bool | None


@dataclass(frozen=True)
class Snapshot:
    at: datetime
    rooms: tuple[RoomObservation, ...]
    system: dict[str, Reading]
    operating_state: str

    @property
    def input_availability(self) -> float:
        """Instantaneous known/configured input share, not historical coverage."""
        readings = [r for room in self.rooms for r in (room.air, room.target, room.demand)]
        readings += list(self.system.values())
        configured = [r for r in readings if r.quality != "not_configured"]
        if not configured:
            return 0.0
        return round(100 * sum(r.quality == "ok" for r in configured) / len(configured), 1)


def read(
    states: dict[str, State],
    entity_id: str | None,
    now: datetime,
    *,
    kind: str = "temperature",
    attribute: str | None = None,
    max_age: int | None = ROOM_MAX_AGE,
    climate_unit: str = "°C",
    time_basis: str = "last_reported",
) -> Reading:
    """Normalise a reading and retain source/quality even when no value is usable."""
    if not entity_id:
        return Reading(None, "not_configured")
    state = states.get(entity_id)
    if state is None:
        return Reading(None, "missing", entity_id)
    reported = getattr(state, time_basis)

    def result(value, quality):
        return Reading(value, quality, entity_id, reported)

    if state.state in ("unknown", "unavailable"):
        return result(None, "unavailable")
    age = (now - reported).total_seconds()
    if age < 0:
        return result(None, "invalid")
    if max_age is not None and age > max_age:
        return result(None, "stale")
    raw = state.attributes.get(attribute) if attribute else state.state
    if kind == "binary":
        return result(raw == "on", "ok") if raw in ("on", "off") else result(None, "invalid")
    if isinstance(raw, bool):
        return result(None, "invalid")
    try:
        value = float(raw)
    except TypeError, ValueError:
        return result(None, "invalid")
    if not math.isfinite(value):
        return result(None, "invalid")
    unit = state.attributes.get("unit_of_measurement")
    if kind == "temperature":
        # HA climate temperature attributes use HA's configured temperature unit.
        unit = climate_unit if entity_id.startswith("climate.") else unit
        if unit == "°F":
            value = (value - 32) * 5 / 9
        elif unit == "K":
            value -= 273.15
        elif unit != "°C":
            return result(None, "invalid")
        if not -80 <= value <= 150:
            return result(None, "invalid")
    elif kind == "demand":
        # A climate heat_demand attribute is a fraction, regardless of unrelated units.
        unit = None if attribute else unit
        if unit == "%":
            value /= 100
        elif unit not in (None, ""):
            return result(None, "invalid")
        if not 0 <= value <= 1:
            return result(None, "invalid")
    return result(value, "ok")


def make_snapshot(states: dict[str, State], config: dict[str, Any], now, climate_unit):
    rooms = []
    for room in config["rooms"]:
        zone = room["climate"]
        air = read(
            states,
            room.get("air_sensor") or zone,
            now,
            attribute=None if room.get("air_sensor") else "current_temperature",
            climate_unit=climate_unit,
        )
        target = read(
            states, zone, now, attribute="temperature", max_age=None, climate_unit=climate_unit
        )
        demand = read(
            states,
            room.get("demand_sensor") or zone,
            now,
            kind="demand",
            attribute=None if room.get("demand_sensor") else "heat_demand",
        )
        state = states.get(zone)
        enabled = (
            None
            if state is None or state.state in ("unavailable", "unknown")
            else state.state != "off"
        )
        deficit = None
        if enabled and air.value is not None and target.value is not None:
            deficit = round(max(0.0, target.value - air.value), 3)
        rooms.append(
            RoomObservation(room["id"], room["name"], air, target, demand, deficit, enabled)
        )
    system = {
        key: read(states, config.get(key), now, kind=spec.kind, max_age=spec.max_age)
        for key, spec in SYSTEM_SOURCES.items()
    }
    heating, dhw = system["heating_active"].value, system["dhw_active"].value
    mode = "unknown"
    if heating is not None and dhw is not None:
        mode = "mixed" if heating and dhw else "heating" if heating else "dhw" if dhw else "idle"
    return Snapshot(now, tuple(rooms), system, mode)


def watched_entities(config):
    entities = {config[k] for k in SYSTEM_SOURCES if config.get(k)}
    for room in config["rooms"]:
        entities.update(room[k] for k in ("climate", "air_sensor", "demand_sensor") if room.get(k))
    if config.get("boiler_decision_sensor"):
        entities.add(config["boiler_decision_sensor"])
    for room in config["rooms"]:
        entities.update(
            room[k]
            for k in (
                "comfort_target_sensor",
                "corrected_air_target_sensor",
                "estimated_operative_sensor",
                "decision_sensor",
            )
            if room.get(k)
        )
    return sorted(entities)
