"""Optional RF fetch failures must not block or flood actuator control."""

import asyncio
from datetime import timedelta
from unittest.mock import patch

import pytest
from homeassistant.core import SupportsResponse
from homeassistant.exceptions import HomeAssistantError, ServiceNotFound
from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.control.comfort.schedule_fetch import (
    ScheduleFetcher,
    classify_failure,
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
        return {"climate.test": {"schedule": [{"day_of_week": 0}]}}

    hass.services.async_register(
        "ramses_cc", "get_zone_schedule", fetch, supports_response=SupportsResponse.OPTIONAL
    )
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


async def test_successful_service_without_new_schedule_does_not_refresh_old_cache(hass, freezer):
    async def empty_fetch(call):
        pass

    hass.services.async_register("ramses_cc", "get_zone_schedule", empty_fetch)
    hass.states.async_set("climate.test", "heat", {"schedule": [{"day_of_week": 0}]})
    freezer.tick(timedelta(hours=1))
    fetcher = ScheduleFetcher(hass, MemoryStore())
    fetcher.request("climate.test", asyncio.Lock(), lambda: False)
    await fetcher.task
    assert fetcher.store.get("ramses_schedule_saved_at") is None
    assert fetcher.store.get("ramses_schedule_failures") == 1


async def test_bad_optional_fetch_metadata_cannot_abort_room_cycle(hass):
    async def fail(call):
        raise HomeAssistantError("radio unavailable")

    hass.services.async_register("ramses_cc", "get_zone_schedule", fail)
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    fetcher.store.set("ramses_schedule_failures", "invalid")
    lock = asyncio.Lock()
    fetcher.request("climate.test", lock, lambda: False)
    await fetcher.task
    assert fetcher.store.get("ramses_schedule_failures") == 1

    def broken_cache():
        raise ValueError("bad optional cache")

    fetcher.request("climate.test", lock, broken_cache)  # Must not propagate into control.
    assert fetcher.task.done()


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


# --- #15: failure classes, bounded background retries, unload, precedence -------


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (
            HomeAssistantError(
                "Failed to get zone schedule: Failed to decompress schedule fragments"
            ),
            "incomplete_fragments",
        ),
        (
            HomeAssistantError(
                "Failed to get zone schedule: Incomplete schedule fragment payload set"
            ),
            "incomplete_fragments",
        ),
        (
            HomeAssistantError("Failed to get zone schedule: Timeout waiting for reply"),
            "transport_timeout",
        ),
        (
            HomeAssistantError("Failed to obtain schedule within 15 secs"),
            "transport_timeout",
        ),
        (TimeoutError(), "transport_timeout"),
        (
            HomeAssistantError("Invalid schedule switchpoint binary block: 00ff"),
            "parser_error",
        ),
        (ValueError("bad payload"), "parser_error"),
        (ServiceNotFound("ramses_cc", "get_zone_schedule"), "unavailable"),
        (HomeAssistantError("Entity climate.x is unavailable"), "unavailable"),
        (RuntimeError("something else"), "unknown"),
        (None, "unavailable"),
    ],
)
def test_failure_class_from_exception(exc, expected):
    assert classify_failure(exc) == expected


async def test_failed_fetch_records_failure_class_and_next_retry(hass, freezer):
    async def fail(call):
        raise HomeAssistantError(
            "Failed to get zone schedule: Failed to decompress schedule fragments"
        )

    hass.services.async_register("ramses_cc", "get_zone_schedule", fail)
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    lock = asyncio.Lock()
    assert fetcher.snapshot() == {
        "status": "not_attempted",
        "failure_class": None,
        "attempts": 0,
        "next_retry_at": None,
    }
    started = dt_util.utcnow()
    fetcher.request("climate.test", lock, lambda: False)
    await fetcher.task
    snap = fetcher.snapshot()
    assert snap["status"] == "failed"
    assert snap["failure_class"] == "incomplete_fragments"
    assert snap["attempts"] == 1
    assert snap["next_retry_at"] == started + timedelta(minutes=5)
    # Consecutive failures: 5, 10, 20, 40, 60 and then a 60-minute ceiling.
    expected = [10, 20, 40, 60, 60, 60]
    for minutes in expected:
        freezer.tick(timedelta(minutes=61))
        started = dt_util.utcnow()
        fetcher.request("climate.test", lock, lambda: False)
        await fetcher.task
        assert fetcher.snapshot()["next_retry_at"] == started + timedelta(minutes=minutes)
    assert fetcher.snapshot()["attempts"] == 1 + len(expected)
    assert fetcher.snapshot()["failure_class"] == "incomplete_fragments"


