"""End-to-end regressions for v0.3 behaviour, through the HA coordinator."""

from datetime import timedelta

import pytest
from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.control.boiler.const import (
    CONF_CYLINDER_TARGET_ENTITY,
    CONF_CYLINDER_TEMP_ENTITY,
    CONF_INPUT_FRESHNESS_MINUTES,
    CONF_MAX_FLOW_ENTITY,
    CONF_ROOM_CLIMATE_ENTITIES,
    CONF_ZONE_DEMAND_ENTITIES,
    OVERRIDE_AUTO,
)
from custom_components.home_heating_optimisation.control.boiler.core.model import (
    ReturnCorrectionState,
)
from tests.control.boiler_setup import _setup

pytestmark = pytest.mark.asyncio


async def auto(hass):
    entry, calls = await _setup(hass)
    c = entry.runtime_data
    c.override = OVERRIDE_AUTO
    return c, calls


def cylinder(hass, c, value="50"):
    c._config[CONF_CYLINDER_TEMP_ENTITY] = "sensor.cylinder"
    hass.states.async_set("sensor.cylinder", value)
    hass.states.async_set("sensor.hw_relay_demand", "100")


async def test_dhw_entry_and_exit_immediately_replace_heating_target(hass):
    c, calls = await auto(hass)
    await c.async_refresh()
    assert calls[-1]["value"] == 55
    cylinder(hass, c)
    await c.async_refresh()
    assert calls[-1]["value"] == 70
    hass.states.async_set("sensor.hw_relay_demand", "0")
    await c.async_refresh()
    assert calls[-1]["value"] == 55


async def test_dhw_missing_cylinder_and_outdoor_still_uses_dhw_fallback(hass):
    c, calls = await auto(hass)
    hass.states.async_set("sensor.outdoor_temp", "unavailable")
    hass.states.async_set("sensor.hw_relay_demand", "100")
    await c.async_refresh()
    assert calls[-1]["value"] == 70
    assert c.data.dhw_status == "fallback_missing_temperature"


async def test_dhw_cycles_cannot_create_unapplied_interventions_or_hold(hass):
    c, calls = await auto(hass)
    cylinder(hass, c)
    await c.async_refresh()
    for _ in range(6):
        c._hub.record_ignition(dt_util.utcnow())
        await c.async_refresh()
    assert calls[-1]["value"] == 70
    assert c._hub.dhw_cycling.attempts == 0
    assert not c._hub.dhw_cycling.holding
    assert c.data.cycling_status == "frequent_starts_diagnostic_only"


async def test_new_charge_excludes_earlier_heating_starts(hass):
    c, calls = await auto(hass)
    await c.async_refresh()
    for _ in range(3):
        c._hub.record_ignition(dt_util.utcnow())
    cylinder(hass, c)
    await c.async_refresh()
    assert c.data.dhw_charge_starts == 0


async def test_capped_write_memory_and_dial_raise_do_not_cause_manual_hold(hass):
    c, calls = await auto(hass)
    c._config[CONF_MAX_FLOW_ENTITY] = "number.max_flow"
    hass.states.async_set("number.max_flow", "50")
    await c.async_refresh()
    assert calls[-1]["value"] == c.data.last_written_setpoint == c.data.flow_setpoint == 50
    hass.states.async_set("number.max_flow", "70")
    await c.async_refresh()
    assert c.data.mode == "heating" and calls[-1]["value"] == 55


async def test_lowered_max_applies_during_minimum_hold(hass):
    c, calls = await auto(hass)
    c._config[CONF_MAX_FLOW_ENTITY] = "number.max_flow"
    hass.states.async_set("number.max_flow", "70")
    await c.async_refresh()
    hass.states.async_set("number.max_flow", "45")
    await c.async_refresh()
    assert calls[-1]["value"] == c.data.last_written_setpoint == 45


async def test_unavailable_max_suspends_writes_and_recovers(hass):
    c, calls = await auto(hass)
    c._config[CONF_MAX_FLOW_ENTITY] = "number.max_flow"
    hass.states.async_set("number.max_flow", "unavailable")
    await c.async_refresh()
    assert calls == []
    hass.states.async_set("number.max_flow", "60")
    await c.async_refresh()
    assert calls[-1]["value"] == 55


async def test_conflicting_floor_and_max_suspends_without_invalid_service_call(hass):
    c, calls = await auto(hass)
    c._config[CONF_MAX_FLOW_ENTITY] = "number.max_flow"
    hass.states.async_set("number.max_flow", "30")
    await c.async_refresh()
    assert not calls
    assert any("conflicts" in message for message in c.data.disabled_features)


