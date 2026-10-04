"""DHW measured-temperature cutoff: hold off a charge Evohome would overrun."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.const import DOMAIN, effective_config
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.dhw.cutoff_policy import (
    cloud_override,
    cutoff_config,
    is_hold,
    recent_temperature,
    should_release,
    should_stop,
    validation_error,
)
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

WH = "water_heater.stored_hw"
SENSOR = f"sensor.{DOMAIN}_dhw_cutoff"
PARAMS = {"setpoint": 50.0, "overrun": 0, "differential": 5.0}
HOLD = {"mode": "temporary_override", "active": False, "until": None}
SCHEDULE = {"mode": "follow_schedule", "active": True, "until": None}
CLOUD = "water_heater.dhw_controller"


def cloud(hass, mode="FollowSchedule", scheduled="On"):
    hass.states.async_set(
        CLOUD,
        "auto",
        {
            "status": {
                "state_status": {"state": "On", "mode": mode},
                "setpoints": {"this_sp_state": scheduled},
            }
        },
    )


def with_cutoff(config, **extra):
    config = deepcopy(config)
    config["dhw_cutoff"] = {
        "enabled": True,
        "water_heater_entity": WH,
        "demand_entity": "sensor.hw",
        "cylinder_temp_entity": "sensor.cylinder",
        **extra,
    }
    return config


def heater(hass, mode=SCHEDULE, setpoint=50.0):
    hass.states.async_set(
        WH,
        "auto",
        {
            "temperature": setpoint,
            "params": {"config": {**PARAMS, "setpoint": setpoint}},
            "mode": mode,
        },
    )


def cylinder(hass, value):
    hass.states.async_set(
        "sensor.cylinder", value, {"unit_of_measurement": "°C"}, force_update=True
    )


async def begin(hass, controlled, reply=True, **extra):  # noqa: F811
    heater(hass)
    entry, controls, _ = await start(hass, with_cutoff(controlled, **extra))
    calls = []

    async def set_mode(call):
        calls.append(dict(call.data))
        if not reply:
            return
        if call.data["mode"] == "follow_schedule":
            heater(hass)
        else:
            heater(hass, {"mode": call.data["mode"], "active": call.data["active"], "until": None})

    hass.services.async_register("ramses_cc", "set_dhw_mode", set_mode)
    await handover(controls)
    return entry, entry.runtime_data.dhw_cutoff, calls


async def tick(hass, freezer, cutoff, **delta):
    if delta:
        freezer.tick(timedelta(**delta))
    await hass.async_block_till_done()
    await cutoff.step()
    await hass.async_block_till_done()


# -- pure rules ------------------------------------------------------------


def test_stop_needs_a_charge_a_reading_and_target_plus_margin():
    assert should_stop(True, 52.0, PARAMS, 2.0)
    assert not should_stop(True, 51.9, PARAMS, 2.0)
    # Live 1 Oct: the sensor went quiet at 51.8 °C; the default margin catches it.
    assert should_stop(True, 51.8, PARAMS, cutoff_config({})["margin"])
    assert not should_stop(False, 60.0, PARAMS, 2.0)
    assert not should_stop(None, 60.0, PARAMS, 2.0)
    assert not should_stop(True, None, PARAMS, 2.0)
    assert not should_stop(True, 60.0, None, 2.0)
    # A raised target (the weekly DHW schedule) moves the cutoff with it.
    assert not should_stop(True, 61.0, {**PARAMS, "setpoint": 60.0}, 2.0)


def test_release_below_the_controllers_reheat_point():
    assert should_release(44.9, PARAMS)
    assert not should_release(45.0, PARAMS)
    assert not should_release(None, PARAMS)


def test_hold_is_only_an_off_temporary_override():
    assert is_hold(HOLD)
    assert not is_hold({**HOLD, "active": True})
    assert not is_hold(SCHEDULE)
    assert not is_hold(None)


def test_stale_reading_is_never_used(hass):
    hass.states.async_set("sensor.cylinder", 60, {"unit_of_measurement": "°C"})
    now = dt_util.utcnow()
    assert recent_temperature(hass.states.get("sensor.cylinder"), now) == 60
    assert (
        recent_temperature(hass.states.get("sensor.cylinder"), now + timedelta(minutes=11)) is None
    )


def test_config_defaults_and_validation():
    assert cutoff_config({})["enabled"] is False
    assert cutoff_config({})["margin"] == 1.0
    assert validation_error({"margin": 0.2}) == "dhw_cutoff_invalid_margin"
    assert validation_error({"margin": 2, "enabled": True}) == "dhw_missing_entity"
    assert validation_error({"margin": 2}) is None


# -- runtime ---------------------------------------------------------------


async def test_disabled_by_default_sends_nothing(hass, controlled, sources):  # noqa: F811
    heater(hass)
    entry, controls, _ = await start(hass, controlled)
    calls = []
    hass.services.async_register("ramses_cc", "set_dhw_mode", lambda c: calls.append(c))
    await handover(controls)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 60)
    await entry.runtime_data.dhw_cutoff.step()
    assert calls == []
    assert hass.states.get(SENSOR).state == "disabled"


async def test_charge_past_cutoff_is_held_off_then_released(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    entry, cutoff, calls = await begin(hass, controlled, margin=2.0)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 51.0)
    await tick(hass, freezer, cutoff)
    assert calls == []
    assert hass.states.get(SENSOR).state == "watching"
    cylinder(hass, 52.0)
    await tick(hass, freezer, cutoff)
    assert calls == [
        {
            "entity_id": WH,
            "mode": "temporary_override",
            "active": False,
            "duration": {"minutes": 60},
        }
    ]
    await tick(hass, freezer, cutoff, seconds=30)
    assert hass.states.get(SENSOR).state == "holding"
    # The relay drops, the cylinder cools slowly: still held at 46 °C ...
    hass.states.async_set("sensor.hw", 0)
    cylinder(hass, 46.0)
    await tick(hass, freezer, cutoff, minutes=20)
    assert len(calls) == 1
    # ... and handed back below the controller's reheat point (50 - 5).
    cylinder(hass, 44.8)
    await tick(hass, freezer, cutoff, minutes=10)
    assert calls[-1] == {"entity_id": WH, "mode": "follow_schedule"}
    assert hass.states.get(SENSOR).state == "watching"
    events = [e["data"]["event"] for e in entry.runtime_data.journal.events(kinds=["dhw_cutoff"])]
    assert events == ["stop", "release"]


async def test_controller_ending_the_hold_is_accepted(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    entry, cutoff, calls = await begin(hass, controlled)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 53.0)
    await tick(hass, freezer, cutoff)
    await tick(hass, freezer, cutoff, seconds=30)
    assert cutoff.status == "holding"
    # 60 minutes later the controller returns to its schedule on its own.
    freezer.tick(timedelta(minutes=59))
    heater(hass)
    hass.states.async_set("sensor.hw", 0)
    await tick(hass, freezer, cutoff)
    assert cutoff.status == "watching"
    assert len(calls) == 1
    events = [e["data"]["event"] for e in entry.runtime_data.journal.events(kinds=["dhw_cutoff"])]
    assert "taken_over" not in events


async def test_boost_and_manual_override_are_left_alone(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    _, cutoff, calls = await begin(hass, controlled)
    hass.states.async_set("sensor.hw", 100)
    heater(hass, {"mode": "permanent_override", "active": True, "until": None})
    cylinder(hass, 60.0)
    await tick(hass, freezer, cutoff)
    assert calls == []
    assert cutoff.status == "standby"


async def test_unconfirmed_stop_is_retried_once_then_dropped(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    entry, cutoff, calls = await begin(hass, controlled, reply=False)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 53.0)
    await tick(hass, freezer, cutoff)
    assert cutoff.status == "unconfirmed"
    cylinder(hass, 53.5)
    await tick(hass, freezer, cutoff, minutes=4)
    assert len(calls) == 2
    cylinder(hass, 54.0)
    await tick(hass, freezer, cutoff, minutes=4)
    assert len(calls) == 2
    events = [e["data"]["event"] for e in entry.runtime_data.journal.events(kinds=["dhw_cutoff"])]
    assert events[-1] == "unconfirmed"


async def test_blocked_by_a_target_writing_automation(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    _, cutoff, calls = await begin(hass, controlled)
    with patch.object(
        type(cutoff.heating.controls), "dhw_guard_reason", return_value="blocked for test"
    ):
        # State changes trigger an evaluation immediately, so they go inside the patch.
        hass.states.async_set("sensor.hw", 100)
        cylinder(hass, 53.0)
        await tick(hass, freezer, cutoff)
    assert calls == []
    assert cutoff.report()["blocked"] == "blocked for test"


async def test_options_flow_saves_the_section(hass, controlled, sources):  # noqa: F811
    heater(hass)
    entry, _, _ = await start(hass, controlled)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "dhw_cutoff" in flow["menu_options"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "dhw_cutoff"}
    )
    values = {
        "enabled": True,
        "water_heater_entity": WH,
        "demand_entity": "sensor.hw",
        "cylinder_temp_entity": "sensor.cylinder",
        "margin": 2.0,
    }
    missing = {k: v for k, v in values.items() if k != "demand_entity"}
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], missing)
    assert flow["errors"] == {"base": "dhw_missing_entity"}
    result = await hass.config_entries.options.async_configure(flow["flow_id"], values)
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    section = effective_config(entry)["dhw_cutoff"]
    assert section["enabled"] and section["margin"] == 2.0
    assert section["cloud_entity"] is None
    assert effective_config(entry)["rooms"] == controlled["rooms"]


async def test_hold_is_renewed_while_the_cylinder_stays_hot(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    entry, cutoff, calls = await begin(hass, controlled)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 52.0)
    await tick(hass, freezer, cutoff)
    hass.states.async_set("sensor.hw", 0)
    await tick(hass, freezer, cutoff, seconds=30)
    for minute in range(1, 300):
        cylinder(hass, 50.0)  # the sensor still reports; the cylinder has not cooled
        await tick(hass, freezer, cutoff, minutes=1)
        if cutoff.hold_until is None:
            break
    renewals = [c for c in calls if c["mode"] == "temporary_override"]
    # One stop and three renewals: four hours in total, then the controller's own expiry.
    assert len(renewals) == 4
    assert cutoff.stops == 1
    events = [e["data"]["event"] for e in entry.runtime_data.journal.events(kinds=["dhw_cutoff"])]
    assert events == ["stop", "renew", "renew", "renew"]


def test_cloud_override_reasons():
    def state(mode, scheduled, value="auto"):
        return type(
            "S",
            (),
            {
                "state": value,
                "attributes": {
                    "status": {
                        "state_status": {"mode": mode},
                        "setpoints": {"this_sp_state": scheduled},
                    }
                },
            },
        )()

    assert cloud_override(state("FollowSchedule", "On")) is None
    assert cloud_override(state("PermanentOverride", "Off")) == "Evohome mode PermanentOverride"
    assert cloud_override(state("FollowSchedule", "Off")) == "Evohome schedule has DHW off"
    # No usable answer falls back to the RAMSES mode check.
    assert cloud_override(state("FollowSchedule", "On", "unavailable")) is None
    assert cloud_override(None) is None


async def test_boost_hidden_by_a_restart_is_left_alone(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    """Live 4 Oct: after an HA restart mid-boost the RAMSES entity showed follow_schedule
    while the controller (and the cloud entity) stayed in a permanent override."""
    cloud(hass, "PermanentOverride", "Off")
    _, cutoff, calls = await begin(hass, controlled, cloud_entity=CLOUD)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 52.0)
    await tick(hass, freezer, cutoff)
    assert calls == []
    assert cutoff.status == "standby"
    assert cutoff.report()["standby_reason"] == "Evohome mode PermanentOverride"
    # A charge outside the schedule (the schedule has DHW off) is someone else's too.
    cloud(hass, "FollowSchedule", "Off")
    await tick(hass, freezer, cutoff)
    assert calls == []
    # A scheduled charge is still stopped.
    cloud(hass, "FollowSchedule", "On")
    cylinder(hass, 52.1)
    await tick(hass, freezer, cutoff)
    assert len(calls) == 1 and calls[0]["mode"] == "temporary_override"


async def test_unavailable_cloud_entity_falls_back_to_ramses_mode(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    hass.states.async_set(CLOUD, "unavailable")
    _, cutoff, calls = await begin(hass, controlled, cloud_entity=CLOUD)
    hass.states.async_set("sensor.hw", 100)
    cylinder(hass, 52.0)
    await tick(hass, freezer, cutoff)
    assert len(calls) == 1
