"""Pure empirical warm-up model for optimum start and stop.

A first physics fit (a lumped RC room) could not be identified on the live house: warm-ups
are driven by 0.1-0.3 K target nudges and a pulsing burner, and mild weather hides the
loss. This learns directly from what happened instead. Each *warm-up event* is a rise of
the target that the room then followed to arrival. A room's pace is the recency-weighted
median of ``minutes per kelvin`` over its events; the start lead is that pace times the
kelvin still to climb. Pace counts the whole climb to target (not to target minus the
arrival tolerance): small warm-ups carry a fixed dead time that would otherwise inflate it.

Time is injected: nothing here reads the clock.
"""

import math
import statistics
from dataclasses import dataclass
from datetime import datetime, time, timedelta

HALF_LIFE_DAYS = 7.0
# Smallest climb a pace is divided by; events start at 0.5 K.
MIN_CLIMB = 0.3
# Floor for an outdoor-adjusted pace (minutes per kelvin).
MIN_PACE = 1.0
# Smallest climb whose first +0.1 K reading measures the dead time.
DELAY_MIN_DEFICIT = 0.8
# The outdoor term is only tried with this many events, and must beat the plain median.
MIN_OUTDOOR_EVENTS = 8
RADIATOR_EXPONENT = 1.3
MEAN_WATER_DROP = 5.0
# Event supply is a mean over a pulsing burner, so the pure radiator law overstates a boost;
# never claim more than twice the pace.
BOOST_FACTOR_BOUNDS = (0.5, 1.0)
# A planned warm-up at a gentler flow than the events were learned at can take longer than
# the learned pace (factor above 1) or, at a hotter flow, less; the same pulsing-burner
# caution bounds it either way.
PLAN_FACTOR_BOUNDS = (0.5, 2.0)
MIN_SUPPLY_MARGIN = 5.0


@dataclass(frozen=True)
class RoomModel:
    minutes_per_k: float
    # Outdoor term minutes_per_k = a + b * (goal - outdoor), only when it earned its place.
    outdoor_a: float | None
    outdoor_b: float | None
    median_supply: float | None
    events: int
    loo_error_min: float | None
    # Dead time before a room responds, median over events that recorded one.
    delay_minutes: float = 0.0

    @property
    def outdoor_model(self):
        return self.outdoor_b is not None


@dataclass(frozen=True)
class StartParams:
    margin_minutes: int = 10
    max_lead_minutes: int = 180
    # Reaching target within this counts as arrived, as in the recovery analytics.
    tolerance: float = 0.3


@dataclass(frozen=True)
class StartPrediction:
    """When a room must start warming to reach its target at the switchpoint."""

    start_at: datetime
    lead_minutes: int
    heating_minutes: float
    minutes_per_k: float
    delay_minutes: float
    # Predicted minutes past the switchpoint when the lead would exceed the cap; 0 on time.
    late_minutes: int
    # What-if at the boost flow (never applied); all None when the supply is unknown.
    boost_lead_minutes: int | None
    boost_late_minutes: int | None
    boost_gain_minutes: int | None
    outdoor_used: bool
    # Pace as learned (outdoor-adjusted, before any plan-flow scaling), and the plan-flow
    # factor that was applied to it (1 when the plan flow is not used or the supply unknown).
    base_minutes_per_k: float | None = None
    plan_factor: float = 1.0


@dataclass(frozen=True)
class StopPrediction:
    """How long heating can be off before the switchpoint without a drop past the limit."""

    off_at: datetime
    coast_minutes: int
    rate: float


def effective_deficit(event, tolerance):
    return max(MIN_CLIMB, event["deficit"])


def climb(target, temp, tolerance):
    """Kelvin to climb, counted to the target; zero once the room is within tolerance."""
    return target - temp if temp < target - tolerance else 0.0


def pace(event, tolerance, delay):
    """Minutes per kelvin after the room's dead time ``delay``."""
    return max(0.0, event["minutes"] - delay) / effective_deficit(event, tolerance)


def _delay(events, now_ts):
    """Recency-weighted median dead time (0 when none recorded).

    Only climbs of DELAY_MIN_DEFICIT or more measure it: sensors report in 0.1 K steps, so
    on a small climb the first +0.1 K reading is often the arrival itself (live 13 Oct,
    Kitchen 18.9 -> 19.2 in one report) and the "delay" would swallow the whole warm-up.
    """
    pairs = [
        (e["delay_minutes"], recency_weight(e, now_ts))
        for e in events
        if e.get("delay_minutes") is not None and e["deficit"] >= DELAY_MIN_DEFICIT
    ]
    if not pairs:
        return 0.0
    return weighted_median([d for d, _ in pairs], [w for _, w in pairs])


def predicted_minutes(event, delay, pace_value, tolerance):
    return delay + pace_value * effective_deficit(event, tolerance)


