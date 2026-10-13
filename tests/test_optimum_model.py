"""Optimum start / stop: empirical model, event learning, forecast and scoring (pure)."""

import math
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from custom_components.home_heating_optimisation.optimum.forecast import (
    STALE,
    Forecast,
    parse_forecast,
)
from custom_components.home_heating_optimisation.optimum.learning import (
    cooldowns,
    fit_room,
    merge_events,
    warmup_events,
)
from custom_components.home_heating_optimisation.optimum.model import (
    StartParams,
    boost_factor,
    climb,
    fit_model,
    next_switchpoints,
    predict_start,
    predict_stop,
    setpoint_at,
    weighted_median,
)
from custom_components.home_heating_optimisation.optimum.policy import (
    optimum_config,
    validation_error,
)
from custom_components.home_heating_optimisation.optimum.scoring import rolling, score_start

T0 = datetime(2026, 1, 15, 4, 0, tzinfo=timezone.utc)
BASE = datetime(2026, 9, 12, tzinfo=timezone.utc)
DAY = 86400
PARAMS = {"tolerance": 0.3, "timezone": "Europe/London", "min_warmups": 2, "min_cooldowns": 3}


def point(minute, temp, target=20, zone="z", outdoor=5.0, supply=45.0, active=False, dhw=False):
    time = (BASE + timedelta(minutes=minute)).timestamp()
    z = {
        "temperature": temp,
        "target": target,
        "active": active,
        "demand": 0.8 if active else 0.0,
        "valid_until": time + 1800,
        "demand_valid_until": time + 1800,
        "temperature_updated": time,
    }
    return {
        "time": time,
        "zones": {zone: z},
        "context": {
            "outdoor": outdoor,
            "supply": supply,
            "heating_active": active,
            "dhw_active": dhw,
        },
    }


def warmup(start, t0, goal, minutes, outdoor=5.0, supply=45.0, hold=30, zone="z", low=16.0):
    """Observations every 5 minutes: target steps up at ``start`` and the room follows it."""
    points = [point(start - 5, t0, low, zone, outdoor, supply)]
    for m in range(0, int(minutes) + hold + 1, 5):
        temp = t0 + (goal - t0) * min(1.0, m / minutes)
        points.append(point(start + m, temp, goal, zone, outdoor, supply, active=m < minutes))
    return points


def event(start_day, deficit, minutes, goal=21.0, outdoor=5.0, supply=45.0):
    return {
        "start": (BASE + timedelta(days=start_day)).timestamp(),
        "t0": goal - deficit,
        "goal": goal,
        "deficit": deficit,
        "minutes": minutes,
        "outdoor": outdoor,
        "supply": supply,
        "hour": 6,
    }


# -- warm-up detection ------------------------------------------------------


def test_detects_a_warmup_with_its_conditions():
    found = warmup_events(warmup(60, 18.0, 21.0, 90, outdoor=4.0, supply=50.0), "z", 0.3)
    assert len(found) == 1
    e = found[0]
    assert e["deficit"] == 3.0 and e["goal"] == 21.0 and e["t0"] == 18.0
    # Arrival is the first sample within 0.3 K of the goal: 90 minutes, less 0.3 K of the climb.
    assert 80 <= e["minutes"] <= 90
    assert e["outdoor"] == 4.0 and e["supply"] == 50.0


def test_small_target_nudges_are_ignored():
    points = [point(m, 18.0 + m * 0.02, 20.0 + m * 0.01) for m in range(0, 300, 5)]
    assert warmup_events(points, "z", 0.3) == []
    # A 0.5 K rise with the room already at target is not a warm-up either.
    warm = [point(0, 20.0, 19.5), point(5, 20.0, 20.0), point(10, 20.0, 20.0)]
    assert warmup_events(warm, "z", 0.3) == []


def test_a_stepped_rise_counts_once_at_the_final_goal():
    # Live 13 Oct, Kitchen: schedule 18.0 -> 19.0 at 06:31 (room 18.9, so not yet short),
    # HHO's 19.3 a minute later; the room arrived (19.0) 78 minutes after that.
    points = [point(0, 18.9, 18.0), point(1, 18.9, 19.0), point(2, 18.9, 19.3)]
    for m in range(5, 120, 5):
        points.append(point(m, 18.9 if m < 80 else 19.1, 19.3, active=True))
    events = warmup_events(points, "z", 0.3)
    assert len(events) == 1
    assert events[0]["goal"] == 19.3 and events[0]["t0"] == 18.9
    assert events[0]["start"] == points[2]["time"] and events[0]["minutes"] == 78.0


