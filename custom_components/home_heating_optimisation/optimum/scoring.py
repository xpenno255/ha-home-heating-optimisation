"""Score a shadow start prediction against what Evohome's own Optimum Start did (pure).

A record holds the samples seen from the start of the prediction window to 120 minutes
past the switchpoint: ``[timestamp, zone temperature, zone setpoint, outdoor]``.
"""

import statistics
from datetime import datetime, timezone

from .model import boost_factor, climb

SETPOINT_EPS = 0.05


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def _temp_at(samples, ts):
    """Temperature of the last sample at or before ``ts`` (else the first)."""
    before = [s for s in samples if s[0] <= ts and s[1] is not None]
    if before:
        return before[-1][1]
    return next((s[1] for s in samples if s[1] is not None), None)


def evohome_start(samples, schedule_target, switch):
    """``(time, early, censored)`` of Evohome's start for a switchpoint.

    Early when the zone setpoint reached the scheduled target before the switchpoint;
    otherwise Evohome did nothing early and its start is the switchpoint itself.
    """
    for i, (t, _temp, setpoint, _out) in enumerate(samples):
        if t >= switch:
            break
        if setpoint is not None and setpoint >= schedule_target - SETPOINT_EPS:
            return t, True, i == 0
    return switch, False, False


def reached_at(samples, goal):
    """First time the zone is at ``goal`` and still is at the next sample."""
    for (t, temp, *_), (_t2, temp2, *_) in zip(samples, samples[1:]):
        if temp is not None and temp2 is not None and temp >= goal and temp2 >= goal:
            return t
    return None


def _minutes(a, b):
    return round((a - b) / 60, 1)


def score_start(record, tolerance=0.3):
    """Scores for one closed record; every figure is None when it cannot be judged.

    The record carries the commanded ``target`` (the goal: schedule plus HHO's offset), the
    ``schedule_target`` Evohome aims at, the pace and dead time used (``mpk``,
    ``delay_minutes``), the room's median event supply and the boost flow for the what-if.
    """
    samples = [s for s in record["samples"] if s[1] is not None]
    switch, goal = record["switch_ts"], record["target"]
    schedule_target = record.get("schedule_target", goal)
    arrival = goal - tolerance
    mpk, delay = record["mpk"], record.get("delay_minutes") or 0.0
    outdoors = [s[3] for s in samples if s[3] is not None]
    mean_out = statistics.fmean(outdoors) if outdoors else None
    frozen = record.get("frozen")
    base = {
        "switch": _at(switch).isoformat(),
        "target": goal,
        "schedule_target": schedule_target,
        "outdoor_source": record.get("outdoor_source"),
        "predicted_start": None if not frozen else _at(frozen["start_ts"]).isoformat(),
        "predicted_lead_min": None if not frozen else frozen["lead_minutes"],
    }
    at_switch = _temp_at(samples, switch)
    empty = {
        "evohome_start": None,
        "evohome_early": None,
        "evohome_start_censored": False,
        "reached": None,
        "evohome_lateness_min": None,
        "model_error_min": None,
        "hho_lateness_estimate_min": None,
        "boost_lateness_estimate_min": None,
        "forecast_error_k": None,
    }
    if at_switch is None or at_switch >= arrival:
        return {**base, **empty, "outcome": "not_needed", "temperature_at_switch": at_switch}

    def heating(ts, factor=1.0):
        temp = _temp_at(samples, ts)
        if temp is None:
            return None
        need = climb(goal, temp, tolerance)
        return delay + mpk * need * factor if need > 0 else 0.0

    es, early, censored = evohome_start(samples, schedule_target, switch)
    reached = reached_at([s for s in samples if s[0] >= es], arrival)
    actual = None if reached is None else max(0, _minutes(reached, es))
    predicted = heating(es)
    hho = None
    if frozen:
        # A prediction made after its own start time (lead 0) is judged from when it was made.
        late_made = frozen["lead_minutes"] == 0 and frozen.get("frozen_at")
        ref = frozen["frozen_at"] if late_made else frozen["start_ts"]
        if (own := heating(ref)) is not None:
            hho = _minutes(ref + own * 60, switch)
    boost = None
    factor = boost_factor(
        record.get("median_supply"), record["boost_flow"], _temp_at(samples, es), goal
    )
    if factor is not None and (scaled := heating(es, factor)) is not None:
        boost = _minutes(es + scaled * 60, switch)
    forecast_mean = record.get("forecast_mean")
    return {
        **base,
        "outcome": "scored",
        "temperature_at_switch": at_switch,
        "evohome_start": _at(es).isoformat(),
        "evohome_early": early,
        "evohome_start_censored": censored,
        "reached": None if reached is None else _at(reached).isoformat(),
        "evohome_lateness_min": None if reached is None else _minutes(reached, switch),
        "model_error_min": None
        if predicted is None or actual is None
        else round(predicted - actual, 1),
        "hho_lateness_estimate_min": hho,
        "boost_lateness_estimate_min": boost,
        "forecast_error_k": None
        if forecast_mean is None or mean_out is None
        else round(forecast_mean - mean_out, 2),
    }


def rolling(scores):
    """Median model error (minutes, absolute) and Evohome lateness over scored records."""
    scores = [s for s in scores if s.get("outcome") != "not_needed"]
    errors = [abs(s["model_error_min"]) for s in scores if s.get("model_error_min") is not None]
    late = [s["evohome_lateness_min"] for s in scores if s.get("evohome_lateness_min") is not None]
    return {
        "scored": len(scores),
        "median_model_error_min": round(statistics.median(errors), 1) if errors else None,
        "median_evohome_lateness_min": round(statistics.median(late), 1) if late else None,
    }
