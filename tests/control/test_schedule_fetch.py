"""Optional RF fetch failures must not block or flood actuator control."""

import asyncio
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError

from custom_components.home_heating_optimisation.control.comfort.schedule_fetch import (
    ScheduleFetcher,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start


class MemoryStore:
    def __init__(self):
        self.data = {}

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        self.data[key] = value

    async def async_save(self):
        pass


async def test_missing_schedule_backoff_catches_service_failure(hass, freezer):
    calls = []

    async def fail(call):
        calls.append(call)
        raise HomeAssistantError("radio timeout")

    hass.services.async_register("ramses_cc", "get_zone_schedule", fail)
    hass.states.async_set("climate.test", "heat")
    store = MemoryStore()
    fetcher = ScheduleFetcher(hass, store)
    lock = asyncio.Lock()
    for interval in (5, 10, 20, 40, 60, 60):
        fetcher.request("climate.test", lock, lambda: False)
        await fetcher.task  # Error is contained; no service task escapes to HA's error log.
        count = len(calls)
        freezer.tick(timedelta(minutes=interval - 1))
        fetcher.request("climate.test", lock, lambda: False)
        await fetcher.task
        assert len(calls) == count
        freezer.tick(timedelta(minutes=1))
    assert len(calls) == 6
    assert store.get("ramses_schedule_failures") == 5


async def test_rooms_serialize_background_fetches_and_cancel_on_unload(hass):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def fetch(call):
        calls.append(call.data["entity_id"])
        started.set()
        await release.wait()

    hass.services.async_register("ramses_cc", "get_zone_schedule", fetch)
    lock = asyncio.Lock()
    first = ScheduleFetcher(hass, MemoryStore())
    second = ScheduleFetcher(hass, MemoryStore())
    for entity in ("climate.first", "climate.second"):
        hass.states.async_set(entity, "heat")
    first.request("climate.first", lock, lambda: False)
    await started.wait()
    first_task = first.task
    first.request("climate.first", lock, lambda: False)
    assert first.task is first_task  # One pending fetch per room, no queue growth.
    second.request("climate.second", lock, lambda: False)
    await asyncio.sleep(0)
    assert calls == ["climate.first"]  # Second room did not start a competing RF transfer.
    await second.stop()
    await first.stop()
    assert first.task.cancelled() and second.task.cancelled()
    assert not lock.locked()
    first.request("climate.first", lock, lambda: False)
    assert first.task is first_task  # Closed controller cannot launch work.


async def test_unavailable_zone_is_skipped_even_after_waiting_for_radio(hass):
    calls = []

    async def fetch(call):
        calls.append(call)

    hass.services.async_register("ramses_cc", "get_zone_schedule", fetch)
    fetcher = ScheduleFetcher(hass, MemoryStore())
    lock = asyncio.Lock()
    fetcher.request("climate.missing", lock, lambda: False)
    assert fetcher.task is None
    hass.states.async_set("climate.test", "unavailable")
    fetcher.request("climate.test", lock, lambda: False)
    assert fetcher.task is None
    hass.states.async_set("climate.test", "heat")
    await lock.acquire()
    fetcher.request("climate.test", lock, lambda: False)
    await asyncio.sleep(0)
    hass.states.async_set("climate.test", "unavailable")
    lock.release()
    await fetcher.task
    assert calls == []
    assert fetcher.store.get("ramses_schedule_requested_at") is None


async def test_successful_cached_schedule_uses_daily_refresh(hass, freezer):
    calls = []
    cached = False

    async def fetch(call):
        nonlocal cached
        calls.append(call)
        cached = True
        hass.states.async_set("climate.test", "heat", {"schedule": [{"day_of_week": 0}]})

    hass.services.async_register("ramses_cc", "get_zone_schedule", fetch)
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    lock = asyncio.Lock()
    fetcher.request("climate.test", lock, lambda: cached)
    await fetcher.task
    assert fetcher.store.get("ramses_schedule_failures") == 0
    first_saved_at = fetcher.store.get("ramses_schedule_saved_at")
    freezer.tick(timedelta(hours=23))
    fetcher.request("climate.test", lock, lambda: cached)
    assert len(calls) == 1
    freezer.tick(timedelta(hours=1))
    fetcher.request("climate.test", lock, lambda: cached)
    await fetcher.task
    assert len(calls) == 2
    assert fetcher.store.get("ramses_schedule_saved_at") != first_saved_at


async def test_slow_rf_schedule_does_not_delay_radiator_command(hass, controlled, sources):
    _, controls, calls = await start(hass, controlled)
    await handover(controls)
    started = asyncio.Event()
    never = asyncio.Event()

    async def stalled_fetch(call):
        started.set()
        await never.wait()

    hass.services.async_register("ramses_cc", "get_zone_schedule", stalled_fetch)
    room = controls.rooms["study"]
    await room._maybe_fetch_ramses_schedule("climate.study")
    await started.wait()
    await controls.set_mode("study", "active")
    assert any(kind == "ramses" for kind, _ in calls)
    assert not room._schedule_fetcher.task.done()
    await room._schedule_fetcher.stop()
