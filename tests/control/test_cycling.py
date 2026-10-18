"""Burner cycling metrics and the cycling-adjusted efficiency estimate."""

from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.cycling import (
    START_LOSS_KWH,
    BurnTracker,
    add_burn,
    adjusted_efficiency,
    covered_days,
    fuel_fire_kwh,
    load_burns,
    prune,
    summarise,
)
from custom_components.home_heating_optimisation.control.entities import BoilerCyclingSensor
from tests.control.test_boiler_efficiency import configured, report  # noqa: F401

T0 = datetime(2026, 10, 17, 6, 0, tzinfo=timezone.utc)
DAY = timedelta(days=1)


def at(seconds):
    return T0 + timedelta(seconds=seconds)


def feed(tracker, readings, dhw=False):
    rows = []
    for seconds, power in readings:
        row = tracker.update(at(seconds), power, dhw)
        if row:
            rows.append(row)
    return rows


def test_burn_start_end_and_time_weighted_mean():
    rows = feed(BurnTracker(), [(0, 0), (10, 60), (70, 40), (130, 0)])
    # 60 % for 60 s then 40 % for 60 s, from the first reading above zero.
    assert rows == [[at(10).timestamp(), 120.0, 50.0, 0]]


def test_burn_mode_is_dhw_when_dhw_for_most_of_it():
    tracker = BurnTracker()
    feed(tracker, [(0, 0), (10, 50)])
    tracker.update(at(40), 50, True)
    tracker.update(at(70), 50, True)
    row = tracker.update(at(100), 0, False)
    assert row[3] == 1  # 60 of 90 seconds
    assert feed(BurnTracker(), [(0, 0), (10, 50), (100, 0)], dhw=False)[0][3] == 0


def test_unavailable_gap_and_restart_make_incomplete_burns_that_do_not_count():
    # Unavailable mid-burn.
    assert feed(BurnTracker(), [(0, 0), (10, 50), (40, None), (70, 0)]) == []
    # A gap of more than five minutes between readings.
    assert feed(BurnTracker(), [(0, 0), (10, 50), (400, 50), (410, 0)]) == []
    # Already firing when tracking began (an HA restart): the start is unknown.
    assert feed(BurnTracker(), [(0, 50), (60, 50), (120, 0)]) == []
    # But the next burn after an observed zero counts.
    rows = feed(BurnTracker(), [(0, 50), (60, 0), (100, 30), (160, 0)])
    assert len(rows) == 1 and rows[0][1] == 60.0
    # Back from unavailable straight into firing is not a start either.
    assert feed(BurnTracker(), [(0, 0), (10, None), (20, 40), (80, 0)]) == []


def test_log_pruning_and_round_trip():
    burns = []
    add_burn(burns, [(T0 - 8 * DAY).timestamp(), 100.0, 50.0, 0], T0)
    add_burn(burns, [(T0 - 2 * DAY).timestamp(), 100.0, 50.0, 1], T0)
    assert len(burns) == 1
    assert load_burns([list(b) for b in burns]) == burns
    assert load_burns(None) == [] and load_burns([[1, 2], "x", [1, 0, 50, 0]]) == []
    prune(burns, T0 + 6 * DAY)
    assert burns == []


def log(*rows):
    return [[(T0 - timedelta(minutes=m)).timestamp(), s, mod, d] for m, s, mod, d in rows]


def test_aggregates_short_share_mean_median_and_split():
    burns = log((60, 60, 40, 0), (50, 120, 40, 0), (40, 600, 50, 1), (3000, 1800, 50, 0))
    day = summarise(burns, T0, DAY)
    assert day["starts"] == 3 and (day["heating_starts"], day["dhw_starts"]) == (2, 1)
    assert day["firing_minutes"] == 13.0
    assert day["mean_burn_minutes"] == round(780 / 3 / 60, 1)
    assert day["median_burn_minutes"] == 2.0
    assert day["short_burn_share"] == round(180 / 780, 3)
    assert day["starts_per_firing_hour"] == round(3 / (780 / 3600), 1)
    week = summarise(burns, T0, 7 * DAY)
    assert week["starts"] == 4
    assert summarise([], T0, DAY)["mean_burn_minutes"] is None
    assert covered_days(burns, T0, 7 * DAY) == pytest.approx(3000 / 1440, abs=0.01)
    assert covered_days([], T0, 7 * DAY) == 1.0


def test_adjusted_efficiency_maths_and_none_cases():
    burns = log((10, 3600, 50, 0))  # one hour at 50 % of 30 kW
    fuel = fuel_fire_kwh(burns, T0, DAY, 30.0)
    assert fuel == pytest.approx(15.0)
    assert adjusted_efficiency(90.0, fuel, 1) == round(90.0 * 15 / (15 + START_LOSS_KWH), 1)
    # Many short starts cost far more than one long burn of the same fuel.
    assert adjusted_efficiency(90.0, 0.5, 25) == pytest.approx(45.0)
    assert adjusted_efficiency(None, fuel, 1) is None
    assert adjusted_efficiency(90.0, None, 1) is None
    assert adjusted_efficiency(90.0, 0.0, 0) is None


@pytest.mark.asyncio
async def test_sensors_none_before_any_burn_then_bursts_are_counted(hass, freezer):
    c, _ = await configured(hass)
    c._entry.async_on_unload(c.async_subscribe_burns())
    controls = SimpleNamespace(boiler=c, entry=c._entry, registry_id=lambda s, k: f"sensor.t_{k}")
    starts = BoilerCyclingSensor(controls, "burner_starts_24h", None, "starts")
    mean = BoilerCyclingSensor(controls, "mean_burn_minutes_24h", "min", "mean")
    adjusted = BoilerCyclingSensor(controls, "estimated_efficiency_adjusted_24h", "%", "adjusted")
    await c.async_refresh()
    assert starts.native_value is None and mean.native_value is None
    assert adjusted.native_value is None
    # A valid firing estimate for the rolling figure.
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    freezer.tick(timedelta(minutes=1))
    report(hass)
    await c.async_refresh()
    assert c.data.efficiency_rolling["24h"]["percent"] == pytest.approx(90.1, abs=0.1)
    assert adjusted.native_value is None  # no completed burn yet: no fuel figure

    async def power(value, seconds=0):
        freezer.tick(timedelta(seconds=seconds))
        hass.states.async_set("sensor.eff_power", value)
        await hass.async_block_till_done()

    await power(0, 30)
    await power(60, 10)
    await power(40, 60)
    await power(0, 60)  # burn 1: 120 s at a mean 50 %, heating
    c.data.dhw_active = True
    await power(80, 30)
    await power(0, 60)  # burn 2: 60 s at 80 %, hot water
    assert starts.native_value == 2 and mean.native_value == 1.5
    attrs = starts.extra_state_attributes
    assert (attrs["heating_starts"], attrs["dhw_starts"]) == (1, 1)
    assert attrs["firing_minutes"] == 3.0 and attrs["short_burn_share"] == 1.0
    assert attrs["starts_7d_per_day"] == 2.0
    fuel = 30 * 0.5 * 120 / 3600 + 30 * 0.8 * 60 / 3600
    assert adjusted.native_value == pytest.approx(
        90.1 * fuel / (fuel + 2 * START_LOSS_KWH), abs=0.2
    )
    extra = adjusted.extra_state_attributes
    assert extra["start_loss_kwh_assumed"] == START_LOSS_KWH and "not metered" in extra["method"]
    assert c._store.get("burn_log") and len(c._store.get("burn_log")) == 2
