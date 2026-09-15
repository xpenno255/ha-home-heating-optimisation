"""Physical counterexamples and measurement/recovery boundary regressions."""

from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

from custom_components.home_heating_optimisation.analytics.analyzer import (
    ZoneStats,
    _episodes,
    _ramp,
    _response,
    compare_windows,
    compute_analytics,
)

BASE = datetime(2026, 9, 12, tzinfo=timezone.utc)


def point(minute, temp=20, target=20, active=False, others=None, **extra):
    time = (BASE + timedelta(minutes=minute)).timestamp()
    zone = {
        "temperature": temp,
        "target": target,
        "active": active,
        "demand": 0.8,
        "valid_until": time + 1800,
        "demand_valid_until": time + 1800,
        "temperature_updated": time,
        **extra,
    }
    return {
        "time": time,
        "zones": {"climate.a": zone, **(others or {})},
        "context": {"outdoor": 8, "supply": 50, "heating_active": True, "dhw_active": False},
    }


def result(points, days=1, end=1440, zones=None, **kwargs):
    return compute_analytics(
        points, zones or ["climate.a"], days, now=BASE + timedelta(minutes=end), **kwargs
    )


def test_full_day_duty_denominator_and_no_valve_advice():
    points = [point(m, temp=18 + min(m, 30) / 120, active=m < 30) for m in range(0, 1441, 5)]
    data = result(points)
    z = data.zone_stats["climate.a"]
    assert z.duty_cycle == 2.1
    assert z.coverage == 100
    assert not any("opening" in r or "restricting" in r for r in data.system.recommendations)


def test_partial_observation_is_not_full_coverage():
    z = result([point(0, active=True), point(30, active=False)]).zone_stats["climate.a"]
    assert z.coverage == 4.2  # 30 min observed demand plus 30 min freshness-limited idle.
    assert z.demand_coverage == 4.2
    assert z.duty_cycle == 50


def test_crossing_window_counts_overlap():
    points = [point(m, active=True) for m in range(-60, 31, 5)]
    z = result(points, end=1440).zone_stats["climate.a"]
    assert z.observed_hours == 1  # 00:00–01:00 incl. freshness tail, not before midnight.


def test_long_demand_never_disappears():
    z = result([point(m, active=True) for m in range(0, 1441, 5)]).zone_stats["climate.a"]
    assert z.duty_cycle == 100
    assert z.coverage == 100
    assert z.heating_rate_avg is None  # Maintenance is not a recovery ramp.


def test_cooling_room_is_visible_and_there_is_no_fake_balance_score():
    points = [point(m, temp=20 - m / 1440) for m in range(0, 1441, 5)]
    data = result(points)
    assert data.zone_stats["climate.a"].deficit_degree_hours > 2
    assert data.system.recommendations
    assert not hasattr(data.system, "balance_score")


def test_sustained_first_arrival_not_whole_demand_duration():
    points = [
        point(0, 18, active=False),
        point(5, 18, active=True),
        point(10, 19, active=True),
        point(15, 20, active=True),
        point(20, 20, active=True),
        point(35, 20, active=False),
    ]
    z = result(points).zone_stats["climate.a"]
    assert z.time_to_setpoint_avg == 10
    assert z.setpoint_achievement == 100


def test_setback_cancels_instead_of_succeeding():
    points = [point(0, 18), point(5, 18, active=True), point(30, 18.2, target=16)]
    z = result(points).zone_stats["climate.a"]
    assert z.setpoint_achievement is None
    assert z.cancelled_recoveries == 1


def test_afterheat_can_finish_recovery():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(15, 19.5),
        point(20, 20),
        point(25, 20),
    ]
    z = result(points).zone_stats["climate.a"]
    assert z.time_to_setpoint_avg == 15
    assert z.completed_recoveries == 1


def test_one_in_band_sample_is_not_success():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(10, 20),
        point(12, 19.5),
        point(15, 20),
    ]
    z = result(points).zone_stats["climate.a"]
    assert z.setpoint_achievement is None
    assert z.ongoing_recoveries == 1


