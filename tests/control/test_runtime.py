"""Real HA control setup, service writes, restoration and exclusive ownership."""

from copy import deepcopy
from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.control.comfort.core.policy import (
    Action,
    Decision,
    State,
)
from custom_components.home_heating_optimisation.control.migration import handover, rollback
from custom_components.home_heating_optimisation.observations import read
from custom_components.home_heating_optimisation.telemetry import Telemetry
from tests.test_integration import setup


@pytest.fixture
def controlled(config):
    config = deepcopy(config)
    config["control"] = {
        "schema": 1,
        "hub": {
            "house_dir": str(Path(__file__).parent / "fixtures"),
            "outdoor_temp_sensor": "sensor.outdoor",
        },
        "rooms": {
            "study": {
                "config": {
                    "name": "Living room",
                    "room_id": "living_room",
                    "primary_climate": "climate.study",
                    "backup_climate": "climate.cloud",
                    "air_temp_sensor": "sensor.air",
                },
                "seed": {},
                "enabled": True,
                "occupancy_enabled": False,
            }
        },
        "boiler": {
            "config": {
                "flow_setpoint_entity": "number.flow_setpoint",
                "outdoor_temp_entity": "sensor.outdoor",
                "heat_demand_entity": "sensor.study_demand",
                "hw_relay_demand_entity": "sensor.hw",
                "max_flow_entity": "number.limit",
                "cylinder_temp_entity": "sensor.cylinder",
            },
            "seed": {},
        },
        "legacy_entries": [],
    }
    return config