def test_legacy_scores_are_left_out_of_the_rolling_figures():
    legacy = {"evohome_lateness_min": -175.2, "model_error_min": None}
    scored = {"outcome": "scored", "evohome_lateness_min": 20.0, "model_error_min": -7.0}
    assert rolling([legacy, scored]) == {
        "scored": 1,
        "median_model_error_min": 7.0,
        "median_evohome_lateness_min": 20.0,
    }


def test_later_target_changes_are_ignored_but_a_drop_abandons():
    points = warmup(60, 18.0, 21.0, 90)
    # The target creeps up 0.2 K during the warm-up: still one event at the original goal.
    for p in points:
        if p["time"] > points[10]["time"]:
            p["zones"]["z"]["target"] += 0.2
    assert len(warmup_events(points, "z", 0.3)) == 1
    dropped = warmup(60, 18.0, 21.0, 90)
    for p in dropped:
        if p["time"] > dropped[8]["time"]:
            p["zones"]["z"]["target"] = 16.0
    assert warmup_events(dropped, "z", 0.3) == []


def test_gap_and_timeout_abandon_and_hold_is_required():
    gap = warmup(60, 18.0, 21.0, 90)
    cut = next(i for i, p in enumerate(gap) if p["zones"]["z"]["temperature"] > 19.5)
    gap = gap[:cut] + gap[cut + 8 :]  # 40 minutes missing
    assert warmup_events(gap, "z", 0.3) == []
    assert warmup_events(warmup(60, 18.0, 21.0, 300), "z", 0.3) == []  # over 4 hours
    # The room touches the goal for one sample, then falls away: not arrived.
    touch = warmup(60, 18.0, 21.0, 90, hold=0)
    first = next(i for i, p in enumerate(touch) if p["zones"]["z"]["temperature"] >= 20.7)
    touch = touch[: first + 1]
    last = (touch[-1]["time"] - BASE.timestamp()) / 60
    touch += [point(last + 5 * k, 19.0, 21.0) for k in (1, 2, 3, 4)]
    assert warmup_events(touch, "z", 0.3) == []


def test_two_warmups_and_merge_dedupes():
    points = warmup(60, 18.0, 21.0, 90) + warmup(1500, 18.5, 21.0, 60)
    found = warmup_events(points, "z", 0.3)
    assert len(found) == 2
    assert len(merge_events(found, found)) == 2
    assert len(merge_events([event(d, 2.0, 60) for d in range(80)], [])) == 60


# -- model -------------------------------------------------------------------


def test_weighted_median_and_recency():
    assert weighted_median([1, 2, 3], [1, 1, 1]) == 2
    assert weighted_median([1, 2, 3], [5, 1, 1]) == 1
    now = (BASE + timedelta(days=40)).timestamp()
    old = [event(d, 2.0, 100) for d in range(0, 5)]  # 50 min per K, a month ago
    new = [event(d, 2.0, 40) for d in range(35, 38)]  # 20 min per K, this week
    model = fit_model([*old, *new], now)
    assert abs(model.minutes_per_k - 40 / 2.0) < 1e-6  # the recent events dominate
    assert fit_model([], now) is None


def test_outdoor_term_needs_eight_events_and_must_beat_the_median():
    now = (BASE + timedelta(days=30)).timestamp()
    # Colder mornings take longer: minutes per K = 6 + 2.5 * (goal - outdoor) / 10.
    cold = [
        event(d, 2.0, (6 + 0.25 * (21 - o)) * 2.0, outdoor=o) for d, o in enumerate(range(0, 16, 2))
    ]
    model = fit_model(cold, now)
    assert model.outdoor_model and model.outdoor_b > 0
    assert fit_model(cold[:7], now).outdoor_model is False
    # The same times with outdoor unrelated: the plain median wins.
    flat = [
        event(d, 2.0, 30 + (d % 3), outdoor=o) for d, o in enumerate([5, 12, 3, 9, 1, 14, 6, 10])
    ]
    assert fit_model(flat, now).outdoor_model is False
    # A wrong-way relationship (warmer outdoors slower) is never used.
    backwards = [
        event(d, 2.0, (6 + 0.25 * o) * 2.0, outdoor=o) for d, o in enumerate(range(0, 16, 2))
    ]
    assert fit_model(backwards, now).outdoor_model is False