def test_changed_target_and_missing_period_are_censored():
    points = [point(0, 18), point(5, 18, active=True), point(60, 19, active=True)]
    z = result(points).zone_stats["climate.a"]
    assert z.setpoint_achievement is None
    assert z.cancelled_recoveries == 2


def test_startup_while_heating_is_left_censored():
    z = result([point(0, 18, active=True), point(10, 20), point(15, 20)]).zone_stats["climate.a"]
    assert z.completed_recoveries == 0
    assert z.cancelled_recoveries == 1


def test_deadline_is_explicit_and_ongoing_is_not_failure():
    points = [point(0, 18)] + [point(m, 18, active=True) for m in range(5, 141, 5)]
    z = result(points, recovery_minutes=120).zone_stats["climate.a"]
    assert z.completed_recoveries == 1
    assert z.setpoint_achievement == 0
    assert z.heating_rate_avg == 0  # Zero slope must not disappear.


def test_demand_coverage_survives_failed_independent_temperature():
    points = [point(m, temp=None, active=True, valid_until=0) for m in range(0, 1441, 5)]
    z = result(points).zone_stats["climate.a"]
    assert z.demand_coverage == 100
    assert z.duty_cycle == 100
    assert z.coverage == 0


def test_local_morning_summer_and_winter():
    points = [
        point(265, 18),
        point(270, 18, active=True),
        point(275, 18.5, active=True),
        point(280, 19, active=True),
        point(285, 20, active=True),
        point(290, 20, active=True),
    ]
    z = result(points, timezone_name="Europe/London").zone_stats["climate.a"]
    assert z.total_morning_sessions == 1  # 04:30 UTC is 05:30 BST.
    z = result(points, timezone_name="UTC").zone_stats["climate.a"]
    assert z.total_morning_sessions == 0


def test_empty_comparison_is_not_unchanged():
    data = result([])
    assert compare_windows(data, data).summary == "Insufficient data for comparison"


def test_daily_changes_are_descriptive_and_windows_exact():
    a = result([point(m) for m in range(0, 1441, 5)])
    b = result([point(m, temp=18) for m in range(-1440, 1, 5)], end=0)
    comparison = compare_windows(a, b)
    assert comparison.previous_end == comparison.current_start
    assert comparison.zone_comparisons["climate.a"]["within_band_change_percentage_points"] == 100
    assert all(
        "efficient" not in r and "Adjustment helping" not in r for r in comparison.recommendations
    )


def ramp(day, exposure, rate=1, **extra):
    return {
        "start": (BASE + timedelta(days=day)).timestamp(),
        "rate": rate,
        "start_temp": 18,
        "deficit": 2,
        "supply": 50,
        "outdoor": 8,
        "demand": 0.8,
        "exposure": exposure,
        "comparable": True,
        **extra,
    }


def test_matched_zero_response_is_zero_and_requires_independent_days():
    zs = ZoneStats("a")
    ramps = [ramp(d, "alone") for d in range(5)] + [ramp(d, "loaded", 0) for d in range(5)]
    _response(zs, ramps, ZoneInfo("UTC"))
    assert zs.response_ratio == 0
    assert zs.matched_pairs == 5
    assert zs.response_interval == [-1, -1]


@pytest.mark.parametrize(
    "change",
    [
        {"supply": 60},
        {"outdoor": 15},
        {"deficit": 3},
        {"start_temp": 21},
        {"demand": 0.2},
        {"comparable": False},
    ],
)
def test_unmatched_conditions_cannot_produce_response(change):
    zs = ZoneStats("a")
    _response(
        zs,
        [ramp(d, "alone") for d in range(5)] + [ramp(d, "loaded", 0, **change) for d in range(5)],
        ZoneInfo("UTC"),
    )
    assert zs.response_ratio is None


