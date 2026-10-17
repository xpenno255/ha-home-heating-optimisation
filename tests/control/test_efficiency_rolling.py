"""Rolling heat-weighted efficiency: buckets, windows, persistence and sensors."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.efficiency import (
    BUCKET_SECONDS,
    MAX_ELAPSED_SECONDS,
    add_sample,
    aggregate,
    dump_buckets,
    load_buckets,
    prune,
)
from custom_components.home_heating_optimisation.control.entities import (
    BoilerEfficiencySensor,
    BoilerRollingEfficiencySensor,
)
from tests.control.test_boiler_efficiency import configured, report  # noqa: F401

T0 = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def test_higher_power_counts_more():
    buckets = {}
    add_sample(buckets, T0, 60, 80, 92.0, "heating")
    add_sample(buckets, T0 + timedelta(minutes=1), 60, 20, 88.0, "heating")
    result = aggregate(buckets, T0 + timedelta(minutes=2), DAY)
    # Weights 80 x 60 and 20 x 60: (92 x 4 + 88) / 5.
    assert result["percent"] == pytest.approx(91.2)
    assert result["firing_minutes"] == 2.0


def test_gaps_count_at_most_the_cap_and_idle_adds_nothing():
    buckets = {}
    add_sample(buckets, T0, 3600, 50, 90.0, "heating")  # a one hour gap
    assert aggregate(buckets, T0, DAY)["firing_minutes"] == MAX_ELAPSED_SECONDS / 60
    add_sample(buckets, T0, 0, 50, 90.0, "heating")
    add_sample(buckets, T0, 60, 0, 90.0, "heating")
    add_sample(buckets, T0, 60, 50, 90.0, "bogus")
    assert aggregate(buckets, T0, DAY)["firing_minutes"] == MAX_ELAPSED_SECONDS / 60


def test_heating_and_dhw_are_split_and_combined():
    buckets = {}
    add_sample(buckets, T0, 60, 50, 92.0, "heating")
    add_sample(buckets, T0, 60, 50, 86.0, "dhw")
    result = aggregate(buckets, T0, DAY)
    assert (result["heating_percent"], result["dhw_percent"], result["percent"]) == (
        92.0,
        86.0,
        89.0,
    )
    only_heat = {}
    add_sample(only_heat, T0, 60, 50, 92.0, "heating")
    assert aggregate(only_heat, T0, DAY)["dhw_percent"] is None


def test_windows_and_pruning():
    buckets = {}
    add_sample(buckets, T0 - 3 * DAY, 60, 50, 80.0, "heating")
    add_sample(buckets, T0 - timedelta(hours=1), 60, 50, 90.0, "heating")
    add_sample(buckets, T0 - 8 * DAY, 60, 50, 70.0, "heating")
    assert aggregate(buckets, T0, DAY)["percent"] == 90.0
    assert aggregate(buckets, T0, 7 * DAY)["percent"] == 85.0
    prune(buckets, T0)
    assert len(buckets) == 2 and all(k % BUCKET_SECONDS == 0 for k in buckets)
    assert aggregate({}, T0, DAY) == {
        "percent": None,
        "heating_percent": None,
        "dhw_percent": None,
        "firing_minutes": 0.0,
    }


def test_persistence_round_trip_ignores_bad_rows():
    buckets = {}
    add_sample(buckets, T0, 60, 50, 90.0, "heating")
    add_sample(buckets, T0 + timedelta(minutes=10), 60, 30, 85.0, "dhw")
    assert load_buckets(dump_buckets(buckets)) == buckets
    assert load_buckets(None) == {}
    assert load_buckets([[1, 2], "x", [1, 0, 0, 0, 0, 0, float("nan")]]) == {}


@pytest.mark.asyncio
async def test_burn_is_bucketed_and_last_burn_shown_after_it_ends(hass, freezer):
    c, _ = await configured(hass)
    controls = SimpleNamespace(boiler=c, entry=c._entry, registry_id=lambda s, k: f"sensor.t_{k}")
    day = BoilerRollingEfficiencySensor(controls, "24h", "24 hours")
    week = BoilerRollingEfficiencySensor(controls, "7d", "7 days")
    realtime = BoilerEfficiencySensor(controls)
    await c.async_refresh()
    assert day.native_value is None and week.native_value is None  # startup, no valid firing
    assert day.extra_state_attributes["firing_minutes"] == 0.0
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()  # first valid sample: nothing to weigh yet
    assert day.native_value is None
    freezer.tick(timedelta(minutes=1))
    report(hass)
    await c.async_refresh()
    assert day.native_value == pytest.approx(90.1, abs=0.1)
    assert week.native_value == day.native_value
    attrs = day.extra_state_attributes
    assert attrs["heating_efficiency"] == day.native_value and attrs["dhw_efficiency"] is None
    assert attrs["firing_minutes"] == 1.0 and "not metered" in attrs["coverage"]
    assert load_buckets(c._store.get("efficiency_buckets")) == c._efficiency_buckets
    assert realtime.extra_state_attributes["last_burn_efficiency"] == day.native_value
    assert realtime.extra_state_attributes["last_burn_ended_at"] is None

    hass.states.async_set("binary_sensor.eff_active", "off")
    hass.states.async_set("sensor.eff_power", 0)
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=1))
    await c.async_refresh()
    # Realtime state is unknown between burns; the last burn is still shown.
    assert realtime.native_value is None
    attrs = realtime.extra_state_attributes
    assert attrs["last_burn_efficiency"] == pytest.approx(90.1, abs=0.1)
    assert attrs["last_burn_ended_at"] is not None
    assert day.native_value == pytest.approx(90.1, abs=0.1)