def test_start_lead_is_pace_times_climb_plus_margin():
    now = (BASE + timedelta(days=30)).timestamp()
    events = [event(d, 2.0, 40.0) for d in range(25, 30)]  # 20 min per K of whole climb
    model = fit_model(events, now)
    params = StartParams(margin_minutes=10, max_lead_minutes=180)
    switch = T0 + timedelta(hours=3)
    pred = predict_start(T0, switch, 21.0, 18.0, model, None, 65.0, params)
    assert pred.heating_minutes == 20 * 3.0
    assert pred.lead_minutes == 70 and pred.late_minutes == 0
    assert pred.start_at == switch - timedelta(minutes=70)
    # Within tolerance of target: no climb needed.
    near = predict_start(T0, switch, 21.0, 20.8, model, None, 65.0, params)
    assert near.lead_minutes == 0
    capped = predict_start(T0, switch, 21.0, 10.0, model, None, 65.0, StartParams(10, 60))
    assert capped.lead_minutes == 60 and capped.late_minutes > 0
    warm = predict_start(T0, switch, 21.0, 21.0, model, None, 65.0, params)
    assert warm.lead_minutes == 0 and warm.start_at == switch


def test_boost_factor_and_what_if():
    # Supply 55, ambient (18 + 21) / 2 = 19.5: drive 30.5 vs 40.5 at 65 degrees.
    factor = boost_factor(55.0, 65.0, 18.0, 21.0)
    assert math.isclose(factor, (30.5 / 40.5) ** 1.3)
    # The pulsing-burner mean overstates a boost: never more than twice the pace.
    assert boost_factor(45.0, 65.0, 18.0, 21.0) == 0.5
    assert boost_factor(None, 65.0, 18.0, 21.0) is None
    assert boost_factor(24.0, 65.0, 18.0, 21.0) is None  # supply not above ambient + 5
    assert 0.5 <= boost_factor(64.9, 65.0, 18.0, 21.0) <= 1.0
    now = (BASE + timedelta(days=30)).timestamp()
    model = fit_model([event(d, 2.0, 34.0, supply=45.0) for d in range(25, 30)], now)
    pred = predict_start(T0, T0 + timedelta(hours=3), 21.0, 18.0, model, None, 65.0, StartParams())
    assert pred.boost_lead_minutes < pred.lead_minutes
    assert pred.boost_gain_minutes == pred.lead_minutes - pred.boost_lead_minutes
    unknown = fit_model([event(d, 2.0, 34.0, supply=None) for d in range(25, 30)], now)
    none = predict_start(
        T0, T0 + timedelta(hours=3), 21.0, 18.0, unknown, None, 65.0, StartParams()
    )
    assert none.boost_lead_minutes is None and none.boost_gain_minutes is None


def test_outdoor_forecast_lengthens_a_cold_morning():
    now = (BASE + timedelta(days=30)).timestamp()
    events = [
        event(d, 2.0, (6 + 0.25 * (21 - o)) * 2.0, outdoor=o) for d, o in enumerate(range(0, 16, 2))
    ]
    model = fit_model(events, now)
    params = StartParams(margin_minutes=0, max_lead_minutes=240)
    switch = T0 + timedelta(hours=4)
    cold = predict_start(T0, switch, 21.0, 18.0, model, lambda _w: 0.0, 65.0, params)
    mild = predict_start(T0, switch, 21.0, 18.0, model, lambda _w: 12.0, 65.0, params)
    assert cold.outdoor_used and cold.lead_minutes > mild.lead_minutes


def test_stop_gating_and_prediction():
    params = dict(PARAMS)

    # Evening (UTC+1 in September: 19:00 local) cool-downs falling 0.5 K/h.
    def evening(day):
        return [
            point(day * 1440 + 18 * 60 + m, 20.0 - 0.5 * m / 60, target=16)
            for m in range(0, 121, 5)
        ]

    points = [p for d in range(4) for p in evening(d)]
    fit, _ = fit_room(points, "z", params)
    assert fit.coast_rate is not None and math.isclose(fit.coast_rate, -0.5, rel_tol=0.1)
    stop = predict_stop(T0, fit.coast_rate, 0.3, 180)
    assert stop.coast_minutes == 36 and stop.off_at == T0 - timedelta(minutes=36)
    assert predict_stop(T0, -0.1, 0.3, 120).coast_minutes == 120  # capped
    few, _ = fit_room(points[:50], "z", params)
    assert few.coast_rate is None and few.coast_reason == "insufficient cool-downs"
    # A house holding its heat (slope above -0.02 K/h) gives no stop prediction.
    holding = [
        p
        for d in range(4)
        for p in [
            point(d * 1440 + 1080 + m, 20.0 - 0.005 * m / 60, target=16) for m in range(0, 121, 5)
        ]
    ]
    held, _ = fit_room(holding, "z", params)
    assert held.coast_rate is None and held.coast_reason == "house holding heat"
    # Daytime cool-downs (sun-warmed) do not count.
    day = [
        point(d * 1440 + 600 + m, 20.0 - 0.5 * m / 60, target=16)
        for d in range(4)
        for m in range(0, 121, 5)
    ]
    assert fit_room(day, "z", params)[0].coast_reason == "insufficient cool-downs"
    assert len(cooldowns(points, "z")) == 4