@pytest.mark.parametrize("bad", ["nan", "inf", "-inf", "unavailable"])
async def test_invalid_live_setpoint_blocks_control(hass, bad):
    c, calls = await auto(hass)
    hass.states.async_set("number.boiler_selflowtemp", bad)
    await c.async_refresh()
    assert c.data.no_boiler and not calls


async def test_stale_cylinder_uses_fallback(hass, freezer):
    c, calls = await auto(hass)
    cylinder(hass, c, "30")
    await c.async_refresh()
    assert calls[-1]["value"] == 65
    freezer.tick(timedelta(minutes=31))
    hass.states.async_set("sensor.hw_relay_demand", "100", {"refresh": 1})
    await c.async_refresh()
    assert calls[-1]["value"] == 70
    assert c.data.dhw_status == "fallback_missing_temperature"


async def test_complete_fresh_zones_infer_only_after_debounce(hass, freezer):
    c, calls = await auto(hass)
    c._config[CONF_ZONE_DEMAND_ENTITIES] = ["sensor.zone1", "sensor.zone2"]
    hass.states.async_set("sensor.zone1", "0")
    hass.states.async_set("sensor.zone2", "0")
    hass.states.async_set("sensor.heat_demand", "100")
    await c.async_refresh()
    assert not c.data.dhw_active
    freezer.tick(timedelta(minutes=2))
    await c.async_refresh()
    assert c.data.dhw_active and c.data.dhw_source == "inferred"
    assert calls[-1]["value"] == 70
    assert (
        hass.states.get("binary_sensor.home_heating_optimisation_control_boiler_dhw").state == "on"
    )


async def test_partial_or_stale_zones_cannot_infer_dhw(hass, freezer):
    c, calls = await auto(hass)
    c._config[CONF_ZONE_DEMAND_ENTITIES] = ["sensor.zone1", "sensor.zone2"]
    hass.states.async_set("sensor.zone1", "0")
    hass.states.async_set("sensor.zone2", "unavailable")
    hass.states.async_set("sensor.heat_demand", "100")
    await c.async_refresh()
    freezer.tick(timedelta(minutes=3))
    await c.async_refresh()
    assert not c.data.dhw_active
    hass.states.async_set("sensor.zone2", "0")
    freezer.tick(timedelta(minutes=31))
    hass.states.async_set("sensor.heat_demand", "100", {"refresh": 1})
    await c.async_refresh()
    assert not c.data.dhw_active


async def test_live_cylinder_target_change_bypasses_hold(hass):
    c, calls = await auto(hass)
    cylinder(hass, c, "30")
    c._config[CONF_CYLINDER_TARGET_ENTITY] = "water_heater.cylinder"
    hass.states.async_set("water_heater.cylinder", "on", {"temperature": 60})
    await c.async_refresh()
    assert calls[-1]["value"] == 65
    hass.states.async_set("water_heater.cylinder", "on", {"temperature": 65})
    await c.async_refresh()
    assert calls[-1]["value"] == 70
    hass.states.async_set("water_heater.cylinder", "on", {"temperature": 70})
    await c.async_refresh()
    assert calls[-1]["value"] == 70 and c.data.dhw_status == "insufficient_flow_headroom"


async def test_charge_stall_uses_fallback_and_alerts(hass, freezer):
    c, calls = await auto(hass)
    c._config[CONF_INPUT_FRESHNESS_MINUTES] = 120
    cylinder(hass, c, "30")
    await c.async_refresh()
    assert calls[-1]["value"] == 65
    freezer.tick(timedelta(minutes=30))
    await c.async_refresh()
    assert calls[-1]["value"] == 70
    assert c.data.dhw_status == "insufficient_temperature_progress"
    assert c.data.dhw_issue_raised


async def test_shadow_uses_same_cap_and_hold_without_changing_live_memory(hass, freezer):
    entry, calls = await _setup(hass)
    c = entry.runtime_data
    c._config[CONF_MAX_FLOW_ENTITY] = "number.max_flow"
    hass.states.async_set("number.max_flow", "50")
    await c.async_refresh()
    assert c.data.would_write == 50
    assert c._hub.last_written_setpoint is None
    hass.states.async_set("sensor.outdoor_temp", "10")
    await c.async_refresh()
    assert c.data.would_write == 50  # shadow's virtual hold, as in auto
    freezer.tick(timedelta(minutes=11))
    await c.async_refresh()
    assert c.data.would_write == 38
    assert not calls and c._hub.last_written_setpoint is None


