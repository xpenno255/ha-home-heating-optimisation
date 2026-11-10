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
MAX_DAYS = 1100
HELD_STATES = ("manual", "window_open")


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
        "fire_minutes": 0.0,
        "fire_condensing_minutes": 0.0,
        "fire_return_sum": 0.0,
        "rooms": {},
    }


def _room(day: dict, room_id: str, name) -> dict:
    room = day["rooms"].get(room_id)
    if room is None:
        room = day["rooms"][room_id] = {
            "name": name,
            "minutes": 0.0,
            "error_sum": 0.0,
            "within": 0.0,
            "short": 0.0,
            "short_degree_minutes": 0.0,
            "over_degree_minutes": 0.0,
            "held": 0.0,
            "writes": 0,
        }
    if name:
        room["name"] = name
    return room


def add_sample(day: dict, minutes: float, *, boiler: dict | None, rooms: dict) -> None:
    """Add one sample covering `minutes`.

    `boiler`: outdoor, dhw, power (burner %), return_c. `rooms`: id -> name, state,
    in_window, air, target.
    """
    minutes = _num(minutes)
    if minutes is None or minutes <= 0:
        return
    minutes = min(minutes, MAX_STEP_MINUTES)
    day["minutes"] += minutes
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
    gas = day.get("gas")
    dhw = day["gas_dhw"] if gas is not None else None
    heating = gas - dhw if gas is not None else None
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
        "gas_unsplit_kwh": None if gas is None else round(day["gas_unsplit"], 1),
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
        "rooms": rooms,
    }


def valid_day(day) -> bool:
    """A stored part-day this version can keep adding to."""
    if not (
        isinstance(day, dict)
        and isinstance(day.get("date"), str)
        and isinstance(day.get("rooms"), dict)
        and all(key in day for key in empty_day(""))
    ):
        return False
    keys = set(_room(empty_day(""), "x", None))
    return all(isinstance(r, dict) and keys <= set(r) for r in day["rooms"].values())