def test_multiple_samples_on_one_day_are_not_independent():
    zs = ZoneStats("a")
    _response(
        zs,
        [ramp(0, "alone") for _ in range(5)] + [ramp(0, "loaded", 0) for _ in range(5)],
        ZoneInfo("UTC"),
    )
    assert zs.response_ratio is None


def test_unknown_context_still_allows_descriptive_recovery_rate():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(10, 18.5, active=True),
        point(15, 19, active=True),
        point(20, 20, active=True),
        point(25, 20, active=True),
    ]
    for p in points:
        p["context"]["supply"] = None
    z = result(points).zone_stats["climate.a"]
    assert z.heating_rate_avg is not None
    assert z.response_ratio is None


def test_sequential_other_demands_use_time_exposure_not_union():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(10, 18.5, active=True),
        point(15, 19, active=True),
        point(20, 20, active=True),
        point(25, 20, active=True),
    ]
    for i, p in enumerate(points):
        for zone in ["climate.b", "climate.c"]:
            p["zones"][zone] = {**p["zones"]["climate.a"], "active": False}
        if i == 1:
            p["zones"]["climate.b"]["active"] = True
        if i == 3:
            p["zones"]["climate.c"]["active"] = True
    episode = _episodes(points, "climate.a", 0.3, 7200)[0]
    r = _ramp(episode, "climate.a")
    assert r["exposure"] == "mixed"


def test_physical_counterexample_identical_heat_different_mass():
    assert [(2000 - 1000) * 3600 / c for c in [2e6, 8e6]] == [1.8, 0.45]


def test_logged_settings_changes_are_not_matched_across_regimes():
    zs = ZoneStats("a")
    _response(
        zs,
        [ramp(d, "alone", regime=0) for d in range(5)]
        + [ramp(d, "loaded", 0, regime=1) for d in range(5)],
        ZoneInfo("UTC"),
    )
    assert zs.matched_pairs == 0
    assert zs.response_ratio is None


def test_fahrenheit_conversion_counterexample_does_not_change_rate():
    from homeassistant.core import State

    from custom_components.home_heating_optimisation.observations import read

    def temperature(value):
        state = State(
            "sensor.air",
            str(value),
            {"unit_of_measurement": "°F"},
            last_updated=BASE,
            last_reported=BASE,
        )
        return read({"sensor.air": state}, "sensor.air", BASE).value

    assert temperature(69.8) - temperature(68) == pytest.approx(1)


def test_expired_context_blocks_matching_without_losing_descriptive_rate():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(10, 18.5, active=True),
        point(15, 19, active=True),
        point(20, 20, active=True),
        point(25, 20, active=True),
    ]
    episode = _episodes(points, "climate.a", 0.3, 7200)[0]
    baseline = _ramp(episode, "climate.a")
    assert baseline["comparable"]
    for p in points:
        p["context_valid_until"] = {"dhw_active": p["time"] + 60}
    actual = _ramp(episode, "climate.a")
    assert not actual["comparable"]
    assert actual["rate"] == baseline["rate"]


def test_other_room_demand_validity_is_independent_of_temperature():
    points = [
        point(0, 18),
        point(5, 18, active=True),
        point(10, 18.5, active=True),
        point(15, 19, active=True),
        point(20, 20, active=True),
        point(25, 20, active=True),
    ]
    for p in points:
        p["zones"]["climate.b"] = {
            "active": False,
            "valid_until": 0,
            "demand_valid_until": p["time"] + 1800,
        }
    episode = _episodes(points, "climate.a", 0.3, 7200)[0]
    assert _ramp(episode, "climate.a")["comparable"]
    for p in points:
        p["zones"]["climate.b"]["demand_valid_until"] = p["time"] + 60
    assert not _ramp(episode, "climate.a")["comparable"]


def test_recovery_after_unknown_demand_start_is_censored():
    points = [
        point(0, 18, active=None),
        point(5, 18, active=True),
        point(10, 20, active=True),
        point(15, 20, active=True),
    ]
    z = result(points).zone_stats["climate.a"]
    assert z.completed_recoveries == 0
    assert z.cancelled_recoveries == 1
