"""Daily scorecard arithmetic (pure; no Home Assistant).

A day is built from samples taken about once a minute. Each sample is weighted by the
minutes since the one before, capped so a gap (restart, outage) is left out rather than
guessed; `coverage` says how much of the day was seen.

Comfort is judged against the room's air target *before* overdrive or sensor matching,
only while the room is in normal control (state ``active``): pre-heat, a manual hold, an
open window and time outside the room's window are not the controller's result.
"""

from __future__ import annotations

from math import isfinite

BASE_C = 15.5  # degree-day base temperature
TOLERANCE_K = 0.3  # "on target" band either side of the air target
SHORT_K = 0.5  # further below target than this counts as short
CONDENSING_RETURN_C = 55.0
SHORT_BURN_SECONDS = 90.0
MAX_STEP_MINUTES = 5.0  # a longer gap between samples is not counted
MAX_GAS_KW = 36.0  # no boiler here burns faster; a bigger jump is a counter fault
# The billing meter reports about every half hour. A day counts as metered only when the
# meter was reporting for most of it, and a reading after a long silence is not trusted.
METER_FRESH_MINUTES = 90.0
METER_MAX_SILENCE_MINUTES = 120.0
METER_MIN_COVERAGE = 0.9
MAX_DAYS = 1100
HELD_STATES = ("manual", "window_open")
CONTROL_STATES = ("active", "preheat")
# Morning arrival: a rise in a room's scheduled comfort target of at least this much.
ARRIVAL_STEP_K = 0.3
ARRIVAL_WATCH_MINUTES = 180.0
MAX_ARRIVALS = 4
# Black globe: felt (operative) temperature sits this share of the air-to-globe gap below
# the air temperature. For a small globe the share is 0.7-0.95; 0.8 is used throughout.
GLOBE_FACTOR = 0.8
GLOBE_FRESH_MINUTES = 30.0
DAY_PARTS = (("night", 23, 6), ("morning", 6, 9), ("daytime", 9, 16), ("evening", 16, 23))


def day_part(hour: int) -> str:
    """The part of the day a local hour falls in."""
    for name, start, end in DAY_PARTS:
        if start <= hour < end or (start > end and (hour >= start or hour < end)):
            return name
    return "night"


def _num(value):
    return (
        float(value)
        if isinstance(value, (int, float)) and not isinstance(value, bool) and isfinite(value)
        else None
    )


def empty_day(date: str) -> dict:
    return {
        "date": date,
        "minutes": 0.0,
        "outdoor_sum": 0.0,
        "outdoor_minutes": 0.0,
        "degree_minutes": 0.0,
        "indoor_sum": 0.0,
        "indoor_minutes": 0.0,
        "gas": None,
        "gas_dhw": 0.0,
        "gas_unsplit": 0.0,
        "gas_meter": None,
        "meter_minutes": 0.0,
        "fire_minutes": 0.0,
        "fire_condensing_minutes": 0.0,
        "fire_return_sum": 0.0,
        "globe": {},
        "rooms": {},
    }


def _blank_room(name=None) -> dict:
    return {
        "name": name,
        "minutes": 0.0,
        "error_sum": 0.0,
        "within": 0.0,
        "short": 0.0,
        "short_degree_minutes": 0.0,
        "over_degree_minutes": 0.0,
        "held": 0.0,
        "writes": 0,
        "offset_sum": 0.0,
        "offset_minutes": 0.0,
        "arrivals": [],
    }


def _room(day: dict, room_id: str, name) -> dict:
    room = day["rooms"].get(room_id)
    if room is None:
        room = day["rooms"][room_id] = _blank_room(name)
    if name:
        room["name"] = name
    return room


