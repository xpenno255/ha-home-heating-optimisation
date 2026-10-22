"""Coast-up learner: how far a room still rises after its demand ends at a warm-up."""

from custom_components.home_heating_optimisation.optimum.learning import (
    DEFAULT_COAST_UP_K,
    build_fit,
    coast_up,
    coast_ups,
)

PARAMS = {"tolerance": 0.3, "timezone": "UTC", "min_warmups": 2, "min_cooldowns": 3}


def obs(t, temp, active, target=20.0):
    return {
        "time": t,
        "zones": {"z": {"temperature": temp, "target": target, "active": active}},
        "context": {},
    }


def morning(day, rise=0.4, gap=False, drop=False, step=300):
    """Warm-up from t0 for 60 minutes (event), demand ends at +60, then a coast to +rise."""
    t0 = day * 86400 + 21600
    out = [obs(t0 + m * 60, 18 + m * 0.03, True) for m in range(0, 61, 5)]
    end_temp = out[-1]["zones"]["z"]["temperature"]
    out.append(obs(t0 + 3600 + step, end_temp, False))
    out.append(obs(t0 + 3600 + step + 60, end_temp + rise / 2, False))
    if gap:
        out.append(obs(t0 + 3600 + step + 40 * 60, end_temp + rise, False))
    else:
        out.append(obs(t0 + 3600 + 2 * step, end_temp + rise, False))
        out.append(obs(t0 + 3600 + 3 * step, end_temp + rise - 0.1, False))
    if drop:
        for o in out[-3:]:
            o["zones"]["z"]["target"] = 17.0
    # the demand-on sample before the transition is the last of the climb
    return [{**o, "time": o["time"]} for o in out], {"start": t0, "minutes": 55.0}


def run(*mornings):
    observations, events = [], []
    for day, kw in enumerate(mornings):
        o, e = morning(day, **kw)
        observations += o
        events.append(e)
    return observations, events


def test_rise_after_demand_ends_is_the_max_over_the_next_40_minutes():
    observations, events = run({"rise": 0.4})
    assert coast_ups(observations, "z", events) == [0.4]


def test_data_gap_and_target_drop_are_excluded():
    observations, events = run({"rise": 0.4}, {"gap": True}, {"drop": True})
    assert coast_ups(observations, "z", events) == [0.4]


def test_demand_that_never_ends_gives_no_sample():
    observations, events = run({})
    observations = [obs(o["time"], o["zones"]["z"]["temperature"], True) for o in observations]
    assert coast_ups(observations, "z", events) == []


def test_median_needs_three_samples_else_default():
    assert coast_up([0.6, 0.6]) == (DEFAULT_COAST_UP_K, 2)
    assert coast_up([0.2, 0.6, 0.4]) == (0.4, 3)
    observations, events = run({"rise": 0.2}, {"rise": 0.6}, {"rise": 0.4})
    rises = coast_ups(observations, "z", events)
    fit = build_fit([], [], PARAMS, 0, rises)
    assert fit.coast_up_k == 0.4 and fit.coast_up_samples == 3
    assert build_fit([], [], PARAMS, 0).coast_up_k == DEFAULT_COAST_UP_K
