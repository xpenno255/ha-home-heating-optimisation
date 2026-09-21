"""A wedged actuator service or coordinator refresh must never hang control."""

import asyncio
from unittest.mock import patch

from custom_components.home_heating_optimisation.control.boiler import (
    coordinator as boiler_coordinator,
)
from custom_components.home_heating_optimisation.control.comfort import (
    coordinator as comfort_coordinator,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.boiler_setup import _setup as boiler_setup
from tests.control.test_runtime import controlled as controlled  # noqa: F401
from tests.control.test_runtime import start


async def _hang(*_args, **_kwargs):
    await asyncio.Event().wait()


def _wedge(hass, domain, service):
    """Replace a service handler with one that never returns (a wedged pipeline)."""
    hass.services.async_register(domain, service, _hang)


async def test_room_actuator_timeout_is_a_failed_write_and_releases_the_lock(
    hass, controlled, sources
):
    entry, c, _ = await start(hass, controlled)
    await handover(c)
    room = c.rooms["study"]
    journal = entry.runtime_data.journal
    with (
        patch.object(comfort_coordinator, "ACTUATOR_CALL_TIMEOUT", 0.05),
    ):
        _wedge(hass, "ramses_cc", "set_zone_mode")
        await c.set_mode("study", "active")
    assert room.mode == "active" and c.settings.get("modes")["study"] == "active"
    assert room.data.write_status == "service_timeout"
    assert not room._cycle_lock.locked()
    results = journal.events(kinds=["command_result"], room_id="study")
    assert results[-1]["data"]["outcome"] == "service_timeout"
    assert room._memory().last_written_at is None  # memory not advanced
    assert "will retry" in room.data.reason

    # The next cycle runs normally and the retry reaches the (now healthy) service.
    calls = []

    async def climate(call):
        calls.append(dict(call.data))

    hass.services.async_register("ramses_cc", "set_zone_mode", climate)
    await room.async_refresh()
    assert calls and calls[-1]["mode"] == "temporary_override"
    assert room.data.write_status != "service_timeout"


async def test_room_cycle_timeout_returns_last_good_data_and_releases_the_lock(
    hass, controlled, sources
):
    entry, c, _ = await start(hass, controlled)
    room = c.rooms["study"]
    await room.async_refresh()
    good = room.data
    journal = entry.runtime_data.journal
    with (
        patch.object(comfort_coordinator, "CYCLE_TIMEOUT", 0.05),
        patch.object(room, "_cycle", side_effect=_hang),
    ):
        await room.async_refresh()
    assert room.data is good
    assert "cycle timed out; showing last good data" in room.data.fallbacks
    assert not room._cycle_lock.locked()
    decisions = journal.events(kinds=["decision"], room_id="study")
    assert decisions[-1]["data"]["outcome"] == "cycle_timeout"
    await room.async_refresh()  # scheduling continues; a normal cycle replaces the note
    assert "cycle timed out; showing last good data" not in room.data.fallbacks


async def test_set_mode_returns_when_refresh_is_wedged(hass, controlled, sources):
    from custom_components.home_heating_optimisation.control import runtime

    _, c, _ = await start(hass, controlled)
    await handover(c)
    room = c.rooms["study"]
    with (
        patch.object(runtime, "REFRESH_WAIT_TIMEOUT", 0.05),
        patch.object(room, "async_refresh", side_effect=_hang),
    ):
        await asyncio.wait_for(c.set_mode("study", "active"), 5)
        await asyncio.wait_for(c.refresh(), 5)
    assert room.mode == "active" and c.settings.get("modes")["study"] == "active"
    assert not c.lock.locked()


async def test_boiler_actuator_timeout_is_a_failed_write_and_releases_the_lock(hass):
    entry, calls = await boiler_setup(hass)
    boiler = entry.runtime_data
    boiler.override = "auto"
    with (
        patch.object(boiler_coordinator, "ACTUATOR_CALL_TIMEOUT", 0.05),
    ):
        _wedge(hass, "number", "set_value")
        await boiler.async_refresh()
    calls_after_timeout = list(calls)
    assert boiler.data.write_status == "service_timeout"
    assert boiler.data.last_written_setpoint is None  # memory not advanced
    assert not boiler._cycle_lock.locked()
    assert calls_after_timeout == []

    async def healthy(call):
        calls.append(dict(call.data))
        hass.states.async_set(call.data["entity_id"], call.data["value"])

    hass.services.async_register("number", "set_value", healthy)
    await boiler.async_refresh()  # retry on the next cycle reaches the healthy service
    assert calls and boiler.data.write_status != "service_timeout"


async def test_boiler_cycle_timeout_returns_last_good_data_and_releases_the_lock(hass):
    entry, _ = await boiler_setup(hass)
    boiler = entry.runtime_data
    await boiler.async_refresh()
    good = boiler.data
    with (
        patch.object(boiler_coordinator, "CYCLE_TIMEOUT", 0.05),
        patch.object(boiler, "_cycle", side_effect=_hang),
    ):
        await boiler.async_refresh()
    assert boiler.data is good
    assert "cycle timed out; showing last good data" in boiler.data.fallbacks
    assert not boiler._cycle_lock.locked()