async def test_successful_fetch_clears_failure_class_and_uses_daily_retry(hass, freezer):
    async def fetch(call):
        return {"climate.test": {"schedule": [{"day_of_week": 0}]}}

    hass.services.async_register(
        "ramses_cc", "get_zone_schedule", fetch, supports_response=SupportsResponse.OPTIONAL
    )
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    fetcher.store.set("ramses_schedule_failures", 3)
    fetcher.store.set("ramses_schedule_failure_class", "transport_timeout")
    fetcher.store.set("ramses_schedule_attempts", 3)
    started = dt_util.utcnow()
    fetcher.request("climate.test", asyncio.Lock(), lambda: False)
    await fetcher.task
    snap = fetcher.snapshot()
    assert snap["status"] == "ok"
    assert snap["failure_class"] is None
    assert snap["attempts"] == 0
    assert snap["next_retry_at"] == started + timedelta(hours=24)


async def test_transport_timeout_class_on_stalled_service(hass):
    never = asyncio.Event()

    async def stalled(call):
        await never.wait()

    hass.services.async_register("ramses_cc", "get_zone_schedule", stalled)
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    real_timeout = asyncio.timeout
    # The 45-second bound is driven by the loop clock, so shrink it for the test.
    with patch("asyncio.timeout", side_effect=lambda _: real_timeout(0.01)):
        fetcher.request("climate.test", asyncio.Lock(), lambda: False)
        await fetcher.task
    assert fetcher.snapshot()["status"] == "failed"
    assert fetcher.snapshot()["failure_class"] == "transport_timeout"
    never.set()


