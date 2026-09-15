"""Recorder history reconstructed through the same adapter as live collection."""

from collections import deque
from datetime import timedelta
from functools import partial

from ..observations import watched_entities
from .const import MAX_POINTS, SAMPLE_SECONDS
from .observations import snapshot


def reconstruct(history, start, end, config, climate_unit, initial_states=None):
    events = {}
    states = initial_states if initial_states is not None else {}
    for entity, rows in history.items():
        for state in sorted(rows, key=lambda s: s.last_updated):
            at = state.last_updated
            if at <= start:
                states[entity] = state
            elif at <= end:
                events.setdefault(at, {})[entity] = state
    at = start
    while at <= end:
        events.setdefault(at, {})
        at += timedelta(seconds=SAMPLE_SECONDS)
    events.setdefault(end, {})
    result = deque(maxlen=MAX_POINTS)
    count = 0
    # Keep every event in the retained interval: do not hide target or demand edges.
    for at, changes in sorted(events.items()):
        states.update(changes)
        result.append(snapshot(states, at, config, climate_unit))
        count += 1
    return list(result), count > MAX_POINTS


async def async_backfill(hass, config, start, end):
    if "recorder" not in hass.config.components:
        return [], False, "recorder_unavailable"
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    retained = {}
    truncated = False
    carried_states = {}
    while start < end:
        chunk_end = min(start + timedelta(days=1), end)
        history = await get_instance(hass).async_add_executor_job(
            partial(
                get_significant_states,
                hass,
                start - timedelta(microseconds=1),
                chunk_end,
                watched_entities(config),
                include_start_time_state=False,
                significant_changes_only=False,
                minimal_response=False,
            )
        )
        points, limited = await hass.async_add_executor_job(
            reconstruct,
            history,
            start,
            chunk_end,
            config,
            hass.config.units.temperature_unit,
            carried_states,
        )
        retained.update({p["time"]: p for p in points})
        truncated |= limited or len(retained) > MAX_POINTS
        if len(retained) > MAX_POINTS:
            retained = {t: retained[t] for t in sorted(retained)[-MAX_POINTS:]}
        start = chunk_end
    return [retained[t] for t in sorted(retained)], truncated, "complete"
