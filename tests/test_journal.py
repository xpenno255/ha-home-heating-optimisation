"""Journal API contract, bounds, privacy, persistence and failure isolation."""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.home_heating_optimisation.const import DOMAIN, VERSION
from custom_components.home_heating_optimisation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.home_heating_optimisation.journal.const import (
    KINDS,
    MAX_EVENTS,
    MAX_SAVE_FAILURES,
    QUERY_MAX_EVENTS,
    RETENTION_SECONDS,
    SAVE_DELAY_SECONDS,
    SCHEMA_VERSION,
)
from custom_components.home_heating_optimisation.journal.coordinator import Journal, config_era
from custom_components.home_heating_optimisation.journal.store import JournalStore
from tests.test_integration import entity_id, setup

STORE = "custom_components.home_heating_optimisation.journal.store.Store"


def key(entry):
    return f"{DOMAIN}.{entry.entry_id}.journal"


async def test_record_returns_versioned_event_with_default_provenance(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    assert journal is not None and journal.status == "ready"
    before = len(journal.store.events)
    event = journal.record(
        "adjustment_note",
        room_id="study",
        scope="study",
        origin="user",
        data={"private_note": "moved sofa", "value": float("nan")},
        provenance={"schedule_source": "ramses"},
    )
    assert event["schema"] == SCHEMA_VERSION
    assert len(event["id"]) == 32
    assert event["kind"] == "adjustment_note"
    assert event["room_id"] == "study" and event["scope"] == "study"
    assert event["origin"] == "user"
    assert event["data"]["outcome"] == "unknown"
    assert event["data"]["value"] is None  # non-finite numbers are not stored as NaN
    assert event["provenance"]["controller_version"] == VERSION
    assert event["provenance"]["control_schema"] is None
    assert event["provenance"]["config_era"] == config_era(entry.runtime_data.config)
    assert event["provenance"]["schedule_source"] == "ramses"
    assert len(journal.store.events) == before + 1
    # Unknown origin and unknown kind are explicit, never guessed.
    assert journal.record("decision", origin="mystery")["origin"] == "unknown"
    assert journal.record("not_a_kind") is None
    assert set(KINDS) >= {"decision", "command_sent", "adjustment_note", "migration"}


async def test_events_filter_and_export_strip_private_text(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    journal.record("adjustment_note", room_id="study", origin="user", data={"private_note": "x"})
    journal.record("decision", room_id="study", data={"reason": "test", "note": "free text"})
    journal.record("mode_change", scope="boiler", data={"to": "auto"})
    notes = journal.events(kinds=["adjustment_note"])
    assert [e["kind"] for e in notes] == ["adjustment_note"]
    assert len(journal.events(room_id="study")) == 2
    assert journal.events(limit=1)[0]["kind"] == "mode_change"
    now = dt_util.utcnow().timestamp()
    assert journal.events(since=now + 1) == []
    assert len(journal.events(until=now + 1)) >= 3
    exported = journal.export()
    assert "private_note" not in str(exported) and "free text" not in str(exported)
    assert any("private_note" in e["data"] for e in journal.export(include_private=True))
    times = [e["time"] for e in journal.events()]
    assert times == sorted(times)


async def test_events_accept_datetime_epoch_and_iso_bounds(hass, config, sources, freezer):
    """Callers pass datetimes; stored times are epochs. Every form must filter correctly."""
    freezer.move_to("2026-09-14T10:00:00+00:00")
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    early = journal.record("decision", room_id="study")
    freezer.tick(timedelta(minutes=10))
    late = journal.record("command_sent", room_id="study")
    boundary = datetime(2026, 9, 14, 10, 5, tzinfo=timezone.utc)
    assert [e["id"] for e in journal.events(since=boundary)] == [late["id"]]
    assert [e["id"] for e in journal.events(until=boundary)] == [early["id"]]
    # Naive datetimes are read as UTC; epochs and ISO strings are accepted too.
    assert [e["id"] for e in journal.events(since=boundary.replace(tzinfo=None))] == [late["id"]]
    assert [e["id"] for e in journal.events(since=boundary.timestamp())] == [late["id"]]
    assert [e["id"] for e in journal.events(since=boundary.isoformat())] == [late["id"]]
    assert journal.events(since=boundary, until=boundary + timedelta(hours=1)) == [
        journal.events(kinds=["command_sent"])[0]
    ]
    assert journal.events(since=dt_util.utcnow() + timedelta(days=1)) == []
    with pytest.raises(ValueError):
        journal.events(since="not a time")


async def test_event_recorded_during_in_flight_save_is_persisted(hass, hass_storage):
    """A record that lands while HA is writing must leave the store dirty for the next save."""
    store = JournalStore(hass, "test")
    gate = asyncio.Event()
    written = []

    async def slow_save(data):
        written.append(data)
        await gate.wait()

    def event(name):
        return {"id": name, "time": dt_util.utcnow().timestamp(), "kind": "decision"}

    store.append(event("first"))
    with patch.object(store.backend, "async_save", side_effect=slow_save):
        task = hass.async_create_task(store.save())
        await asyncio.sleep(0)
        assert written and [e["id"] for e in written[0]["events"]] == ["first"]
        store.append(event("second"))
        gate.set()
        await task
    assert store.dirty is True, "late event must not be marked as saved"
    await store.save()
    saved = hass_storage[f"{DOMAIN}.test.journal"]["data"]["events"]
    assert [e["id"] for e in saved] == ["first", "second"]
    assert store.dirty is False


async def test_journal_reschedules_save_for_events_recorded_mid_write(
    hass, config, sources, hass_storage
):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    journal.record("decision", data={"which": "first"})
    real_save = journal.store.backend.async_save
    late = []

    async def record_while_saving(data):
        await real_save(data)
        late.append(journal.record("decision", data={"which": "late"}))

    with patch.object(journal.store.backend, "async_save", side_effect=record_while_saving):
        await journal.flush()
    saved = {e["id"] for e in hass_storage[key(entry)]["data"]["events"]}
    assert late[0]["id"] not in saved and journal.store.dirty
    assert journal._save_cancel is not None, "debounced save must be rescheduled"
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY_SECONDS + 1))
    await hass.async_block_till_done()
    assert late[0]["id"] in {e["id"] for e in hass_storage[key(entry)]["data"]["events"]}
    assert not journal.store.dirty