async def test_request_is_background_only(hass):
    """`request` returns while the radio transfer is still in flight."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def fetch(call):
        entered.set()
        await release.wait()

    hass.services.async_register("ramses_cc", "get_zone_schedule", fetch)
    hass.states.async_set("climate.test", "heat")
    fetcher = ScheduleFetcher(hass, MemoryStore())
    fetcher.request("climate.test", asyncio.Lock(), lambda: False)
    assert fetcher.task is not None and not fetcher.task.done()
    await entered.wait()
    assert fetcher.snapshot()["status"] == "fetching"
    assert not fetcher.task.done()  # Control returned to the caller mid-transfer.
    release.set()
    await fetcher.task
    assert fetcher.snapshot()["status"] == "failed"  # No schedule published: honest result.
    assert fetcher.snapshot()["failure_class"] == "unavailable"


async def test_snapshot_survives_corrupt_store_values(hass):
    fetcher = ScheduleFetcher(hass, MemoryStore())
    fetcher.store.set("ramses_schedule_attempts", "many")
    fetcher.store.set("ramses_schedule_next_retry_at", object())
    fetcher.store.set("ramses_schedule_status", "ok")
    snap = fetcher.snapshot()
    assert snap["attempts"] == 0
    assert snap["next_retry_at"] is None
    assert snap["status"] == "ok"


async def test_unload_cancels_fetch_and_leaves_no_task(hass, controlled, sources):
    _, controls, _ = await start(hass, controlled)
    started = asyncio.Event()
    never = asyncio.Event()

    async def stalled_fetch(call):
        started.set()
        await never.wait()

    hass.services.async_register("ramses_cc", "get_zone_schedule", stalled_fetch)
    room = controls.rooms["study"]
    await room._maybe_fetch_ramses_schedule("climate.study")
    await started.wait()
    task = room._schedule_fetcher.task
    assert not task.done()
    await room.async_shutdown()
    assert task.cancelled()
    assert room._schedule_fetcher.closed
    hub = hass.data["home_heating_optimisation"]["hub"]["data"]
    assert not hub.schedule_fetch_lock.locked()
    await room._maybe_fetch_ramses_schedule("climate.study")
    assert room._schedule_fetcher.task is task  # Nothing new after unload.


async def test_cloud_live_cache_precedence_and_48_hour_limit(hass, controlled, sources, freezer):
    _, controls, _ = await start(hass, controlled)
    room = controls.rooms["study"]
    live = [
        {
            "day_of_week": dow,
            "switchpoints": [{"time_of_day": "00:00", "heat_setpoint": 17.0}],
        }
        for dow in range(7)
    ]
    hass.states.async_set("climate.study", "heat", {"schedule": live, "temperature": 17.0})
    # 1. Cloud entity present: cloud wins even with a live RF schedule.
    zone = room._schedule()
    assert zone.schedule_setpoint == 20.0
    assert room._schedule_source == "evohome"
    # 2. Cloud unavailable (stale attributes retained): live RF schedule wins.
    hass.states.async_set(
        "climate.cloud", "unavailable", {"status": {"setpoints": {"this_sp_temp": 20}}}
    )
    zone = room._schedule()
    assert zone.schedule_setpoint == 17.0
    assert room._schedule_source == "ramses"
    assert room._store.get("ramses_schedule") == live
    saved_at = room._store.get("ramses_schedule_saved_at")
    # 3. RF entity unavailable during a radio fault: fresh cache still serves.
    hass.states.async_set("climate.study", "unavailable")
    zone = room._schedule()
    assert zone.schedule_setpoint == 17.0
    assert room._schedule_source == "ramses"
    freezer.tick(timedelta(hours=47))
    assert room._schedule().schedule_setpoint == 17.0
    # 4. Beyond 48 hours the cache is refused; no schedule rather than a stale one.
    freezer.tick(timedelta(hours=2))
    zone = room._schedule()
    assert zone.schedule_setpoint is None
    assert room._store.get("ramses_schedule_saved_at") == saved_at  # Fault did not renew the cache.
    # 5. Cloud recovery restores the cloud source without needing the radio.
    hass.states.async_set("climate.cloud", "auto", {"status": {"setpoints": {"this_sp_temp": 20}}})
    assert room._schedule().schedule_setpoint == 20.0
    assert room._schedule_source == "evohome"


async def test_room_decision_sensor_exposes_fetch_diagnostics(hass, controlled, sources):
    _, controls, _ = await start(hass, controlled)

    async def fail(call):
        raise HomeAssistantError("Failed to get zone schedule: Timeout waiting for reply")

    hass.services.async_register("ramses_cc", "get_zone_schedule", fail)
    hass.states.async_set("climate.study", "heat", {"temperature": 20.0})
    room = controls.rooms["study"]
    await room._maybe_fetch_ramses_schedule("climate.study")
    await room._schedule_fetcher.task
    await controls.refresh()
    data = room.data
    assert data.schedule_fetch_status == "failed"
    assert data.schedule_fetch_failure_class == "transport_timeout"
    assert data.schedule_fetch_attempts == 1
    assert data.schedule_next_retry_at is not None
    assert data.schedule_source == "evohome"  # Diagnostics never alter precedence.
    entity = controls.registry_id("study", "state")
    attrs = hass.states.get(entity).attributes
    assert attrs["schedule_fetch_status"] == "failed"
    assert attrs["schedule_fetch_failure_class"] == "transport_timeout"
    assert attrs["schedule_fetch_attempts"] == 1
    assert isinstance(attrs["schedule_next_retry_at"], str)
