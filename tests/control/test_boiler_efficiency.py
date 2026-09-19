"""Reference estimates remain opt-in and independent of boiler actuation."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from homeassistant.const import EntityCategory
from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.control.boiler.core.efficiency import (
    CONF_EFFICIENCY_PROFILE,
    PROFILE_BG430I_NATURAL_GAS,
    PROFILE_DISABLED,
    estimate,
)
from custom_components.home_heating_optimisation.control.configuration import (
    ControlConfigError,
    boiler_values,
    update_boiler,
)
from custom_components.home_heating_optimisation.control.entities import BoilerEfficiencySensor
from tests.control.boiler_setup import _setup

PROFILE = PROFILE_BG430I_NATURAL_GAS


def model(**overrides):
    args = dict(
        profile=PROFILE, heating_active=True, burner_power=50, return_c=45, burn_seconds=300
    )
    args.update(overrides)
    return estimate(**args)


@pytest.mark.parametrize("temperature,expected", [(30, 93), (45, 90), (60, 87)])
def test_reference_points(temperature, expected):
    assert model(return_c=temperature).percent == expected


@pytest.mark.parametrize(
    "overrides,status",
    [
        ({"profile": PROFILE_DISABLED}, "disabled"),
        ({"profile": "unknown"}, "unsupported_profile"),
        ({"heating_active": False}, "off"),
        ({"heating_active": None}, "burner_unavailable"),
        ({"burner_power": 0}, "off"),
        ({"burner_power": 101}, "burner_unavailable"),
        ({"burner_power": float("nan")}, "burner_unavailable"),
        ({"burner_power": None}, "burner_unavailable"),
        ({"burn_seconds": None}, "awaiting_observed_ignition"),
        ({"burn_seconds": float("nan")}, "awaiting_observed_ignition"),
        ({"burn_seconds": float("inf")}, "awaiting_observed_ignition"),
        ({"burn_seconds": -1}, "awaiting_observed_ignition"),
        ({"burn_seconds": 299}, "startup_exclusion"),
        ({"return_c": None}, "return_unavailable"),
        ({"return_c": float("nan")}, "return_unavailable"),
        ({"return_c": float("inf")}, "return_unavailable"),
        ({"return_c": 29.99}, "return_out_of_range"),
        ({"return_c": 60.01}, "return_out_of_range"),
    ],
)
def test_model_rejects_unusable_evidence(overrides, status):
    result = model(**overrides)
    assert result.percent is None
    assert result.status == status


def test_profile_configuration():
    assert boiler_values({})[CONF_EFFICIENCY_PROFILE] == PROFILE_DISABLED
    control = {}
    values = {
        "flow_setpoint_entity": "number.flow",
        "outdoor_temp_entity": "sensor.outdoor",
        CONF_EFFICIENCY_PROFILE: PROFILE,
    }
    update_boiler(control, values)
    assert boiler_values(control)[CONF_EFFICIENCY_PROFILE] == PROFILE
    with pytest.raises(ControlConfigError, match="control_invalid_policy"):
        update_boiler(control, {**values, CONF_EFFICIENCY_PROFILE: "lpg"})


async def configured(hass):
    entry, calls = await _setup(hass)
    c = entry.runtime_data
    c._config.update(
        {
            CONF_EFFICIENCY_PROFILE: PROFILE,
            "return_temp_entity": "sensor.eff_return",
            "burner_power_entity": "sensor.eff_power",
            "heating_active_entity": "binary_sensor.eff_active",
        }
    )
    hass.states.async_set("binary_sensor.eff_active", "off")
    hass.states.async_set("sensor.eff_return", 45)
    hass.states.async_set("sensor.eff_power", 0)
    entry_unsub = c.async_subscribe_heating_active()
    c._entry.async_on_unload(entry_unsub)
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.eff_active", "on")
    await hass.async_block_till_done()
    # Source reports may follow ignition, and zero modulation can arrive first.
    hass.states.async_set("sensor.eff_power", 50)
    hass.states.async_set("sensor.eff_return", 45)
    await hass.async_block_till_done()
    return c, calls


def report(hass, value=45):
    hass.states.async_set("sensor.eff_return", value)
    hass.states.async_set("sensor.eff_power", 50)


@pytest.mark.asyncio
async def test_running_startup_stop_and_sensor_metadata(hass, freezer):
    c, calls = await configured(hass)
    await c.async_refresh()
    assert c.data.efficiency_status == "startup_exclusion"
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency == 90
    controls = SimpleNamespace(
        boiler=c,
        entry=c._entry,
        registry_id=lambda scope, key: f"sensor.test_{key}",
    )
    sensor = BoilerEfficiencySensor(controls)
    assert sensor.native_value == 90
    assert sensor.name == "Estimated boiler efficiency"
    assert sensor.native_unit_of_measurement == "%"
    assert sensor.state_class is None
    assert sensor.entity_category == EntityCategory.DIAGNOSTIC
    assert sensor.extra_state_attributes["energy_basis"] == "gross"
    assert sensor.unique_id.endswith(":control:boiler:estimated_running_efficiency")
    before = list(calls)
    listener = Mock()
    unsub = c.async_add_listener(listener)
    hass.states.async_set("binary_sensor.eff_active", "off")
    await hass.async_block_till_done()
    assert c.data.estimated_running_efficiency is None
    assert c.data.efficiency_status == "off"
    assert listener.called
    assert calls == before
    unsub()


@pytest.mark.asyncio
@pytest.mark.parametrize("source", ["sensor.eff_return", "sensor.eff_power"])
async def test_gap_does_not_reuse_old_burn(hass, freezer, source):
    c, calls = await configured(hass)
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency == 90
    hass.states.async_set(source, "unavailable")
    await hass.async_block_till_done()
    assert c.data.estimated_running_efficiency is None
    report(hass)
    await c.async_refresh()
    assert c.data.efficiency_status == "awaiting_observed_ignition"
    hass.states.async_set("binary_sensor.eff_active", "off")
    await hass.async_block_till_done()
    hass.states.async_set("binary_sensor.eff_active", "on")
    await hass.async_block_till_done()
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency == 90


@pytest.mark.asyncio
async def test_stale_and_pre_ignition_readings(hass, freezer):
    c, calls = await configured(hass)
    freezer.tick(timedelta(minutes=6))
    await c.async_refresh()
    assert c.data.estimated_running_efficiency is None
    report(hass)
    await c.async_refresh()
    assert c.data.efficiency_status == "awaiting_observed_ignition"


@pytest.mark.asyncio
async def test_unknown_start_and_invalid_setpoint(hass, freezer):
    c, calls = await configured(hass)
    c._hub.burn_started_at = None
    await c.async_refresh()
    assert c.data.efficiency_status == "awaiting_observed_ignition"
    c._hub.record_ignition(dt_util.utcnow())
    freezer.tick(timedelta(minutes=5))
    report(hass)
    hass.states.async_set("number.boiler_selflowtemp", "unavailable")
    before = list(calls)
    await c.async_refresh()
    assert c.data.no_boiler
    assert c.data.estimated_running_efficiency == 90
    assert calls == before


@pytest.mark.asyncio
@pytest.mark.parametrize("override", ["shadow", "auto", "hold"])
async def test_model_is_independent_of_control_mode(hass, freezer, override):
    c, calls = await configured(hass)
    c.override = override
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency == 90
    assert c.override == override


@pytest.mark.asyncio
async def test_gap_during_warmup_requires_new_ignition(hass, freezer):
    c, calls = await configured(hass)
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("sensor.eff_power", "unavailable")
    await hass.async_block_till_done()
    report(hass)
    freezer.tick(timedelta(minutes=4))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency is None
    assert c.data.efficiency_status == "awaiting_observed_ignition"


@pytest.mark.asyncio
async def test_stop_during_cycle_does_not_restore_old_estimate(hass, freezer, monkeypatch):
    c, calls = await configured(hass)
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    cycle = c._cycle

    async def stopped_cycle():
        data = await cycle()
        assert data.estimated_running_efficiency == 90
        hass.states.async_set("binary_sensor.eff_active", "off")
        await hass.async_block_till_done()
        return data

    monkeypatch.setattr(c, "_cycle", stopped_cycle)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency is None
    assert c.data.efficiency_status == "off"


@pytest.mark.asyncio
async def test_pre_ignition_readings_wait_without_breaking_continuity(hass, freezer):
    c, calls = await configured(hass)
    freezer.tick(timedelta(seconds=1))
    c._hub.record_ignition(dt_util.utcnow())
    await c.async_refresh()
    assert c.data.efficiency_status == "awaiting_current_burn_readings"
    report(hass)
    await c.async_refresh()
    assert c.data.efficiency_status == "startup_exclusion"
    freezer.tick(timedelta(minutes=5))
    report(hass)
    await c.async_refresh()
    assert c.data.estimated_running_efficiency == 90