def weighted_median(values, weights):
    """Median where each value counts by its weight (lower middle on an exact tie)."""
    pairs = sorted(zip(values, weights))
    total = sum(w for _, w in pairs)
    if not pairs or total <= 0:
        return None
    running = 0.0
    for value, weight in pairs:
        running += weight
        if running >= total / 2:
            return value
    return pairs[-1][0]


def recency_weight(event, now_ts):
    age_days = max(0.0, (now_ts - event["start"]) / 86400)
    return 0.5 ** (age_days / HALF_LIFE_DAYS)


def _plain(events, now_ts, tolerance):
    delay = _delay(events, now_ts)
    return weighted_median(
        [pace(e, tolerance, delay) for e in events], [recency_weight(e, now_ts) for e in events]
    )


def _line(points):
    """Least-squares (a, b) for y = a + b x, None when x does not vary."""
    n = len(points)
    mx = sum(x for x, _ in points) / n
    my = sum(y for _, y in points) / n
    sxx = sum((x - mx) ** 2 for x, _ in points)
    if sxx <= 1e-9:
        return None
    b = sum((x - mx) * (y - my) for x, y in points) / sxx
    return my - b * mx, b


def _x(event):
    return event["goal"] - event["outdoor"]


def fit_model(events, now_ts, tolerance=0.3):
    """The room's pace from its events (None with none), and whether outdoor helps."""
    events = list(events)
    if not events:
        return None
    base = _plain(events, now_ts, tolerance)
    errors = []
    for i, e in enumerate(events):
        rest = events[:i] + events[i + 1 :]
        guess = _plain(rest, now_ts, tolerance) if rest else None
        if guess is not None:
            errors.append(
                abs(predicted_minutes(e, _delay(rest, now_ts), guess, tolerance) - e["minutes"])
            )
    plain_error = statistics.fmean(errors) if errors else None
    plain_median = statistics.median(errors) if errors else None
    a = b = None
    chosen_error = plain_median
    with_outdoor = [e for e in events if isinstance(e.get("outdoor"), (int, float))]
    if len(with_outdoor) >= MIN_OUTDOOR_EVENTS and plain_error is not None:
        delay = _delay(with_outdoor, now_ts)
        fit = _line([(_x(e), pace(e, tolerance, delay)) for e in with_outdoor])
        if fit is not None and fit[1] > 0:
            loo = []
            for i, e in enumerate(with_outdoor):
                rest = with_outdoor[:i] + with_outdoor[i + 1 :]
                sub = _line([(_x(r), pace(r, tolerance, _delay(rest, now_ts))) for r in rest])
                if sub is None:
                    loo = None
                    break
                guess = max(MIN_PACE, sub[0] + sub[1] * _x(e))
                loo.append(
                    abs(predicted_minutes(e, _delay(rest, now_ts), guess, tolerance) - e["minutes"])
                )
            if loo and statistics.fmean(loo) < plain_error:
                a, b = fit
                chosen_error = statistics.median(loo)
    supplies = [e["supply"] for e in events if isinstance(e.get("supply"), (int, float))]
    return RoomModel(
        minutes_per_k=base,
        outdoor_a=a,
        outdoor_b=b,
        median_supply=statistics.median(supplies) if supplies else None,
        events=len(events),
        loo_error_min=None if chosen_error is None else round(chosen_error, 1),
        delay_minutes=round(_delay(events, now_ts), 1),
    )


def pace_at(model, target, outdoor):
    """minutes per kelvin for a warm-up to ``target`` in ``outdoor`` weather."""
    if model.outdoor_b is None or outdoor is None:
        return model.minutes_per_k
    return max(MIN_PACE, model.outdoor_a + model.outdoor_b * (target - outdoor))


def _drive_ratio(supply, flow, t_now, target):
    """Radiator drive at ``supply`` over drive at ``flow`` (power law 1.3), None if unusable."""
    if supply is None:
        return None
    ta = (t_now + target) / 2
    if supply <= ta + MIN_SUPPLY_MARGIN or flow - MEAN_WATER_DROP - ta <= 0:
        return None
    return ((supply - MEAN_WATER_DROP - ta) / (flow - MEAN_WATER_DROP - ta)) ** RADIATOR_EXPONENT


def boost_factor(supply, boost_flow, t_now, target):
    """Heating-time factor for a hotter flow: ratio of radiator drives, power law 1.3."""
    ratio = _drive_ratio(supply, boost_flow, t_now, target)
    if ratio is None:
        return None
    low, high = BOOST_FACTOR_BOUNDS
    return min(high, max(low, ratio))


def plan_factor(supply, plan_flow, t_now, target):
    """Pace factor for planning at ``plan_flow`` instead of the flow the pace was learned at.

    Above 1 when the plan flow is gentler than the room's median supply. 1 when either
    figure is missing: an unknown supply must not change the plan.
    """
    ratio = _drive_ratio(supply, plan_flow, t_now, target)
    if ratio is None:
        return 1.0
    low, high = PLAN_FACTOR_BOUNDS
    return min(high, max(low, ratio))


