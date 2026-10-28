"""Options for the warm-up flow boost; saved on their own, never via a control edit."""

import math

from .core.warmup import MODES

SECTION = "warmup_boost"
DEFAULTS = {
    "mode": "off",
    "catchup_mode": "off",
    "start": 0.15,
    "full": 0.40,
    "decay_k_per_min": 1.0,
}
LIMITS = {"start": (0.05, 0.6), "full": (0.06, 1.0), "decay_k_per_min": (0.2, 5.0)}


def warmup_config(config):
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    for key in ("mode", "catchup_mode"):
        if out[key] not in MODES:
            out[key] = DEFAULTS[key]
    for key, (low, high) in LIMITS.items():
        try:
            value = float(out[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = value
    if out["full"] <= out["start"]:
        out["start"], out["full"] = DEFAULTS["start"], DEFAULTS["full"]
    return out


def validation_error(values):
    if values.get("mode") not in MODES or values.get("catchup_mode", "off") not in MODES:
        return "warmup_boost_invalid"
    for key, (low, high) in LIMITS.items():
        value = values.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "warmup_boost_invalid"
    if values["full"] <= values["start"]:
        return "warmup_boost_invalid"
    return None
