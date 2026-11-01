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
    # Overdrive: most K added to a room's written zone target (and the pre-heat push);
    # None turns both off.
    "catchup_overdrive_k": None,
    # Overdrive scale (1 = the standard table) and how often a room's overdrive may change.
    "overdrive_strength": 1.0,
    "overdrive_period_min": 10,
}
LIMITS = {"start": (0.05, 0.6), "full": (0.06, 1.0), "decay_k_per_min": (0.2, 5.0)}
OVERDRIVE_LIMITS = (0.2, 2.0)
OVERDRIVE_SETTINGS = {"overdrive_strength": (0.25, 3.0), "overdrive_period_min": (1, 30)}


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
    out["catchup_overdrive_k"] = _overdrive(out["catchup_overdrive_k"])
    for key, (low, high) in OVERDRIVE_SETTINGS.items():
        try:
            value = float(out[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = int(round(value)) if key == "overdrive_period_min" else value
    return out


def _overdrive(value):
    try:
        value = float(value)
    except TypeError, ValueError:
        return None
    low, high = OVERDRIVE_LIMITS
    return round(value, 1) if math.isfinite(value) and low <= value <= high else None


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
    overdrive = values.get("catchup_overdrive_k")
    if overdrive is not None and _overdrive(overdrive) is None:
        return "warmup_boost_invalid"
    for key, (low, high) in OVERDRIVE_SETTINGS.items():
        value = values.get(key, DEFAULTS[key])
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "warmup_boost_invalid"
    return None