def test_fit_room_status_and_confidence():
    points = [p for d in range(3) for p in warmup(d * 1440 + 360, 18.0, 21.0, 90)]
    fit, events = fit_room(points, "z", PARAMS)
    assert fit.status == "ready" and fit.events == 3 and fit.confidence == "low"
    assert fit.model.minutes_per_k > 0 and len(fit.last_events) == 3
    learning, _ = fit_room(points[:30], "z", PARAMS)
    assert learning.status == "learning" and learning.model is None
    stored, _ = fit_room([], "z", PARAMS, events)  # stored events outlast the observations
    assert stored.status == "ready" and stored.events == 3


SCHEDULE = [
    {
        "day_of_week": 0,
        "switchpoints": [
            {"time_of_day": "06:30", "heat_setpoint": 21.0},
            {"time_of_day": "22:00", "heat_setpoint": 16.0},
        ],
    },
    {"day_of_week": 1, "switchpoints": [{"time_of_day": "06:30", "heat_setpoint": 21.5}]},
    {"day_of_week": 6, "switchpoints": [{"time_of_day": "08:00", "heat_setpoint": 20.0}]},
]
LONDON = ZoneInfo("Europe/London")


def test_next_switchpoints_wrap_midnight_and_week():
    sunday_late = datetime(2026, 1, 11, 23, 0, tzinfo=LONDON)  # Sunday
    nxt = next_switchpoints(SCHEDULE, sunday_late, 2)
    assert [(a.weekday(), a.hour, a.minute, sp) for a, sp in nxt] == [
        (0, 6, 30, 21.0),
        (0, 22, 0, 16.0),
    ]
    sat = datetime(2026, 1, 17, 12, 0, tzinfo=LONDON)  # Saturday, no entry: wraps to Sunday
    assert next_switchpoints(SCHEDULE, sat, 1)[0][0].weekday() == 6
    assert setpoint_at(SCHEDULE, datetime(2026, 1, 12, 7, 0, tzinfo=LONDON)) == 21.0
    # Monday 05:00 is still under Sunday's last switchpoint.
    assert setpoint_at(SCHEDULE, datetime(2026, 1, 12, 5, 0, tzinfo=LONDON)) == 20.0
    assert next_switchpoints(None, sat) == []


def test_next_switchpoints_across_dst():
    # UK clocks go back on 25 Oct 2026: the 06:30 local switchpoint moves by an hour in UTC.
    sched = [
        {"day_of_week": d, "switchpoints": [{"time_of_day": "06:30", "heat_setpoint": 21.0}]}
        for d in range(7)
    ]
    before = next_switchpoints(sched, datetime(2026, 10, 23, 12, 0, tzinfo=LONDON), 1)[0][0]
    after = next_switchpoints(sched, datetime(2026, 10, 25, 12, 0, tzinfo=LONDON), 1)[0][0]
    assert before.astimezone(timezone.utc).hour == 5
    assert after.astimezone(timezone.utc).hour == 6


