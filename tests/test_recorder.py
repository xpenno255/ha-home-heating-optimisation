"""A real Recorder database exercises the supported history API."""

from datetime import timedelta

import pytest
from pytest_homeassistant_custom_component.components.recorder.common import (
    async_recorder_block_till_done,
)

from tests.test_integration import entity_id, setup


@pytest.fixture
def mock_recorder_before_hass(recorder_db_url):
    yield


async def test_real_recorder_backfill_and_reload(hass, recorder_mock, freezer, config):
    config["analytics_enabled"] = True
    # Keep this short fixture above one-decimal coverage rounding.
    config["analysis_window_days"] = 3
    for minute, demand, temperature in ((0, 0, 18), (5, 70, 18.5), (5, 40, 19)):
        freezer.tick(timedelta(minutes=minute))
        hass.states.async_set(
            "climate.study",
            "auto",
            {"temperature": 20, "current_temperature": temperature, "hvac_action": "idle"},
        )
        hass.states.async_set("sensor.study_demand", demand, {"unit_of_measurement": "%"})
        await hass.async_block_till_done()
        await async_recorder_block_till_done(hass)
    entry = await setup(hass, config)
    history = entry.runtime_data.analytics
    await history.task
    assert history.backfill_status == "complete"
    assert any(p["zones"].get("study", {}).get("demand") == 0.7 for p in history.store.observations)
    old_id = entity_id(hass, entry, "room:study:analytics_coverage")
    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await entry.runtime_data.analytics.task
    assert history.closed
    assert entity_id(hass, entry, "room:study:analytics_coverage") == old_id
    assert (
        entry.runtime_data.analytics.data["analysis"]["zone_stats"]["study"]["demand_coverage"] > 0
    )


async def test_recorder_does_not_refresh_old_start_state(hass, recorder_mock, freezer, config):
    from homeassistant.util import dt as dt_util

    from custom_components.home_heating_optimisation.analytics.backfill import async_backfill

    hass.states.async_set("climate.study", "auto", {"temperature": 20, "current_temperature": 18})
    hass.states.async_set("sensor.study_demand", 70, {"unit_of_measurement": "%"})
    await hass.async_block_till_done()
    await async_recorder_block_till_done(hass)
    freezer.tick(timedelta(hours=3))
    end = dt_util.utcnow()
    points, _, status = await async_backfill(hass, config, end - timedelta(hours=1), end)
    assert status == "complete"
    assert all(p["zones"]["study"]["temperature"] is None for p in points)
    assert all(p["zones"]["study"]["active"] is None for p in points)
