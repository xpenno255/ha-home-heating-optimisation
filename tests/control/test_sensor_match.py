"""Sensor matching: write the zone target in the zone sensor's terms."""

import pytest

from custom_components.home_heating_optimisation.control.comfort.core.sensor_match import (
    MAX_OFFSET_K,
    gap_step,
    to_zone,
    zone_offset,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

SWITCH = "switch.home_heating_optimisation_living_room_control_sensor_match_enabled"


def test_gap_filter_seeds_then_follows_slowly():
    assert gap_step(None, 0.5, 0.0) == 0.5
    assert gap_step(0.5, 0.5, 3.0) == 0.5
    # A door left open for 10 minutes barely moves a 2-hour filter.
    assert gap_step(0.5, -1.5, 10 / 60) == pytest.approx(0.5 - 2.0 * 0.0799, abs=0.01)
    assert gap_step(0.5, -1.5, 100.0) == pytest.approx(-1.5)


def test_offset_is_capped_and_on_the_thermostat_grid():
    assert zone_offset(0.43) == 0.4
    assert zone_offset(-0.27) == -0.3
    assert zone_offset(4.0) == MAX_OFFSET_K
    assert zone_offset(-4.0) == -MAX_OFFSET_K
    assert zone_offset(None) == 0.0
    assert to_zone(19.3, 0.4) == 18.9
    assert to_zone(None, 0.4) is None


async def test_matching_writes_the_target_less_the_gap(hass, controlled, sources):  # noqa: F811
    # The room sensor reads 18.0; the zone's own sensor reads 0.5 K lower.
    hass.states.async_set("climate.study", "auto", {"current_temperature": 17.5, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    plain = [d for kind, d in calls if kind == "ramses"][-1]["setpoint"]
    assert room.data.sensor_gap_k == pytest.approx(0.5)
    assert room.data.sensor_offset_k == 0.0 and plain == pytest.approx(room.data.air_setpoint)

    assert hass.states.get(SWITCH).state == "off"
    await hass.services.async_call("switch", "turn_on", {"entity_id": SWITCH}, blocking=True)
    await hass.async_block_till_done()
    assert room.sensor_match_enabled and room.data.sensor_offset_k == 0.5
    assert c.settings.get("flags")["study:sensor_match_enabled"] is True
    matched = [d for kind, d in calls if kind == "ramses"][-1]["setpoint"]
    assert matched == pytest.approx(room.data.air_setpoint - 0.5)
    # The boiler side still judges the room in room-sensor terms.
    assert c.warmup_rooms()[0][5] == room.data.air_setpoint


async def test_one_sensor_has_no_gap(hass, controlled, sources):  # noqa: F811
    controlled["control"]["rooms"]["study"]["config"]["air_temp_sensor"] = None
    hass.states.async_remove("sensor.air")
    _, c, _ = await start(hass, controlled)
    hass.states.async_remove("sensor.air")
    hass.states.async_set("climate.study", "auto", {"current_temperature": 17.5, "temperature": 20})
    await c.refresh()
    room = c.rooms["study"]
    assert room.data.air_temp_source == "climate.study.current_temperature"
    assert room.data.sensor_gap_k == 0.0
