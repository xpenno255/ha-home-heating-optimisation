"""Pure rules for the DHW measured-temperature cutoff.

Evohome switches DHW from the cylinder temperature it last received. When the
controller misses the sensor's reports it keeps charging past its target until the
sensor next reports, which can be an hour once the temperature settles. HA hears the
same sensor through its own gateways; once a charge is under way and HA's reading is
``margin`` above the Evohome target, DHW is held off with a temporary override.

Handing back to the schedule straight away would let the controller, still holding
its stale reading, restart the charge. Each hold lasts an hour and the controller
ends it by itself, so HA stopping can never leave DHW off for longer; HA renews it
while still needed, up to four hours, and hands back early once it measures the
cylinder below the controller's reheat point (target minus differential).
"""

import math
from datetime import timedelta

SECTION = "dhw_cutoff"
MIN_MARGIN, MAX_MARGIN = 0.5, 10.0
DEFAULTS = {
    "enabled": False,
    "water_heater_entity": None,
    "demand_entity": None,
    "cylinder_temp_entity": None,
    # Optional Evohome cloud water heater: its mode survives an HA restart, when the
    # RAMSES entity can briefly show follow_schedule in the middle of a boost.
    "cloud_entity": None,
    # Evohome normally stops at its first report a little above target (51-52 °C for a
    # 50 °C target); 1 °C catches the overruns without racing that by much.
    "margin": 1.0,
}
ENTITY_FIELDS = ("water_heater_entity", "demand_entity", "cylinder_temp_entity")
HOLD = timedelta(minutes=60)
# A hold still needed is renewed shortly before the controller would end it, up to
# MAX_HOLD in total. The cylinder can take hours to cool to the reheat point.
RENEW_BEFORE = timedelta(minutes=5)
MAX_HOLD = timedelta(hours=4)
# The sensor reports every minute or two while the cylinder heats. An older reading
# stops a charge only if it was taken during that charge (see `charge_temperature`).
READING_MAX_AGE = timedelta(minutes=10)
# Time for the controller to echo the override before it is sent once more.
CONFIRM = timedelta(minutes=3)
MAX_ATTEMPTS = 2


def cutoff_config(config):
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["enabled"] = bool(out["enabled"])
    for key in (*ENTITY_FIELDS, "cloud_entity"):
        out[key] = out[key] if isinstance(out[key], str) and out[key] else None
    try:
        out["margin"] = float(out["margin"])
    except TypeError, ValueError:
        out["margin"] = DEFAULTS["margin"]
    return out


def validation_error(values):
    margin = values.get("margin")
    if (
        not isinstance(margin, (int, float))
        or isinstance(margin, bool)
        or not math.isfinite(margin)
        or not MIN_MARGIN <= margin <= MAX_MARGIN
    ):
        return "dhw_cutoff_invalid_margin"
    if values.get("enabled") and not all(values.get(k) for k in ENTITY_FIELDS):
        return "dhw_missing_entity"
    return None


def usable(cfg):
    return cfg["enabled"] and all(cfg[k] for k in ENTITY_FIELDS)


def recent_temperature(state, now):
    """A plausible °C reading reported within READING_MAX_AGE, else None."""
    return _reading(state, now - READING_MAX_AGE, now)


def charge_temperature(state, now, charge_since):
    """A recent reading, or failing that one taken since the running charge began.

    While the cylinder charges it only gains heat, so a reading from this charge is a
    lower bound even after the sensor goes quiet. Live 28 Oct: it reported 51.8 °C at
    18:20, then nothing until 60.75 °C at 19:19, and the controller charged on.
    """
    if (value := recent_temperature(state, now)) is not None or charge_since is None:
        return value
    return _reading(state, charge_since, now)


def _reading(state, earliest, now):
    """A plausible °C reading reported between `earliest` and `now`, else None."""
    if state is None or state.state in ("unavailable", "unknown", "none", ""):
        return None
    seen = getattr(state, "last_reported", None) or state.last_updated
    if not earliest <= seen <= now:
        return None
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    if state.attributes.get("unit_of_measurement") == "°F":
        value = (value - 32) * 5 / 9
    return value if math.isfinite(value) and -20 <= value <= 110 else None


def cutoff_temperature(params, margin):
    return None if params is None else params["setpoint"] + margin


def should_stop(demand, temperature, params, margin):
    """A charge is running and HA's recent reading has passed target plus margin."""
    limit = cutoff_temperature(params, margin)
    return demand is True and temperature is not None and limit is not None and temperature >= limit


def should_release(temperature, params):
    """HA measures the cylinder below the controller's own reheat point."""
    if temperature is None or params is None:
        return False
    return temperature < params["setpoint"] - params["differential"]


def is_hold(mode):
    """The water heater's mode is a temporary override with DHW off."""
    return (
        isinstance(mode, dict)
        and mode.get("mode") == "temporary_override"
        and mode.get("active") is False
    )


def cloud_override(state):
    """Why the Evohome cloud entity shows DHW is not following a scheduled charge.

    None when it follows the schedule with the schedule on, or when it gives no usable
    answer (missing, unavailable, no status): the RAMSES mode check still applies.
    """
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    status = state.attributes.get("status")
    if not isinstance(status, dict):
        return None
    mode = (status.get("state_status") or {}).get("mode")
    if isinstance(mode, str) and mode and mode != "FollowSchedule":
        return f"Evohome mode {mode}"
    scheduled = (status.get("setpoints") or {}).get("this_sp_state")
    if mode == "FollowSchedule" and scheduled == "Off":
        return "Evohome schedule has DHW off"
    return None
