"""Score a shadow start prediction against what Evohome's own Optimum Start did (pure).

A record holds the samples seen from the start of the prediction window to 120 minutes
past the switchpoint: ``[timestamp, zone temperature, zone setpoint, outdoor]``.
"""

import statistics
from datetime import datetime, timezone

from .model import boost_factor, climb

SETPOINT_EPS = 0.05
HOLD_SECONDS = 300


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc)


def _temp_at(samples, ts):
    """Temperature of the last sample at or before ``ts`` (else the first)."""
    before = [s for s in samples if s[0] <= ts and s[1] is not None]
    if before:
        return before[-1][1]
    return next((s[1] for s in samples if s[1] is not None), None)


def evohome_start(samples, target):
    """First time the zone setpoint reached the new target before the switchpoint."""
    for i, (t, _temp, setpoint, _out) in enumerate(samples):
        if setpoint is not None and abs(setpoint - target) <= SETPOINT_EPS:
            return t, i == 0
    return None, False


def reached_at(samples, goal):
    """First time the zone is at ``goal`` and still is at the next sample 5 minutes on."""
    for (t, temp, *_), (t2, temp2, *_) in zip(samples, samples[1:]):
        if temp is not None and temp2 is not None and temp >= goal and temp2 >= goal:
            if t2 - t >= HOLD_SECONDS - 1:
                return t
    return None


def _minutes(a, b):
    return round((a - b) / 60, 1)


def score_start(record, tolerance=0.3):
    """Scores for one closed record; every figure is None when it cannot be judged.

    The record carries the pace used (``mpk``, minutes per kelvin), the room's median
    event supply and the boost flow for the what-if.
    """
    samples = [s for s in record["samples"] if s[1] is not None]
    switch, target = record["switch_ts"], record["target"]
    goal = target - tolerance
    mpk = record["mpk"]
    outdoors = [s[3] for s in samples if s[3] is not None]
    mean_out = statistics.fmean(outdoors) if outdoors else None

    def heating(ts):
        temp = _temp_at(samples, ts)
        return None if temp is None else mpk * climb(target, temp, tolerance)

    es, censored = evohome_start(samples, target)
    reached = reached_at([s for s in samples if s[0] >= (es or switch - 86400)], goal)
    actual = None if es is None or reached is None else max(0, _minutes(reached, es))
    predicted = heating(es) if es is not None else None
    frozen = record.get("frozen")
    hho = None
    if frozen and (own := heating(frozen["start_ts"])) is not None:
        hho = _minutes(frozen["start_ts"] + own * 60, switch)
    boost = None
    if es is not None and predicted is not None:
        factor = boost_factor(
            record.get("median_supply"), record["boost_flow"], _temp_at(samples, es), target
        )
        if factor is not None:
            boost = _minutes(es + predicted * factor * 60, switch)
    forecast_mean = record.get("forecast_mean")
    return {
        "switch": _at(switch).isoformat(),
        "target": target,
        "evohome_start": None if es is None else _at(es).isoformat(),
        "evohome_start_censored": censored,
        "reached": None if reached is None else _at(reached).isoformat(),
        "evohome_lateness_min": None if reached is None else _minutes(reached, switch),
        "model_error_min": None
        if predicted is None or actual is None
        else round(predicted - actual, 1),
        "hho_lateness_estimate_min": hho,
        "boost_lateness_estimate_min": boost,
        "outdoor_source": record.get("outdoor_source"),
        "forecast_error_k": None
        if forecast_mean is None or mean_out is None
        else round(forecast_mean - mean_out, 2),
        "predicted_start": None if not frozen else _at(frozen["start_ts"]).isoformat(),
        "predicted_lead_min": None if not frozen else frozen["lead_minutes"],
    }


def rolling(scores):
    """Median model error (minutes, absolute) and Evohome lateness over scored records."""
    errors = [abs(s["model_error_min"]) for s in scores if s.get("model_error_min") is not None]
    late = [s["evohome_lateness_min"] for s in scores if s.get("evohome_lateness_min") is not None]
    return {
        "scored": len(scores),
        "median_model_error_min": round(statistics.median(errors), 1) if errors else None,
        "median_evohome_lateness_min": round(statistics.median(late), 1) if late else None,
    }
