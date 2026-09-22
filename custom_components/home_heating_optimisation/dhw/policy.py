"""Pure, clock-injected rules for the DHW target schedule.

HHO only changes the cylinder setpoint. Evohome keeps the DHW schedule, the
on/off decision and its own cutoff; nothing here forces or blocks a charge.
"""

import math
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta

SECTION = "dhw_schedule"
WEEKDAYS = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")
MIN_TARGET, MAX_TARGET, TARGET_STEP = 35.0, 85.0, 0.5
DEFAULTS = {
    "enabled": False,
    "water_heater_entity": None,
    "demand_entity": None,
    "cylinder_temp_entity": None,
    "normal_target": 50.0,
    "high_target": 60.0,
    "weekdays": [],
    "window_start": "04:00:00",
    "window_end": "06:00:00",
    # Changes only on explicit enable or a new normal target: permits one normal-target write
    # and re-arms a paused schedule. Unrelated saves and restarts keep it.
    "revision": None,
}
ENTITY_FIELDS = ("water_heater_entity", "demand_entity", "cylinder_temp_entity")
# Charge completion needs this long of continuous, valid, off demand.
OFF_DWELL_SECONDS = 600
SOURCE_STALE = timedelta(minutes=30)
UNAVAILABLE = ("unavailable", "unknown", "none", "")
TEMPORARY_MODES = ("temporary_override", "countdown_override")


def parse_time(value):
    if isinstance(value, time):
        return value
    try:
        return time.fromisoformat(str(value))
    except TypeError, ValueError:
        return None


def valid_target(value):
    return (
        isinstance(value, (int, float))
        and not isinstance(value, bool)
        and math.isfinite(value)
        and MIN_TARGET <= value <= MAX_TARGET
        and float(value * 2).is_integer()
    )


def dhw_config(config):
    """Normalised section; unknown keys are dropped, missing ones take defaults."""
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["enabled"] = bool(out["enabled"])
    for key in ENTITY_FIELDS:
        out[key] = out[key] if isinstance(out[key], str) and out[key] else None
    for key in ("normal_target", "high_target"):
        try:
            out[key] = float(out[key])
        except TypeError, ValueError:
            out[key] = DEFAULTS[key]
    days = out["weekdays"] if isinstance(out["weekdays"], list) else []
    out["weekdays"] = [d for d in WEEKDAYS if d in days]
    for key in ("window_start", "window_end"):
        parsed = parse_time(out[key])
        out[key] = (parsed or parse_time(DEFAULTS[key])).isoformat()
    out["revision"] = out["revision"] if isinstance(out["revision"], str) else None
    return out


def validation_error(values):
    """First problem with a submitted section, as an options-flow error key."""
    normal, high = values.get("normal_target"), values.get("high_target")
    if not valid_target(normal) or not valid_target(high) or high <= normal:
        return "dhw_invalid_targets"
    start, end = parse_time(values.get("window_start")), parse_time(values.get("window_end"))
    if start is None or end is None or start >= end:
        return "dhw_invalid_window"
    if values.get("enabled") and not all(values.get(k) for k in ENTITY_FIELDS):
        return "dhw_missing_entity"
    return None


def usable(cfg):
    return cfg["enabled"] and all(cfg[k] for k in ENTITY_FIELDS)


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def read_params(state):
    """RAMSES setpoint, overrun and differential; None unless all three are usable."""
    if state is None or state.state in UNAVAILABLE:
        return None
    params = state.attributes.get("params")
    cfg = params.get("config") if isinstance(params, dict) else None
    if not isinstance(cfg, dict):
        return None
    setpoint, overrun, differential = (cfg.get(k) for k in ("setpoint", "overrun", "differential"))
    if not all(_number(v) for v in (setpoint, overrun, differential)):
        return None
    return {
        "setpoint": float(setpoint),
        "overrun": int(overrun),
        "differential": float(differential),
    }


def params_equal(a, b):
    return (
        a is not None
        and b is not None
        and all(
            abs(float(a[k]) - float(b[k])) < 0.01 for k in ("setpoint", "overrun", "differential")
        )
    )