async def test_delayed_readback_is_not_mistaken_for_manual_hold(hass, freezer):
    c, calls = await auto(hass)

    async def delayed(call):
        calls.append(dict(call.data))

    hass.services.async_register("number", "set_value", delayed)
    await c.async_refresh()
    assert c.data.write_status == "pending_readback"
    freezer.tick(timedelta(minutes=4))
    await c.async_refresh()
    assert c.data.write_status == "unconfirmed_readback"
    assert not c.data.manual_hold_active
    hass.states.async_set("number.boiler_selflowtemp", "55")
    await c.async_refresh()
    assert c.data.write_status == "confirmed"


async def test_failed_dhw_transition_retries_immediately(hass):
    c, calls = await auto(hass)
    await c.async_refresh()

    async def fail(call):
        raise RuntimeError("test failure")

    hass.services.async_register("number", "set_value", fail)
    cylinder(hass, c)
    await c.async_refresh()
    assert c._hub.last_written_setpoint == 55

    async def succeed(call):
        calls.append(dict(call.data))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]))

    hass.services.async_register("number", "set_value", succeed)
    await c.async_refresh()
    assert calls[-1]["value"] == 70


async def test_fahrenheit_number_roundtrip_and_grid(hass):
    c, calls = await auto(hass)
    attrs = {"unit_of_measurement": "°F", "min": 41, "max": 194, "step": 1}
    hass.states.async_set("number.boiler_selflowtemp", "122", attrs)

    async def fahrenheit(call):
        calls.append(dict(call.data))
        hass.states.async_set(call.data["entity_id"], str(call.data["value"]), attrs)

    hass.services.async_register("number", "set_value", fahrenheit)
    await c.async_refresh()
    assert calls[-1]["value"] == pytest.approx(131)
    assert c.data.last_written_setpoint == 55 and c.data.write_status == "confirmed"


async def test_old_return_penalty_is_removed_when_sensor_is_missing(hass):
    c, calls = await auto(hass)
    hass.states.async_set("sensor.heat_demand", "50")
    c._return_state = ReturnCorrectionState(-6, dt_util.utcnow())
    await c.async_refresh()
    assert c.data.return_correction == 0


async def test_room_feedback_is_optional_and_protects_a_cold_room(hass, freezer):
    c, calls = await auto(hass)
    c._config[CONF_ROOM_CLIMATE_ENTITIES] = ["climate.studio"]
    hass.states.async_set("climate.studio", "heat", {"current_temperature": 18, "temperature": 20})
    await c.async_refresh()
    freezer.tick(timedelta(minutes=11))
    await c.async_refresh()
    assert c.data.room_correction == 4
    assert calls[-1]["value"] == 59


async def test_stop_diagnostics_distinguish_relay_from_possible_flow_limit(hass):
    c, _ = await auto(hass)
    hub = c._hub
    now = dt_util.utcnow()
    hub.record_ignition(now)
    hub.record_stop(now + timedelta(seconds=40), False, 55, 55)
    assert hub.last_burn_seconds == 40 and hub.last_stop_reason == "relay_request_ended"
    hub.record_ignition(now)
    hub.record_stop(now + timedelta(seconds=50), True, 55, 55)
    assert hub.last_stop_reason == "possible_flow_limit"


async def test_unchanged_dhw_target_does_not_renew_hold_timer(hass, freezer):
    c, calls = await auto(hass)
    cylinder(hass, c)
    await c.async_refresh()
    changed = c._hub.last_target_change
    freezer.tick(timedelta(minutes=1))
    await c.async_refresh()
    assert c._hub.last_target_change == changed


async def test_dhw_signal_remains_valid_when_boiler_is_unavailable(hass):
    c, calls = await auto(hass)
    hass.states.async_set("number.boiler_selflowtemp", "unavailable")
    cylinder(hass, c)
    await c.async_refresh()
    assert c.data.no_boiler and c.data.dhw_active and not calls
    assert (
        hass.states.get("binary_sensor.home_heating_optimisation_control_boiler_dhw").state == "on"
    )


async def test_disabled_reload_does_not_resume_auto_writes(hass):
    c, calls = await auto(hass)
    await c.async_refresh()
    c.enabled = False
    await c.async_save_settings()
    calls.clear()
    await hass.config_entries.async_reload(c._entry.entry_id)
    await hass.async_block_till_done()
    restored = c._entry.runtime_data.controls.boiler
    assert not restored.enabled
    await restored.async_refresh()
    assert not calls