# -- start and stop -------------------------------------------------------


def predict_start(
    now, switch_at, target, t_now, model, outdoor_at, boost_flow, params, plan_flow=None
):
    """Latest start that arrives by the switchpoint, at the room's own observed pace.

    ``outdoor_at(datetime) -> °C`` (or None) feeds the outdoor term when the model has one;
    its mean over the predicted warm-up window is found by two fixed-point passes. With
    ``plan_flow`` the pace (not the dead time) is scaled to that flow by the radiator law,
    so the start allows for a gentle warm-up rather than the flow the events happened at.
    """
    need = climb(target, t_now, params.tolerance)
    scale = plan_factor(model.median_supply, plan_flow, t_now, target) if plan_flow else 1.0
    pace_now = model.minutes_per_k * scale
    used = False
    if model.outdoor_model and outdoor_at is not None and need > 0:
        for _ in range(2):
            span = max(1, round(model.delay_minutes + pace_now * need))
            samples = [outdoor_at(switch_at - timedelta(minutes=span * f / 4)) for f in range(5)]
            samples = [s for s in samples if s is not None]
            if not samples:
                break
            pace_now = pace_at(model, target, sum(samples) / len(samples)) * scale
            used = True
    heating = model.delay_minutes + pace_now * need if need > 0 else 0.0
    lead = math.ceil(heating + params.margin_minutes) if heating > 0 else 0
    total = min(params.max_lead_minutes, lead)
    # The boost what-if is measured from the flow the pace now stands for.
    reference = plan_flow if plan_flow and scale != 1.0 else model.median_supply
    factor = boost_factor(reference, boost_flow, t_now, target) if heating > 0 else None
    boost_lead = boost_late = boost_gain = None
    if factor is not None:
        boost_total = math.ceil(
            model.delay_minutes + pace_now * need * factor + params.margin_minutes
        )
        boost_lead = min(params.max_lead_minutes, boost_total)
        boost_late = max(0, boost_total - params.max_lead_minutes)
        boost_gain = lead - boost_total
    return StartPrediction(
        start_at=switch_at - timedelta(minutes=total),
        lead_minutes=total,
        heating_minutes=round(heating, 1),
        minutes_per_k=round(pace_now, 2),
        delay_minutes=model.delay_minutes if need > 0 else 0.0,
        late_minutes=max(0, lead - params.max_lead_minutes),
        boost_lead_minutes=boost_lead,
        boost_late_minutes=boost_late,
        boost_gain_minutes=boost_gain,
        outdoor_used=used,
        base_minutes_per_k=round(pace_now / scale, 2),
        plan_factor=round(scale, 3),
    )


def predict_stop(switch_at, rate, coast_drop, max_lead_minutes):
    """Heating may stop ``coast_drop / |rate|`` before the downward switchpoint."""
    minutes = min(max_lead_minutes, int(coast_drop / abs(rate) * 60))
    return StopPrediction(
        off_at=switch_at - timedelta(minutes=minutes), coast_minutes=minutes, rate=round(rate, 3)
    )


# -- schedule -------------------------------------------------------------


def _switchpoints_on(schedule, day, tzinfo):
    for entry in schedule if isinstance(schedule, list) else []:
        if not isinstance(entry, dict) or entry.get("day_of_week") != day.weekday():
            continue
        for sp in entry.get("switchpoints") or []:
            try:
                hour, minute = str(sp["time_of_day"]).split(":")[:2]
                at = datetime.combine(day, time(int(hour), int(minute)), tzinfo=tzinfo)
                yield at, float(sp["heat_setpoint"])
            except KeyError, TypeError, ValueError:
                continue


def next_switchpoints(schedule, now_local, count=2):
    """The next ``count`` switchpoints of a RAMSES weekly schedule after ``now_local``.

    ``schedule`` is ``[{"day_of_week": 0-6 (Monday 0), "switchpoints": [{"time_of_day":
    "HH:MM", "heat_setpoint": float}]}]``. Wall-clock times are resolved in the zone of
    ``now_local`` per day, so a clock change moves the absolute time, not the local one.
    """
    found = []
    for offset in range(8):
        day = now_local.date() + timedelta(days=offset)
        found.extend(
            (at, sp)
            for at, sp in _switchpoints_on(schedule, day, now_local.tzinfo)
            if at > now_local
        )
        if len(found) >= count:
            break
    return sorted(found, key=lambda item: item[0])[:count]


def setpoint_at(schedule, now_local):
    """The scheduled setpoint in force at ``now_local``: the latest switchpoint before it."""
    for offset in range(8):
        day = now_local.date() - timedelta(days=offset)
        past = [
            (at, sp)
            for at, sp in _switchpoints_on(schedule, day, now_local.tzinfo)
            if at <= now_local
        ]
        if past:
            return max(past, key=lambda item: item[0])[1]
    return None
