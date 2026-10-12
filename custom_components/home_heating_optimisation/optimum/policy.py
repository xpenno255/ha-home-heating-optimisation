"""Options for the shadow optimum start / stop predictor.

The predictor never writes to a device: it predicts when a room should start warming
before an upward switchpoint (and when heating could stop before a downward one),
journals the prediction and scores it against what Evohome's own Optimum Start did.
A later release can act on it once the scores beat Evohome's.
"""

import math

SECTION = "optimum"
DEFAULTS = {
    "enabled": False,
    # Earliest a start is predicted, and the most a stop is brought forward.
    "max_lead_minutes": 180,
    # Safety time added to every predicted start.
    "margin_minutes": 10,
    # How far below target a room may coast before the next switchpoint (optimum stop).
    "coast_drop": 0.3,
    "min_warmups": 2,
    "min_cooldowns": 3,
    # Optional weather entity; its hourly forecast replaces the current outdoor reading.
    "forecast_entity": None,
    # Flow temperature for the what-if of a warm-up with a hotter flow. Never applied.
    "boost_flow": 65.0,
}
# Inclusive (low, high) for each numeric option.
LIMITS = {
    "max_lead_minutes": (30, 360),
    "margin_minutes": (0, 60),
    "coast_drop": (0.1, 2.0),
    "min_warmups": (1, 20),
    "min_cooldowns": (1, 20),
    "boost_flow": (40.0, 80.0),
}
INTEGERS = ("max_lead_minutes", "margin_minutes", "min_warmups", "min_cooldowns")


def optimum_config(config):
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["enabled"] = bool(out["enabled"])
    entity = out["forecast_entity"]
    out["forecast_entity"] = entity if isinstance(entity, str) and entity else None
    for key, (low, high) in LIMITS.items():
        try:
            value = float(out[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = int(value) if key in INTEGERS else value
    return out


def validation_error(values):
    for key, (low, high) in LIMITS.items():
        value = values.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "optimum_invalid_value"
    return None
