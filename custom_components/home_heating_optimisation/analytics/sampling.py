"""Shared event selection: room edges are exact, numeric context is sampled."""

from ..const import SYSTEM_SOURCES

CONTEXT_SECONDS = 60


def source_groups(config):
    rooms = {
        r[k]
        for r in config["rooms"]
        for k in ("climate", "air_sensor", "demand_sensor")
        if r.get(k)
    }
    signals = {
        config[k] for k, spec in SYSTEM_SOURCES.items() if spec.kind == "binary" and config.get(k)
    }
    numeric = {
        config[k]
        for k, spec in SYSTEM_SOURCES.items()
        if spec.kind == "temperature" and config.get(k)
    }
    return rooms, signals, numeric


def should_capture(config, changed, before, after, at, last):
    rooms, signals, numeric = source_groups(config)
    if not changed or changed & (rooms | signals):
        return True
    if not changed & numeric:
        return False  # Controller intent is sampled on the five-minute timer.
    for entity in changed & numeric:
        old, new = before.get(entity), after.get(entity)
        if (
            old is None
            or new is None
            or old.state in ("unknown", "unavailable")
            or new.state in ("unknown", "unavailable")
        ):
            return True
    return last is None or int(at // CONTEXT_SECONDS) > int(last // CONTEXT_SECONDS)