async def start(hass, controlled):
    hass.states.async_set("sensor.air", 18, {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.cloud", "auto", {"status": {"setpoints": {"this_sp_temp": 20}}})
    hass.states.async_set(
        "number.flow_setpoint", 50, {"unit_of_measurement": "°C", "min": 0, "max": 90, "step": 1}
    )
    hass.states.async_set("number.limit", 70, {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.hw", 0)
    hass.states.async_set("sensor.cylinder", 50, {"unit_of_measurement": "°C"})
    entry = await setup(hass, controlled)
    calls = []

    async def number(call):
        calls.append(("number", dict(call.data)))
        state = hass.states.get(call.data["entity_id"])
        hass.states.async_set(call.data["entity_id"], call.data["value"], state.attributes)

    async def climate(call):
        calls.append(("ramses", dict(call.data)))

    hass.services.async_register("number", "set_value", number)
    hass.services.async_register("ramses_cc", "set_zone_mode", climate)
    return entry, entry.runtime_data.controls, calls


async def test_shadow_has_both_engines_and_independent_air_source(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    await c.refresh()
    assert c.boiler.data.would_write is not None
    assert c.rooms["study"].data.air_setpoint is not None
    assert c.rooms["study"].data.air_temp_source == "sensor.air"
    assert calls == []
    assert entry.runtime_data.config["dhw_active"].startswith(
        "binary_sensor.home_heating_optimisation_control_"
    )
    assert entry.options == {}  # runtime rewiring must not mutate stored mappings


async def test_requires_handover_and_refuses_enabled_legacy(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    with pytest.raises(ServiceValidationError):
        await c.set_mode("boiler", "auto")
    legacy = MockConfigEntry(domain="boiler_flow_control", data={})
    legacy.add_to_hass(hass)
    c.settings.set("ownership", "ready")
    with pytest.raises(ServiceValidationError):
        await c.set_mode("boiler", "auto")
    c.boiler.override = "auto"  # The final actuator gate still refuses a bypassed selector.
    await c.boiler.async_refresh()
    assert calls == []


async def test_both_actuators_work_after_exclusive_handover(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    result = await handover(c)
    assert result["status"] == "handover_complete_shadow" and calls == []
    await c.set_mode("boiler", "auto")
    assert any(kind == "number" for kind, data in calls)
    await c.set_mode("study", "active")
    assert any(kind == "ramses" and data["mode"] == "temporary_override" for kind, data in calls)


async def test_room_explicit_policy_bounds_clamp_the_actual_write(hass, controlled, sources):
    controlled["control"]["rooms"]["study"]["config"].update(
        {
            "zone_setpoint_min": 22,
            "zone_setpoint_max": 23,
            # Legacy keys are deliberately not actuator limits.
            "min_setpoint": 5,
            "max_setpoint": 5,
        }
    )
    _, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    radiator_writes = [data for kind, data in calls if kind == "ramses"]
    assert radiator_writes[-1]["setpoint"] == 22
    assert c.rooms["study"].data.sent_target == 22


async def test_room_command_is_confirmed_only_by_a_fresh_thermostat_echo(
    hass, controlled, sources, freezer
):
    _, c, _ = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    target = room.data.sent_target
    assert target is not None
    assert room.data.requested_target == target
    assert room.data.pending_target == target
    assert room.data.confirmed_target is None
    assert room.data.write_status == "readback_no_echo"

    freezer.tick(timedelta(minutes=3))
    await room.async_refresh()
    assert room.data.write_status == "readback_no_echo"
    assert room.data.readback_timed_out
    assert room.data.pending_target == target

    # A newer report with the wrong value is evidence, but not an acknowledgement.
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": target + 1}
    )
    await room.async_refresh()
    assert room.data.write_status == "readback_different"
    assert room.data.readback_timed_out
    assert room.data.confirmed_target is None

    # This changed target differs from the captured pre-send target, so the
    # fresh primary report is command evidence even without a mode attribute.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": target}
    )
    await room.async_refresh()
    assert room.data.write_status == "confirmed"
    assert room.data.pending_target is None
    assert room.data.confirmed_target == target
    assert room.data.confirmed_at is not None
    published = hass.states.get(c.entities[("study", "state")])
    assert published.attributes["write_status"] == "confirmed"
    assert published.attributes["confirmed_target"] == target
    assert published.attributes["sent_at"] == room.data.sent_at.isoformat()
    assert published.attributes["confirmed_at"] == room.data.confirmed_at.isoformat()


async def test_room_same_target_refresh_needs_primary_mode_transition(
    hass, controlled, sources, freezer
):
    _, c, _ = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    target = room.data.pending_target
    assert target is not None
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": target,
            "mode": {"mode": "temporary_override", "setpoint": target, "until": None},
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "confirmed"

    # The renewal writes the same target.  Its pre-send explicit mode is
    # follow_schedule, so a later transition to temporary_override is evidence.
    freezer.tick(timedelta(minutes=46))
    hass.states.async_set("sensor.air", 18, {"unit_of_measurement": "°C"})
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": target,
            "mode": {"mode": "follow_schedule", "setpoint": target, "until": None},
        },
    )
    await room.async_refresh()
    assert room.data.action == "write"
    assert room.data.write_status == "readback_no_echo"

    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": target,
            "mode": {"mode": "follow_schedule", "setpoint": target, "until": None},
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "matching_readback_unverified"

    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": target,
            "mode": {"mode": "temporary_override", "setpoint": target, "until": None},
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "confirmed"


async def test_room_release_requires_primary_follow_schedule_mode_transition(
    hass, controlled, sources, freezer
):
    _, c, _ = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    target = room.data.pending_target
    assert target is not None
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": target,
            "mode": {"mode": "temporary_override", "setpoint": target, "until": None},
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "confirmed"

    room.enabled = False
    await room.async_refresh()
    assert room.data.action == "release"
    assert room.data.pending_target == room.data.schedule_setpoint
    assert room.data.confirmed_target is None

    # Matching target while the primary still says temporary_override is not a
    # schedule handback acknowledgement.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": room.data.pending_target,
            "mode": {
                "mode": "temporary_override",
                "setpoint": room.data.pending_target,
                "until": None,
            },
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "matching_readback_unverified"

    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": room.data.pending_target,
            "mode": {
                "mode": "follow_schedule",
                "setpoint": room.data.pending_target,
                "until": None,
            },
        },
    )
    await room.async_refresh()
    assert room.data.write_status == "confirmed"


async def test_failed_room_retry_preserves_the_pending_command_baseline(hass, controlled, sources):
    _, c, _ = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    pending = room.data.pending_target
    assert pending is not None
    room._pre_send_setpoint = 19.0
    room._pre_send_mode = "temporary_override"

    async def fail(call):
        raise RuntimeError("test")

    hass.services.async_register("ramses_cc", "set_zone_mode", fail)
    hass.states.async_set(
        "climate.study",
        "auto",
        {
            "current_temperature": 18,
            "temperature": 22,
            "mode": {"mode": "follow_schedule", "setpoint": 22, "until": None},
        },
    )
    forced = Decision(State.ACTIVE, Action.WRITE, 22, "forced retry", room._memory())
    with patch(
        "custom_components.home_heating_optimisation.control.comfort.coordinator.decide",
        return_value=forced,
    ):
        await room.async_refresh()
    assert room._pending_target == pending
    assert room._pre_send_setpoint == 19.0
    assert room._pre_send_mode == "temporary_override"


async def test_room_readback_reports_unavailable_stale_and_invalid_values(
    hass, controlled, sources, freezer
):
    _, c, _ = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]

    hass.states.async_set("climate.study", "unavailable")
    await room.async_refresh()
    assert room.data.write_status == "readback_unavailable"

    target = room.data.pending_target
    assert target is not None
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": target}
    )
    freezer.tick(timedelta(minutes=6))
    await room.async_refresh()
    assert room.data.write_status == "readback_stale"

    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": "not-a-number"}
    )
    await room.async_refresh()
    assert room.data.write_status == "readback_error"


