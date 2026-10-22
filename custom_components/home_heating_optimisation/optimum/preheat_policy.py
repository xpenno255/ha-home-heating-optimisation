"""Options for the planned morning pre-heat; saved on their own, never via a control edit.

``off`` leaves everything as it was. ``shadow`` plans each room's warm-up and records what
it would have done. ``active`` lets HHO raise a room to its next target at the planned
start, so a schedule time means "warm by then". It plans with the optimum predictor, so it
needs that enabled.
"""

import math

from ..control.boiler.core.warmup import MODES

SECTION = "preheat"
DEFAULTS = {
    "mode": "off",
    # Reference flow a warm-up is planned at: gentle enough to keep radiators from loading.
    "plan_flow": 55.0,
    # Earliest a pre-heat starts before the warm-by time.
    "max_lead_minutes": 240,
    # Radiator fullness (return - room) / (flow - room) where the boost cap starts to bite
    # and where it reaches zero. Read by the boiler side.
    "fullness_low": 0.45,
    "fullness_high": 0.65,
}
LIMITS = {
    "plan_flow": (40.0, 80.0),
    "max_lead_minutes": (30, 360),
    "fullness_low": (0.1, 0.95),
    "fullness_high": (0.15, 1.0),
}
INTEGERS = ("max_lead_minutes",)


def preheat_config(config):
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    if out["mode"] not in MODES:
        out["mode"] = DEFAULTS["mode"]
    for key, (low, high) in LIMITS.items():
        try:
            value = float(out[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = int(value) if key in INTEGERS else value
    if out["fullness_high"] <= out["fullness_low"]:
        out["fullness_low"], out["fullness_high"] = (
            DEFAULTS["fullness_low"],
            DEFAULTS["fullness_high"],
        )
    return out


def validation_error(values, optimum_enabled=True):
    if values.get("mode") not in MODES:
        return "preheat_invalid"
    for key, (low, high) in LIMITS.items():
        value = values.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "preheat_invalid"
    if values["fullness_low"] >= values["fullness_high"]:
        return "preheat_invalid"
    if values["mode"] == "active" and not optimum_enabled:
        return "preheat_needs_optimum"
    return None
