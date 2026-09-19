"""Observation-only configuration and source definitions."""

from copy import deepcopy
from dataclasses import dataclass

DOMAIN = "home_heating_optimisation"
NAME = "Home Heating Optimisation"
VERSION = "0.6.4"
ROOMS = "rooms"
ZONES = "zones"
ROOM_MAX_AGE = 30 * 60


@dataclass(frozen=True)
class SourceSpec:
    """A source's meaning and freshness policy, in seconds."""

    kind: str
    max_age: int | None
    domains: tuple[str, ...] = ("sensor",)


SYSTEM_SOURCES = {
    "outdoor_temperature": SourceSpec("temperature", 120 * 60),
    "flow_temperature": SourceSpec("temperature", 10 * 60),
    "return_temperature": SourceSpec("temperature", 10 * 60),
    "flow_setpoint": SourceSpec("temperature", None, ("number", "sensor")),
    "heating_active": SourceSpec("binary", 5 * 60, ("binary_sensor",)),
    "dhw_active": SourceSpec("binary", 5 * 60, ("binary_sensor",)),
}


def effective_config(entry):
    """Options are a complete replacement, including cleared optional mappings."""
    return deepcopy(dict(entry.options or entry.data))