async def test_debounced_save_persists_and_reload_is_idempotent(
    hass, config, sources, hass_storage
):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    event = journal.record("decision", room_id="study", data={"reason": "r"})
    assert key(entry) not in hass_storage  # not written synchronously
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=SAVE_DELAY_SECONDS + 1))
    await hass.async_block_till_done()
    saved = hass_storage[key(entry)]["data"]
    assert saved["schema"] == SCHEMA_VERSION
    assert event["id"] in {e["id"] for e in saved["events"]}
    count = len(saved["events"])
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert journal.closed and journal.record("decision") is None
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    restored = entry.runtime_data.journal
    ids = [e["id"] for e in restored.store.events]
    assert event["id"] in ids and len(ids) == len(set(ids))
    assert len([e for e in restored.store.events if e["id"] == event["id"]]) == 1
    assert len(restored.store.events) >= count


async def test_unload_flushes_unsaved_events(hass, config, sources, hass_storage):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    event = journal.record("handover", scope="system", origin="service")
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert event["id"] in {e["id"] for e in hass_storage[key(entry)]["data"]["events"]}


async def test_corrupt_store_is_preserved_and_journal_becomes_read_only(
    hass, config, sources, hass_storage
):
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", unique_id=DOMAIN, data=config)
    entry.add_to_hass(hass)
    hass_storage[key(entry)] = {"version": 1, "key": key(entry), "data": ["not", "a", "journal"]}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    journal = entry.runtime_data.journal
    assert journal.status == "storage_read_only"
    assert journal.record("decision") is None
    await journal.flush()
    assert hass_storage[key(entry)]["data"] == ["not", "a", "journal"]
    assert hass.states.get(entity_id(hass, entry, "system:journal_status")).state == (
        "storage_read_only"
    )


async def test_unknown_historical_kind_is_preserved_on_load(hass, hass_storage):
    store = JournalStore(hass, "test")
    old = {
        "schema": SCHEMA_VERSION,
        "id": "a" * 32,
        "time": dt_util.utcnow().timestamp(),
        "kind": "retired_kind",
        "room_id": None,
        "scope": "system",
        "origin": "controller",
        "data": {"outcome": "unknown"},
        "provenance": {},
    }
    hass_storage[f"{DOMAIN}.test.journal"] = {
        "version": 1,
        "key": f"{DOMAIN}.test.journal",
        "data": {"schema": SCHEMA_VERSION, "events": [old], "truncated": False},
    }
    await store.load()
    assert store.status == "ready" and store.events == [old]


