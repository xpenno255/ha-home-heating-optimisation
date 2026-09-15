"""Recorder history reconstructed through the same adapter as live collection."""

from collections import deque
from datetime import timedelta
from functools import partial

from ..observations import watched_entities
from .const import MAX_POINTS, SAMPLE_SECONDS
from .observations import snapshot
from .runs import recorded_runs
from .sampling import should_capture


def reconstruct(history, start, end, config, climate_unit, initial_states=None, runs=None):
    events = {}
    states = initial_states if initial_states is not None else {}
    for entity, rows in history.items():
        for state in sorted(rows, key=lambda s: s.last_updated):
            at = state.last_updated
            if at < start:
                states[entity] = state
            elif at <= end:
                events.setdefault(at, {})[entity] = state
    boundaries = {t for a, b, _ in (runs or []) for t in (a, b) if start <= t <= end}
    for t in boundaries:
        events.setdefault(t, {})
    ticks = set()
    at = start
    while at <= end:
        events.setdefault(at, {})
        ticks.add(at)
        at += timedelta(seconds=SAMPLE_SECONDS)
    events.setdefault(end, {})
    result = deque(maxlen=MAX_POINTS)
    count = 0
    # Keep room/activity edges exactly; sample numeric and controller context.
    last_capture = None
    for at, changes in sorted(events.items()):
        active = next((r for r in (runs or []) if r[0] <= at < r[1]), None)
        if at in boundaries or (runs is not None and active is None):
            states.clear()
        before = dict(states)
        states.update(changes)
        selected = (
            config
            if runs is None or (active and active[2])
            else {**config, "history_state_policy": "recent_change"}
        )
        if (
            at in ticks
            or at == end
            or at in boundaries
            or should_capture(config, set(changes), before, states, at.timestamp(), last_capture)
        ):
            point = snapshot(states, at, selected, climate_unit)
            if at not in ticks and at != end:
                point.pop("intent", None)
            result.append(point)
            count += 1
            last_capture = at.timestamp()
    return list(result), count > MAX_POINTS


async def async_backfill(hass, config, start, end):
    if "recorder" not in hass.config.components:
        return [], False, "recorder_unavailable"
    from homeassistant.components.recorder import get_instance
    from homeassistant.components.recorder.history import get_significant_states

    runs = await get_instance(hass).async_add_executor_job(recorded_runs, hass, start, end)
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
            runs,
        )
        retained.update({p["time"]: p for p in points})
        truncated |= limited or len(retained) > MAX_POINTS
        if len(retained) > MAX_POINTS:
            retained = {t: retained[t] for t in sorted(retained)[-MAX_POINTS:]}
        start = chunk_end
    return [retained[t] for t in sorted(retained)], truncated, "complete"