def _mode(state):
    if state is None:
        return None
    mode = state.attributes.get("mode")
    if not isinstance(mode, dict):
        params = state.attributes.get("params")
        mode = params.get("mode") if isinstance(params, dict) else None
    return mode if isinstance(mode, dict) else None


def read_mode(state):
    mode = _mode(state)
    return mode.get("mode") if mode and isinstance(mode.get("mode"), str) else None


def read_allowed(state):
    """Whether Evohome currently allows DHW heating; None when not known.

    This is permission only, never evidence that the cylinder is charging.
    """
    mode = _mode(state)
    if mode and isinstance(mode.get("active"), bool):
        return mode["active"]
    if state is not None and state.state == "off":
        return False
    return None


def _fresh(state, now):
    seen = getattr(state, "last_reported", None) or state.last_updated
    return timedelta(0) <= now - seen <= SOURCE_STALE


def demand_value(state, now):
    """Physical DHW demand: True, False, or None when missing, unavailable or stale."""
    if state is None or state.state in UNAVAILABLE or not _fresh(state, now):
        return None
    if state.state in ("on", "off"):
        return state.state == "on"
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    return value > 0 if math.isfinite(value) else None


def temperature_value(state, now):
    if state is None or state.state in UNAVAILABLE or not _fresh(state, now):
        return None
    try:
        value = float(state.state)
    except TypeError, ValueError:
        return None
    if state.attributes.get("unit_of_measurement") == "°F":
        value = (value - 32) * 5 / 9
    return value if math.isfinite(value) and -20 <= value <= 110 else None


def window(day: date, cfg, zone):
    """Timezone-aware start and end of the local window on one calendar day."""
    start = datetime.combine(day, parse_time(cfg["window_start"]), tzinfo=zone)
    end = datetime.combine(day, parse_time(cfg["window_end"]), tzinfo=zone)
    return start, end


def session_due(now, cfg, zone, armed_since, consumed):
    """The window to open now, if any.

    A window only opens when its start is observed after arming, so a start or
    enable mid-window skips that session. ``consumed`` is a high-watermark of
    local dates: clock or DST repeats never open a date twice.
    """
    day = now.astimezone(zone).date()
    if WEEKDAYS[day.weekday()] not in cfg["weekdays"]:
        return None
    if consumed is not None and day.isoformat() <= consumed:
        return None
    start, end = window(day, cfg, zone)
    if armed_since is None or not armed_since <= start <= now < end:
        return None
    return day, start, end


def next_window(now, cfg, zone):
    if not cfg["weekdays"]:
        return None
    today = now.astimezone(zone).date()
    for offset in range(8):
        day = today + timedelta(days=offset)
        if WEEKDAYS[day.weekday()] in cfg["weekdays"]:
            start, _ = window(day, cfg, zone)
            if start > now:
                return start
    return None


@dataclass
class Evidence:
    """First-session charge evidence, counted from the moment of the request."""

    charged: bool = False
    reached: bool = False
    off_since: float | None = None
    schedule_off: bool = False

    def observe(self, mono, demand, temperature, allowed, high):
        if demand is True:
            self.charged = True
            self.off_since = None
        elif demand is None:
            # Unknown or stale demand never counts towards the off dwell.
            self.off_since = None
        elif self.charged and self.off_since is None:
            self.off_since = mono
        if self.charged and temperature is not None and temperature >= high:
            self.reached = True
        if self.charged and allowed is False:
            self.schedule_off = True

    def result(self, mono):
        """An early end established by evidence, or None to keep waiting."""
        if self.reached and (
            self.schedule_off
            or (self.off_since is not None and mono - self.off_since >= OFF_DWELL_SECONDS)
        ):
            return "complete"
        if self.schedule_off:
            return "incomplete"
        return None

    def deadline_outcome(self):
        if not self.charged:
            return "no_charge"
        if not self.reached:
            return "target_not_reached"
        return "insufficient_evidence"