def add_sample(
    day: dict, minutes: float, *, boiler: dict | None, rooms: dict, meter_live: bool = False
) -> None:
    """Add one sample covering `minutes`.

    `boiler`: outdoor, dhw, power (burner %), return_c. `rooms`: id -> name, state,
    in_window, air, target. `meter_live`: the billing meter is currently reporting.
    """
    minutes = _num(minutes)
    if minutes is None or minutes <= 0:
        return
    minutes = min(minutes, MAX_STEP_MINUTES)
    day["minutes"] += minutes
    if meter_live:
        day["meter_minutes"] += minutes
        if day["gas_meter"] is None:
            day["gas_meter"] = 0.0
    boiler = boiler or {}
    outdoor = _num(boiler.get("outdoor"))
    if outdoor is not None:
        day["outdoor_sum"] += outdoor * minutes
        day["outdoor_minutes"] += minutes
        day["degree_minutes"] += max(0.0, BASE_C - outdoor) * minutes
    power, return_c = _num(boiler.get("power")), _num(boiler.get("return_c"))
    if power is not None and power > 0 and not boiler.get("dhw") and return_c is not None:
        day["fire_minutes"] += minutes
        day["fire_return_sum"] += return_c * minutes
        if return_c < CONDENSING_RETURN_C:
            day["fire_condensing_minutes"] += minutes
    airs = []
    for room_id, sample in rooms.items():
        room = _room(day, room_id, sample.get("name"))
        air, target = _num(sample.get("air")), _num(sample.get("target"))
        if air is not None:
            airs.append(air)
        state = sample.get("state")
        if state in HELD_STATES:
            room["held"] += minutes
        if state != "active" or not sample.get("in_window") or air is None or target is None:
            continue
        error = air - target
        room["minutes"] += minutes
        offset = _num(sample.get("offset"))
        if offset is not None:
            room["offset_sum"] += offset * minutes
            room["offset_minutes"] += minutes
        room["error_sum"] += error * minutes
        if abs(error) <= TOLERANCE_K + 1e-9:
            room["within"] += minutes
        if error < -SHORT_K:
            room["short"] += minutes
        room["short_degree_minutes"] += max(0.0, -error) * minutes
        room["over_degree_minutes"] += max(0.0, error) * minutes
    if airs:
        day["indoor_sum"] += sum(airs) / len(airs) * minutes
        day["indoor_minutes"] += minutes


def add_write(day: dict, room_id: str, name=None) -> None:
    _room(day, room_id, name)["writes"] += 1


def note_arrival(day: dict, room_id: str, name, *, at: str, ts: float, step_k, error_k) -> None:
    """A room's scheduled target just rose: record how far the room was from its new air
    target at that moment. Zero minutes when it was already there."""
    step_k, error_k = _num(step_k), _num(error_k)
    arrivals = _room(day, room_id, name)["arrivals"]
    if step_k is None or error_k is None or len(arrivals) >= MAX_ARRIVALS:
        return
    arrivals.append(
        {
            "at": at,
            "ts": ts,
            "step_k": round(step_k, 2),
            "error_k": round(error_k, 2),
            "minutes": 0 if error_k >= -TOLERANCE_K - 1e-9 else None,
        }
    )


def progress_arrivals(day: dict, room_id: str, *, ts: float, error_k) -> None:
    """Close any open arrival once the room is within the on-target band."""
    error_k = _num(error_k)
    room = day["rooms"].get(room_id)
    if room is None or error_k is None or error_k < -TOLERANCE_K - 1e-9:
        return
    for arrival in room["arrivals"]:
        waited = (ts - arrival["ts"]) / 60
        if arrival["minutes"] is None and 0 <= waited <= ARRIVAL_WATCH_MINUTES:
            arrival["minutes"] = round(waited)