async def test_save_failure_is_reported_and_keeps_events(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    journal.record("decision")
    with patch.object(journal.store.backend, "async_save", side_effect=OSError("disk full")):
        await journal.flush()
    assert journal.status == "save_failed"
    assert journal.store.events
    assert journal.record("decision") is not None
    await journal.flush()
    assert journal.status == "ready"


async def test_persistent_save_failure_backs_off_and_stops_retrying(
    hass, config, sources, hass_storage
):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    delays = []
    module = "custom_components.home_heating_optimisation.journal.coordinator.async_call_later"
    from homeassistant.helpers.event import async_call_later as real_call_later

    def call_later(hass_, delay, job):
        delays.append(delay)
        return real_call_later(hass_, delay, job)

    with (
        patch(module, side_effect=call_later),
        patch.object(journal.store.backend, "async_save", side_effect=OSError("disk full")),
    ):
        first = journal.record("decision", data={"which": "first"})
        assert delays == [SAVE_DELAY_SECONDS]
        for _ in range(MAX_SAVE_FAILURES + 3):
            if journal._save_cancel is None:
                break
            await journal.flush()
        assert journal.status == "save_failed" and first["id"] in {
            e["id"] for e in journal.store.events
        }
        assert journal._save_cancel is None, "retries must stop after repeated failures"
        assert journal._save_failures == MAX_SAVE_FAILURES
    # First timer from record, then 30 s, 1, 2, 4, 8 min and capped at 8 min.
    assert delays == [30, 30, 60, 120, 240, 480, 480, 480, 480, 480]
    # A later record schedules again and a successful save resets the counter.
    with patch(module, side_effect=call_later):
        second = journal.record("decision", data={"which": "second"})
    assert delays[-1] == SAVE_DELAY_SECONDS and journal._save_cancel is not None
    await journal.flush()
    assert journal.status == "ready" and journal._save_failures == 0
    saved = {e["id"] for e in hass_storage[key(entry)]["data"]["events"]}
    assert {first["id"], second["id"]} <= saved
    assert not journal.store.dirty


async def test_bounds_and_retention(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    store = journal.store
    store.events = []
    now = dt_util.utcnow().timestamp()
    stale = journal.record("decision")
    stale["time"] = now - RETENTION_SECONDS - 1
    store.events.sort(key=lambda e: e["time"])
    store.prune(now)
    assert stale not in store.events
    for _ in range(MAX_EVENTS + 5):
        store.events.append({**stale, "time": now})
    store.limit()
    assert len(store.events) == MAX_EVENTS and store.truncated
    assert journal.attributes()["truncated"]


async def test_service_status_sensor_and_diagnostics_expose_counts_only(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    journal.store.events = []
    journal.record("adjustment_note", room_id="study", origin="user", data={"private_note": "s"})
    journal.record("decision", room_id="study", data={"reason": "why"})
    await entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    state = hass.states.get(entity_id(hass, entry, "system:journal_status"))
    assert state.state == "ready"
    assert state.attributes["event_count"] == 2
    assert state.attributes["counts_by_kind"] == {"adjustment_note": 1, "decision": 1}
    assert state.attributes["oldest_at"] <= state.attributes["newest_at"]
    assert "why" not in str(state.attributes) and "private_note" not in str(state.attributes)
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["journal"] == {
        "status": "ready",
        "event_count": 2,
        "counts_by_kind": {"adjustment_note": 1, "decision": 1},
    }
    result = await hass.services.async_call(
        DOMAIN, "get_journal", {}, blocking=True, return_response=True
    )
    assert result["count"] == 2 and result["status"] == "ready" and not result["truncated"]
    assert "private_note" not in str(result)
    result = await hass.services.async_call(
        DOMAIN,
        "get_journal",
        {"kinds": ["adjustment_note"], "room_id": "study", "include_private": True},
        blocking=True,
        return_response=True,
    )
    assert result["count"] == 1 and result["events"][0]["data"]["private_note"] == "s"
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN, "get_journal", {"hours": 721}, blocking=True, return_response=True
        )


async def test_service_caps_response_size(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    journal.store.events = []
    now = dt_util.utcnow().timestamp()
    template = journal.record("decision")
    journal.store.events = [
        {**template, "id": f"{i:032x}", "time": now - i} for i in range(QUERY_MAX_EVENTS + 10)
    ]
    result = await hass.services.async_call(
        DOMAIN, "get_journal", {}, blocking=True, return_response=True
    )
    assert result["count"] == QUERY_MAX_EVENTS and result["truncated"]


async def test_disabled_journal_records_nothing(hass, config, sources):
    config["journal_enabled"] = False
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    assert journal.status == "disabled"
    assert journal.record("decision") is None
    assert hass.states.get(entity_id(hass, entry, "system:journal_status")).state == "disabled"
    result = await hass.services.async_call(
        DOMAIN, "get_journal", {}, blocking=True, return_response=True
    )
    assert result == {"events": [], "count": 0, "truncated": False, "status": "disabled"}
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["journal"]["status"] == "disabled"


async def test_load_exception_does_not_block_setup(hass, config, sources):
    with patch(f"{STORE}.async_load", side_effect=RuntimeError("storage exploded")):
        entry = await setup(hass, config)
    assert entry.runtime_data.journal.status == "storage_read_only"
    assert entry.runtime_data.journal.record("decision") is None


async def test_record_never_raises(hass, config, sources):
    entry = await setup(hass, config)
    journal = entry.runtime_data.journal
    with patch.object(journal.store, "append", side_effect=RuntimeError("boom")):
        assert journal.record("decision") is None
    assert journal.record("decision") is not None


def test_config_era_tracks_actuators_and_rooms_only():
    base = {
        "rooms": [{"id": "a", "name": "A"}],
        "control": {
            "boiler": {"config": {"flow_setpoint_entity": "number.x"}},
            "rooms": {"a": {"config": {"primary_climate": "climate.a"}}},
        },
    }
    renamed = {**base, "rooms": [{"id": "a", "name": "Renamed"}]}
    rebound = {
        **base,
        "control": {**base["control"], "boiler": {"config": {"flow_setpoint_entity": "number.y"}}},
    }
    assert config_era(base) == config_era(renamed)
    assert config_era(base) != config_era(rebound)
    assert config_era({"rooms": []}) != config_era(base)
    assert isinstance(Journal, type)
