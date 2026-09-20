"""Pure counter arithmetic: unit normalisation, resets, rollovers and gaps."""

import math

from .const import (
    BUCKET_SECONDS,
    DEFAULT_CALORIFIC_MJ_M3,
    DEFAULT_VOLUME_CORRECTION,
    GAP_FACTOR,
    KINDS,
    MAX_METERS,
    MIN_CONTEXT_SHARE,
    MJ_PER_KWH,
)

UNIT_ALIASES = {
    "kwh": "kWh",
    "wh": "Wh",
    "mwh": "MWh",
    "m³": "m³",
    "m3": "m³",
    "cubic meters": "m³",
    "cubic metres": "m³",
}


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def normalise_unit(unit):
    if not isinstance(unit, str):
        return None
    return UNIT_ALIASES.get(unit.strip().lower())


def to_kwh(
    value,
    unit,
    *,
    calorific_value_mj_m3=DEFAULT_CALORIFIC_MJ_M3,
    volume_correction=DEFAULT_VOLUME_CORRECTION,
):
    """Convert a metered quantity to kWh; None when the unit is not understood."""
    if not finite(value):
        return None
    unit = normalise_unit(unit)
    if unit == "kWh":
        return float(value)
    if unit == "Wh":
        return value / 1000
    if unit == "MWh":
        return value * 1000
    if unit == "m³":
        return value * volume_correction * calorific_value_mj_m3 / MJ_PER_KWH
    return None


def meter_specs(config):
    """Validated meter definitions with stable slugs derived from kind and order."""
    meters = (config.get("energy") or {}).get("meters") or []
    specs = []
    counts = {}
    for meter in meters[:MAX_METERS]:
        if not isinstance(meter, dict) or not meter.get("entity") or meter.get("kind") not in KINDS:
            continue
        kind = meter["kind"]
        counts[kind] = counts.get(kind, 0) + 1
        slug = kind if counts[kind] == 1 else f"{kind}_{counts[kind]}"
        specs.append(
            {
                "slug": slug,
                "entity": meter["entity"],
                "kind": kind,
                "unit": normalise_unit(meter.get("unit")),
                "calorific_value_mj_m3": meter.get("calorific_value_mj_m3")
                or DEFAULT_CALORIFIC_MJ_M3,
                "volume_correction": meter.get("volume_correction") or DEFAULT_VOLUME_CORRECTION,
            }
        )
    return specs


def rollover_limit(previous):
    """The decade boundary a counter would wrap at, e.g. 99998 -> 100000."""
    if previous <= 0:
        return None
    return 10 ** len(str(int(previous)))


def meter_delta(previous, current, spec, bucket_seconds=BUCKET_SECONDS):
    """Energy between two counter readings, never inventing lost intervals.

    Readings are dicts with value, unit, time and entity. A reset loses the energy
    between the last reading and the reset moment, so it yields no kWh. A decade
    rollover is arithmetically recoverable. A source or unit change is a reset
    flagged as source_changed. A gap longer than the cadence keeps the metered
    kWh but is excluded from coverage because its time allocation is unknown.
    """
    result = {"kwh": None, "quality": "missing", "source_changed": False}
    if current is None or not finite(current.get("value")):
        return result
    unit = spec.get("unit") or normalise_unit(current.get("unit"))
    if unit is None:
        result["quality"] = "unit_unknown"
        return result
    if previous is None or not finite(previous.get("value")):
        return result
    previous_unit = spec.get("unit") or normalise_unit(previous.get("unit"))
    if previous.get("entity") != current.get("entity") or previous_unit != unit:
        result.update(quality="reset", source_changed=True)
        return result
    convert = {
        "calorific_value_mj_m3": spec["calorific_value_mj_m3"],
        "volume_correction": spec["volume_correction"],
    }
    delta = current["value"] - previous["value"]
    if delta < 0:
        limit = rollover_limit(previous["value"])
        if (
            limit is not None
            and previous["value"] >= 0.9 * limit
            and current["value"] < 0.1 * limit
        ):
            result["kwh"] = to_kwh(limit - previous["value"] + current["value"], unit, **convert)
            result["quality"] = "rollover"
        else:
            result["quality"] = "reset"
        return result
    result["kwh"] = to_kwh(delta, unit, **convert)
    elapsed = current["time"] - previous["time"]
    result["quality"] = "gap" if elapsed > bucket_seconds * GAP_FACTOR else "ok"
    return result


def bucket_context(samples, start, end):
    """Time-weighted heating/DHW shares and outdoor mean over [start, end).

    Samples are (time, heating, dhw, outdoor) tuples sorted by time; each value holds
    until the next sample. Shares are None when the known time is under half the bucket.
    """
    length = end - start
    known = {"heating": 0.0, "dhw": 0.0}
    active = {"heating": 0.0, "dhw": 0.0}
    outdoor_weight = 0.0
    outdoor_sum = 0.0
    current = None
    for sample in samples:
        if sample[0] <= start:
            current = sample
            continue
        break
    events = [s for s in samples if start < s[0] < end]
    points = [start] + [s[0] for s in events] + [end]
    states = [current] + events
    for state, begin, finish in zip(states, points, points[1:]):
        if state is None:
            continue
        span = finish - begin
        for index, key in ((1, "heating"), (2, "dhw")):
            if state[index] is not None:
                known[key] += span
                if state[index]:
                    active[key] += span
        if finite(state[3]):
            outdoor_weight += span
            outdoor_sum += state[3] * span
    return {
        key: round(active[key] / known[key], 4)
        if length and known[key] / length >= MIN_CONTEXT_SHARE
        else None
        for key in known
    } | {
        "outdoor": round(outdoor_sum / outdoor_weight, 2)
        if length and outdoor_weight / length >= MIN_CONTEXT_SHARE
        else None
    }


def allocation(heating_share, dhw_share):
    """Never split energy between heating and DHW; report unknown instead."""
    if heating_share is None or dhw_share is None:
        return "unknown"
    if heating_share > 0 and dhw_share > 0:
        return "unknown"
    if heating_share > 0:
        return "heating"
    if dhw_share > 0:
        return "dhw"
    return "idle"