def add_globe(
    day: dict, part: str, minutes: float, *, air, globe, applied, target, outdoor=None
) -> None:
    """One sample of the room holding the black globe, filed under the part of the day.

    `applied` is the correction the controller added to the comfort target; `target` is
    that comfort (operative) target.
    """
    minutes, air, globe = _num(minutes), _num(air), _num(globe)
    applied, target, outdoor = _num(applied), _num(target), _num(outdoor)
    if minutes is None or minutes <= 0 or air is None or globe is None:
        return
    minutes = min(minutes, MAX_STEP_MINUTES)
    acc = day["globe"].setdefault(
        part,
        {
            "minutes": 0.0,
            "gap_sum": 0.0,
            "applied_sum": 0.0,
            "applied_minutes": 0.0,
            "felt_sum": 0.0,
            "felt_minutes": 0.0,
            "outdoor_sum": 0.0,
            "outdoor_minutes": 0.0,
        },
    )
    gap = air - globe
    acc["minutes"] += minutes
    acc["gap_sum"] += gap * minutes
    if applied is not None:
        acc["applied_sum"] += applied * minutes
        acc["applied_minutes"] += minutes
    if target is not None:
        acc["felt_sum"] += (air - GLOBE_FACTOR * gap - target) * minutes
        acc["felt_minutes"] += minutes
    if outdoor is not None:
        acc["outdoor_sum"] += outdoor * minutes
        acc["outdoor_minutes"] += minutes


def gas_delta(previous, current, minutes):
    """kWh between two meter readings, or None for a reset, a fault or no reading."""
    previous, current, minutes = _num(previous), _num(current), _num(minutes)
    if previous is None or current is None or minutes is None or minutes <= 0:
        return None
    delta = current - previous
    if delta < 0 or delta > MAX_GAS_KW * minutes / 60 + 1.0:
        return None
    return delta


def add_gas(day: dict, kwh: float, *, dhw: bool, split: bool) -> None:
    """Add metered gas. `split` is False when it spans a gap, so its use is unknown."""
    day["gas"] = (day["gas"] or 0.0) + kwh
    if not split:
        day["gas_unsplit"] += kwh
    elif dhw:
        day["gas_dhw"] += kwh


def add_meter_gas(day: dict, kwh: float) -> None:
    """Add gas read from the billing meter."""
    day["gas_meter"] = (day["gas_meter"] or 0.0) + kwh


def burner_summary(burns, start_ts: float, end_ts: float) -> dict:
    """Burns that started in [start, end): rows of [start epoch, seconds, mean %, DHW]."""
    inside = [b for b in burns if start_ts <= b[0] < end_ts]
    seconds = sum(b[1] for b in inside)
    return {
        "starts": len(inside),
        "dhw_starts": sum(1 for b in inside if b[3]),
        "short_burns": sum(1 for b in inside if b[1] < SHORT_BURN_SECONDS),
        "firing_minutes": round(seconds / 60, 1),
        "mean_burn_minutes": round(seconds / len(inside) / 60, 1) if inside else None,
    }


def _ratio(part, whole, digits=3):
    return round(part / whole, digits) if whole else None