def test_forecast_interpolates_and_falls_back():
    f = Forecast()
    now = T0
    points = parse_forecast(
        {
            "weather.x": {
                "forecast": [
                    {"datetime": "2026-01-15T05:00:00+00:00", "temperature": 4.0},
                    {"datetime": "2026-01-15T06:00:00+00:00", "temperature": 8.0},
                    {"datetime": "bad", "temperature": 1},
                ]
            }
        },
        "weather.x",
    )
    assert len(points) == 2
    assert f.outdoor_function(now, 3.0)[1] == "current"  # nothing cached yet
    f.update(points, now)
    at, source = f.outdoor_function(now, 3.0)
    assert source == "forecast"
    assert at(datetime(2026, 1, 15, 5, 30, tzinfo=timezone.utc)) == 6.0
    assert at(T0) == 4.0 and at(T0 + timedelta(hours=9)) == 8.0
    stale_now = now + STALE + timedelta(minutes=1)
    assert f.outdoor_function(stale_now, 3.0)[1] == "current"
    assert f.due(stale_now)
    assert parse_forecast(None, "weather.x") == []
    assert (
        parse_forecast(
            {"weather.x": {"forecast": [{"datetime": "2026-01-15T05:00:00", "temperature": 50}]}},
            "weather.x",
            True,
        )[0][1]
        == 10.0
    )


def test_config_defaults_and_validation():
    cfg = optimum_config({})
    assert cfg["enabled"] is False and cfg["max_lead_minutes"] == 180 and cfg["boost_flow"] == 65.0
    assert optimum_config({"optimum": {"max_lead_minutes": 5000}})["max_lead_minutes"] == 180
    assert validation_error(cfg) is None
    assert validation_error({**cfg, "boost_flow": 100}) == "optimum_invalid_value"
    assert validation_error({**cfg, "coast_drop": float("nan")}) == "optimum_invalid_value"


# -- scoring ---------------------------------------------------------------


def test_score_start_against_evohome():
    switch = (BASE + timedelta(hours=7)).timestamp()

    def t(m):
        return switch + m * 60

    samples = []
    for m in range(-120, 121, 5):
        temp = 17.0 if m < -60 else min(21.0, 17.0 + (m + 60) * 0.05)
        setpoint = 21.0 if m >= -60 else 16.0
        samples.append([t(m), temp, setpoint, 5.0])
    record = {
        "switch_ts": switch,
        "target": 21.0,
        "mpk": 18.0,
        "median_supply": 45.0,
        "boost_flow": 65.0,
        "samples": samples,
        "outdoor_source": "current",
        "forecast_mean": None,
        "frozen": {"start_ts": t(-70), "lead_minutes": 70},
    }
    score = score_start(record)
    assert score["evohome_start_censored"] is False
    assert score["evohome_lateness_min"] is not None
    # Predicted heating from 17 K to 20.7 K at 18 min/K, against 74 minutes actual.
    assert isinstance(score["model_error_min"], float)
    assert score["boost_lateness_estimate_min"] < score["evohome_lateness_min"] + 70
    assert score["hho_lateness_estimate_min"] is not None
    assert score["forecast_error_k"] is None


# -- response delay -----------------------------------------------------------


def lagged(start, t0, goal, delay, minutes):
    """A warm-up whose room only starts to respond ``delay`` minutes after the target rises."""
    points = [point(start - 5, t0, 16.0)]
    for m in range(0, int(minutes) + 31, 5):
        frac = max(0.0, min(1.0, (m - delay) / (minutes - delay)))
        points.append(point(start + m, t0 + (goal - t0) * frac, goal))
    return points


def test_delay_is_time_to_first_response():
    found = warmup_events(lagged(60, 18.0, 19.0, 40, 64), "z", 0.3)
    assert len(found) == 1
    assert (
        40 <= found[0]["delay_minutes"] <= 50 and found[0]["delay_minutes"] <= found[0]["minutes"]
    )


def test_delay_and_pace_prediction_for_small_climbs():
    now = (BASE + timedelta(days=30)).timestamp()
    events = [
        {**event(27, 0.5, 64.0, goal=19.0), "delay_minutes": 40.0},
        {**event(28, 1.1, 82.0, goal=19.0), "delay_minutes": 40.0},
        {**event(29, 0.9, 88.0, goal=19.0), "delay_minutes": 42.0},
    ]
    model = fit_model(events, now)
    # Only climbs of 0.8 K or more measure the dead time (0.1 K sensor steps): 40 and 42.
    assert model.delay_minutes == 42.0
    # Pace after that dead time: 44, 36 and 51 minutes per kelvin.
    assert 35 < model.minutes_per_k < 52
    params = StartParams(margin_minutes=10, max_lead_minutes=180)
    switch = T0 + timedelta(hours=3)
    pred = predict_start(T0, switch, 19.3, 18.9, model, None, 65.0, params)
    assert pred.delay_minutes == 42.0
    assert math.isclose(pred.heating_minutes, 42 + model.minutes_per_k * 0.4, abs_tol=0.1)
    assert pred.lead_minutes == math.ceil(pred.heating_minutes + 10)
    # Room already within tolerance of the goal: nothing to start, no dead time either.
    done = predict_start(T0, switch, 19.3, 19.1, model, None, 65.0, params)
    assert done.lead_minutes == 0 and done.heating_minutes == 0
    assert climb(19.3, 19.1, 0.3) == 0.0


