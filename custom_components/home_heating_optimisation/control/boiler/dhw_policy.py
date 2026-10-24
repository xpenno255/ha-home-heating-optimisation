"""Options for how the boiler is run during a hot water charge; saved on their own.

Two independent aids, both off by default:

* **Anti-cycle override.** The boiler's own minimum burn period (EMS-ESP
  ``burnminperiod``) keeps it off after every stop. During a 60 °C cylinder session the
  boiler reaches its flow limit within minutes, stops, and then sits out the whole period,
  so burns are short with long gaps and the cylinder never gets there. While a charge runs
  the period is set lower, and the normal value is put back afterwards.
* **Hot-cylinder flow.** Once the cylinder is warm (or a raised DHW session is running)
  the DHW flow target is a higher fixed value, to keep the coil delivering.
"""

import math

SECTION = "dhw_boiler"
DEFAULTS = {
    "anticycle_enabled": False,
    "anticycle_entity": None,
    "anticycle_minutes": 5,
    # None leaves the DHW flow target as it is.
    "hot_flow": None,
    "hot_threshold": 48.0,
}
LIMITS = {
    "anticycle_minutes": (0, 15),
    "hot_flow": (70.0, 85.0),
    "hot_threshold": (30.0, 70.0),
}
SUGGESTED_ENTITY = "number.boiler_burnminperiod"


def dhw_boiler_config(config):
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["anticycle_enabled"] = bool(out["anticycle_enabled"])
    entity = out["anticycle_entity"]
    out["anticycle_entity"] = entity if isinstance(entity, str) and entity else None
    for key, (low, high) in LIMITS.items():
        value = out[key]
        if key == "hot_flow" and value is None:
            continue
        try:
            value = float(value)
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = int(value) if key == "anticycle_minutes" else value
    return out


def validation_error(values):
    for key, (low, high) in LIMITS.items():
        value = values.get(key)
        if key == "hot_flow" and value is None:
            continue
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "dhw_boiler_invalid"
    if values.get("anticycle_enabled") and not values.get("anticycle_entity"):
        return "dhw_boiler_needs_entity"
    return None


def hot_cylinder_flow(target, cylinder_temp, elevated, hot_flow, threshold):
    """``(flow, source)``: the DHW flow target, or the hot-cylinder value when it applies.

    It applies once the cylinder reads at least ``threshold`` or a raised DHW session is
    running. The caller still bounds the result by the DHW maximum and the boiler's limit.
    """
    if hot_flow is None or target is None:
        return target, "normal"
    if elevated or (cylinder_temp is not None and cylinder_temp >= threshold):
        return hot_flow, "hot_cylinder"
    return target, "normal"