def finalise(day: dict, burner: dict | None = None) -> dict:
    """The day's row. Figures with nothing behind them are None, never zero."""
    # The boiler's own counter gives the split between heating and hot water. The billing
    # meter, when it covered the day, gives the amount, and the split is scaled to it.
    counter = day.get("gas")
    meter_coverage = _ratio(min(day["meter_minutes"], day["minutes"]), day["minutes"])
    meter = day.get("gas_meter")
    metered = meter is not None and (meter_coverage or 0) >= METER_MIN_COVERAGE
    gas, dhw, unsplit, source = counter, None, None, None
    if counter is not None:
        dhw, unsplit, source = day["gas_dhw"], day["gas_unsplit"], "boiler_counter"
    if metered:
        gas, source = meter, "meter"
        scale = meter / counter if counter else None
        dhw = dhw * scale if scale is not None else None
        unsplit = unsplit * scale if scale is not None else None
    heating = gas - dhw if gas is not None and dhw is not None else None
    degree_days = day["degree_minutes"] / 1440 if day["outdoor_minutes"] else None
    rooms, total = {}, {"minutes": 0.0, "within": 0.0, "short_dm": 0.0, "over_dm": 0.0}
    writes = 0
    for room_id, r in day["rooms"].items():
        writes += r["writes"]
        total["minutes"] += r["minutes"]
        total["within"] += r["within"]
        total["short_dm"] += r["short_degree_minutes"]
        total["over_dm"] += r["over_degree_minutes"]
        rooms[room_id] = {
            "name": r["name"],
            "minutes": round(r["minutes"]),
            "mean_error_k": _ratio(r["error_sum"], r["minutes"], 2),
            "within": _ratio(r["within"], r["minutes"]),
            "short": _ratio(r["short"], r["minutes"]),
            "short_degree_hours": round(r["short_degree_minutes"] / 60, 2),
            "over_degree_hours": round(r["over_degree_minutes"] / 60, 2),
            "held_minutes": round(r["held"]),
            "writes": r["writes"],
            "applied_correction_k": _ratio(r["offset_sum"], r["offset_minutes"], 2),
            # Each rise in the scheduled target: how far under the room was at that
            # moment, and minutes until it was within the on-target band (None: not
            # within three hours).
            "arrivals": [
                {
                    "at": a["at"],
                    "step_k": a["step_k"],
                    "error_k": a["error_k"],
                    "minutes_to_target": a["minutes"],
                }
                for a in r["arrivals"]
            ],
        }
    globe = {}
    for part, g in day.get("globe", {}).items():
        gap = _ratio(g["gap_sum"], g["minutes"], 2)
        globe[part] = {
            "minutes": round(g["minutes"]),
            "outdoor_c": _ratio(g["outdoor_sum"], g["outdoor_minutes"], 1),
            "air_minus_globe_k": gap,
            "needed_k": None if gap is None else round(GLOBE_FACTOR * gap, 2),
            "applied_k": _ratio(g["applied_sum"], g["applied_minutes"], 2),
            "felt_vs_target_k": _ratio(g["felt_sum"], g["felt_minutes"], 2),
        }
    return {
        "date": day["date"],
        "coverage": round(min(1.0, day["minutes"] / 1440), 3),
        "outdoor_c": _ratio(day["outdoor_sum"], day["outdoor_minutes"], 1),
        "indoor_c": _ratio(day["indoor_sum"], day["indoor_minutes"], 2),
        "degree_days": None if degree_days is None else round(degree_days, 2),
        "gas_kwh": None if gas is None else round(gas, 1),
        "gas_heating_kwh": None if heating is None else round(heating, 1),
        "gas_dhw_kwh": None if dhw is None else round(dhw, 1),
        "gas_unsplit_kwh": None if unsplit is None else round(unsplit, 1),
        "gas_source": source,
        "gas_meter_kwh": None if meter is None else round(meter, 1),
        "gas_meter_coverage": meter_coverage if meter is not None else None,
        "gas_counter_kwh": None if counter is None else round(counter, 1),
        "counter_to_meter": (
            round(counter / meter, 3) if metered and counter is not None and meter > 0 else None
        ),
        # Below one degree-day the ratio says more about the weather than the house.
        "heating_kwh_per_degree_day": (
            round(heating / degree_days, 2)
            if heating is not None and degree_days is not None and degree_days >= 1
            else None
        ),
        "burner": burner,
        "condensing_share": _ratio(day["fire_condensing_minutes"], day["fire_minutes"]),
        "firing_return_c": _ratio(day["fire_return_sum"], day["fire_minutes"], 1),
        "comfort_within": _ratio(total["within"], total["minutes"]),
        "short_degree_hours": round(total["short_dm"] / 60, 2),
        "over_degree_hours": round(total["over_dm"] / 60, 2),
        "writes": writes,
        "globe": globe or None,
        "rooms": rooms,
    }


def restore_day(stored) -> dict | None:
    """A stored part-day, with anything a newer version added filled in; None if unusable."""
    if not (
        isinstance(stored, dict)
        and isinstance(stored.get("date"), str)
        and isinstance(stored.get("rooms"), dict)
    ):
        return None
    day = {**empty_day(stored["date"]), **stored}
    if not isinstance(day["globe"], dict):
        day["globe"] = {}
    rooms = {}
    for room_id, room in day["rooms"].items():
        if not isinstance(room, dict):
            return None
        rooms[room_id] = {**_blank_room(), **room}
        if not isinstance(rooms[room_id]["arrivals"], list):
            rooms[room_id]["arrivals"] = []
    day["rooms"] = rooms
    return day