def test_stored_events_without_a_delay_count_none():
    now = (BASE + timedelta(days=30)).timestamp()
    old = [event(d, 2.0, 40.0) for d in range(20, 24)]
    assert fit_model(old, now).delay_minutes == 0.0
    assert math.isclose(fit_model(old, now).minutes_per_k, 20.0)
    mixed = [*old, {**event(29, 2.0, 70.0), "delay_minutes": 30.0}]
    model = fit_model(mixed, now)
    assert model.delay_minutes == 30.0  # only the event that recorded one counts
    # A small climb never measures the dead time: its first +0.1 K is often the arrival.
    small = [*old, {**event(29, 0.5, 70.0), "delay_minutes": 70.0}]
    assert fit_model(small, now).delay_minutes == 0.0


# -- scoring replays of 13 Oct ------------------------------------------------

SWITCH = (BASE + timedelta(hours=7)).timestamp()


def at(m):
    return SWITCH + m * 60


def replay(temp, setpoint, **extra):
    """Samples every 5 minutes from 120 minutes before the switchpoint to 120 after."""
    samples = [[at(m), temp(m), setpoint(m), 5.0] for m in range(-120, 121, 5)]
    return {
        "switch_ts": SWITCH,
        "target": 19.3,
        "schedule_target": 19.0,
        "mpk": 30.0,
        "delay_minutes": 40.0,
        "median_supply": 45.0,
        "boost_flow": 65.0,
        "samples": samples,
        "outdoor_source": "current",
        "forecast_mean": None,
        "frozen": {"start_ts": at(0), "lead_minutes": 0, "frozen_at": at(4)},
        **extra,
    }


def slow_rise(m):
    return 18.9 + max(0, m - 40) * 0.025


def test_score_not_needed_when_already_at_target():
    record = replay(lambda m: 19.2, lambda m: 19.0 if m >= 0 else 18.0)
    score = score_start(record)
    assert score["outcome"] == "not_needed"
    assert score["evohome_lateness_min"] is None and score["model_error_min"] is None
    assert score["hho_lateness_estimate_min"] is None
    assert rolling([score])["scored"] == 0


def test_score_evohome_does_nothing_early_and_ignores_pre_switch_samples():
    # Two warm samples before the switchpoint (the room then cooled to 18.9): not arrival.
    def temp(m):
        return 19.2 if -60 <= m <= -55 else 18.9 if m <= 40 else slow_rise(m)

    score = score_start(replay(temp, lambda m: 19.0 if m >= 0 else 18.0))
    assert score["outcome"] == "scored"
    assert score["evohome_early"] is False
    assert score["evohome_start"] == datetime.fromtimestamp(SWITCH, timezone.utc).isoformat()
    assert score["reached"] == datetime.fromtimestamp(at(45), timezone.utc).isoformat()
    assert score["evohome_lateness_min"] == 45.0
    # Predicted 40 + 30 * 0.4 = 52 minutes against 45 actual.
    assert score["model_error_min"] == 7.0
    # Frozen at 4 minutes past its (lead 0) start: judged from the climb at that moment.
    assert score["hho_lateness_estimate_min"] == 56.0
    assert score["boost_lateness_estimate_min"] < 52.0
    assert rolling([score])["scored"] == 1


def test_score_when_evohome_raises_the_zone_early():
    score = score_start(
        replay(lambda m: 18.9 if m <= 40 else slow_rise(m), lambda m: 19.0 if m >= -60 else 18.0)
    )
    assert score["evohome_early"] is True
    assert score["evohome_start"] == datetime.fromtimestamp(at(-60), timezone.utc).isoformat()
    assert score["evohome_lateness_min"] == 45.0


def test_closing_follows_the_schedule_target_not_the_goal():
    from custom_components.home_heating_optimisation.optimum.coordinator import Optimum

    rec = {"schedule_target": 19.0, "target": 19.3, "switch_ts": SWITCH}
    now = datetime.fromtimestamp(SWITCH + 40 * 60, timezone.utc)
    assert Optimum._closing(rec, now, 19.0) is False
    assert Optimum._closing(rec, now, 18.0) is True