async def test_room_acknowledgement_is_not_restored_or_created_in_shadow(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    room = c.rooms["study"]
    await room.async_refresh()
    assert room.data.confirmed_target is None
    assert room.data.sent_target is None
    assert calls == []

    await handover(c)
    await c.set_mode("study", "active")
    assert room.data.pending_target is not None
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = entry.runtime_data.controls.rooms["study"]
    assert restored.data.confirmed_target is None
    assert restored.data.sent_target is None
    assert restored.data.pending_target is None


async def test_lower_limits_and_dhw_transition(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("boiler", "auto")
    hass.states.async_set("sensor.hw", 100)
    await c.boiler.async_refresh()
    assert c.boiler.data.dhw_active
    assert calls[-1][1]["value"] == 70
    hass.states.async_set("number.limit", 60, {"unit_of_measurement": "°C"})
    await c.boiler.async_refresh()
    assert calls[-1][1]["value"] == 60
    assert c.boiler.data.dhw_status == "insufficient_flow_headroom"
    hass.states.async_set("number.limit", "unavailable")
    calls.clear()
    await c.boiler.async_refresh()
    assert calls == []


async def test_failed_write_does_not_advance_memory(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)

    async def fail(call):
        raise RuntimeError("test")

    hass.services.async_register("number", "set_value", fail)
    await handover(c)
    await c.set_mode("boiler", "auto")
    assert c.boiler._hub.last_written_setpoint is None


async def test_restart_preserves_only_completed_ownership(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("boiler", "auto")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    restored = entry.runtime_data.controls
    assert restored.boiler.override == "auto"
    restored.settings.set("ownership", "transition")
    await restored.settings.async_save()
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.runtime_data.controls.boiler.override == "shadow"


async def test_new_legacy_entry_blocks_already_active_writer(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("boiler", "auto")
    legacy = MockConfigEntry(domain="ot_thermostat_control", data={})
    legacy.add_to_hass(hass)
    calls.clear()
    await c.boiler.async_refresh()
    assert calls == []


async def test_rollback_stops_new_writes(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("boiler", "auto")
    calls.clear()
    await rollback(c)
    await c.refresh()
    assert calls == [] and c.settings.get("ownership") == "unclaimed"


async def test_corrupt_storage_blocks_setup(hass, controlled, sources):
    with patch(
        "custom_components.home_heating_optimisation.control.store.Store.async_load",
        return_value=["bad"],
    ):
        entry = MockConfigEntry(domain="home_heating_optimisation", data=controlled)
        entry.add_to_hass(hass)
        assert not await hass.config_entries.async_setup(entry.entry_id)


async def test_unload_blocks_late_calls(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("boiler", "auto")
    await hass.config_entries.async_unload(entry.entry_id)
    calls.clear()
    await c.boiler.async_refresh()
    assert calls == []


async def test_stale_thermostat_blocks_comfort_write(hass, controlled, sources, freezer):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    freezer.tick(timedelta(minutes=31))
    with pytest.raises(ServiceValidationError, match="stale"):
        await c.set_mode("study", "active")
    assert calls == []


@pytest.fixture
def telemetry(hass):
    hass.states.async_set("binary_sensor.heat", "off")
    return Telemetry(
        hass,
        [
            {
                "entity_id": "binary_sensor.heat",
                "topic": "boiler",
                "field": "heatingactive",
                "kind": "binary",
            }
        ],
    )


def send(telemetry, payload='{"heatingactive":"off"}', retain=False):
    telemetry.message(SimpleNamespace(topic="boiler", payload=payload, retain=retain))


def test_mqtt_unchanged_reports_refresh_and_silence_expires(hass, telemetry, freezer):
    send(telemetry)
    freezer.tick(timedelta(minutes=6))
    send(telemetry)
    value = read(
        {"binary_sensor.heat": telemetry.get("binary_sensor.heat")},
        "binary_sensor.heat",
        dt_util.utcnow(),
        kind="binary",
        max_age=300,
    )
    assert value.value is False and value.quality == "ok"
    assert (
        hass.states.get("binary_sensor.heat").last_reported
        < telemetry.get("binary_sensor.heat").last_reported
    )
    freezer.tick(timedelta(minutes=6))
    value = read(
        {"binary_sensor.heat": telemetry.get("binary_sensor.heat")},
        "binary_sensor.heat",
        dt_util.utcnow(),
        kind="binary",
        max_age=300,
    )
    assert value.quality == "stale"


def test_retained_missing_and_invalid_payloads_cannot_refresh(telemetry, freezer):
    send(telemetry, retain=True)
    assert telemetry.get("binary_sensor.heat").state == "unavailable"
    send(telemetry)
    before = telemetry.get("binary_sensor.heat").last_reported
    freezer.tick(timedelta(minutes=10))
    send(telemetry, "{}")
    send(telemetry, "bad")
    assert telemetry.get("binary_sensor.heat").last_reported == before
    send(telemetry, '{"heatingactive":"broken"}')
    assert telemetry.get("binary_sensor.heat").state == "unavailable"


def test_mqtt_never_masks_source_unavailability(hass, telemetry):
    send(telemetry)
    hass.states.async_set("binary_sensor.heat", "unavailable")
    assert telemetry.get("binary_sensor.heat").state == "unavailable"


async def test_missing_schedule_fetch_retries_in_five_minutes(hass, controlled, sources, freezer):
    _, c, _ = await start(hass, controlled)
    room = c.rooms["study"]
    calls = []

    async def fetch(call):
        calls.append(call.data)

    hass.services.async_register("ramses_cc", "get_zone_schedule", fetch)
    await room._maybe_fetch_ramses_schedule("climate.study")
    await hass.async_block_till_done()
    assert len(calls) == 1
    freezer.tick(timedelta(minutes=4))
    await room._maybe_fetch_ramses_schedule("climate.study")
    assert len(calls) == 1
    freezer.tick(timedelta(minutes=2))
    await room._maybe_fetch_ramses_schedule("climate.study")
    await hass.async_block_till_done()
    assert len(calls) == 2


async def test_expired_offline_schedule_cannot_be_used(hass, controlled, sources, freezer):
    _, c, _ = await start(hass, controlled)
    room = c.rooms["study"]
    room._store.set(
        "ramses_schedule",
        [{"day_of_week": 0, "switchpoints": [{"time_of_day": "00:00", "heat_setpoint": 20}]}],
    )
    room._store.set("ramses_schedule_saved_at", dt_util.utcnow().isoformat())
    assert room._ramses_schedule("climate.study") is not None
    freezer.tick(timedelta(hours=49))
    assert room._ramses_schedule("climate.study") is None


async def test_synchronous_echo_confirmation_time_follows_report(
    hass, controlled, sources, freezer
):
    _, controls, _ = await start(hass, controlled)
    await handover(controls)

    async def echo(call):
        freezer.tick(timedelta(seconds=1))
        hass.states.async_set(
            "climate.study",
            "heat",
            {
                "current_temperature": 18,
                "temperature": call.data["setpoint"],
                "mode": {"mode": "temporary_override"},
            },
        )

    hass.services.async_register("ramses_cc", "set_zone_mode", echo)
    await controls.set_mode("study", "active")
    data = controls.rooms["study"].data
    assert data.confirmed_target == data.sent_target
    assert data.sent_at < data.readback_at <= data.confirmed_at
