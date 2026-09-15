"""History correctness, storage failures, source changes and real HA lifecycle."""

import asyncio
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.core import State
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.home_heating_optimisation.analytics.backfill import reconstruct
from custom_components.home_heating_optimisation.analytics.observations import snapshot
from custom_components.home_heating_optimisation.analytics.store import (
    HistoryStore,
    source_signature,
    unpack_history,
)
from custom_components.home_heating_optimisation.const import DOMAIN
from tests.test_integration import entity_id, setup

BASE = datetime(2026, 9, 12, tzinfo=timezone.utc)


def state(entity, value, minute=0, **attrs):
    at = BASE + timedelta(minutes=minute)
    return State(entity, str(value), attrs, last_updated=at, last_reported=at)


def test_selected_demand_and_its_own_expiry(config):
    states = {
        "climate.study": state(
            "climate.study", "auto", current_temperature=18, temperature=20, hvac_action="idle"
        ),
        "sensor.study_demand": state("sensor.study_demand", 80, minute=20, unit_of_measurement="%"),
    }
    point = snapshot(states, BASE + timedelta(minutes=31), config, "°C")
    zone = point["zones"]["study"]
    assert zone["temperature"] is None
    assert zone["target"] == 20
    assert zone["active"] is True  # hvac_action idle does not override selected demand.
    assert zone["demand_valid_until"] == (BASE + timedelta(minutes=50)).timestamp()
    assert (
        snapshot(states, BASE + timedelta(minutes=51), config, "°C")["zones"]["study"]["active"]
        is None
    )


def test_history_ignores_unrecorded_heartbeats_and_context_expires(config):
    climate = state("climate.study", "auto", current_temperature=18, temperature=20)
    climate.last_reported = BASE + timedelta(minutes=40)
    point = snapshot(
        {"climate.study": climate, "binary_sensor.heating": state("binary_sensor.heating", "on")},
        BASE + timedelta(minutes=40),
        config,
        "°C",
    )
    assert point["zones"]["study"]["temperature"] is None
    assert point["context"]["heating_active"] is None


def test_replay_matches_live_at_every_event_including_unknown_and_target_edges(config):
    end = BASE + timedelta(hours=2)
    history = {
        "climate.study": [
            state("climate.study", "auto", current_temperature=18, temperature=20),
            state("climate.study", "auto", minute=11, current_temperature=19, temperature=16),
            state("climate.study", "unavailable", minute=22),
        ],
        "sensor.study_demand": [
            state("sensor.study_demand", 80, unit_of_measurement="%"),
            state("sensor.study_demand", "unavailable", minute=7),
            state("sensor.study_demand", 0, minute=13, unit_of_measurement="%"),
        ],
    }
    points, truncated = reconstruct(history, BASE, end, config, "°C")
    assert not truncated
    for point in points:
        at = datetime.fromtimestamp(point["time"], timezone.utc)
        states = {
            e: max(eligible, key=lambda s: s.last_updated)
            for e, rows in history.items()
            if (eligible := [s for s in rows if s.last_updated <= at])
        }
        expected = snapshot(states, at, config, "°C")
        assert {k: v for k, v in point.items() if k != "intent"} == {
            k: v for k, v in expected.items() if k != "intent"
        }
    assert points[-1]["zones"]["study"]["active"] is None
    assert any(p["time"] == (BASE + timedelta(minutes=11)).timestamp() for p in points)


def test_decision_context_is_allowlisted_and_does_not_change_measurements(config):
    states = {
        "climate.study": state("climate.study", "auto", current_temperature=18, temperature=20)
    }
    before = snapshot(states, BASE, config, "°C")
    config["rooms"][0]["decision_sensor"] = "sensor.ot"
    states["sensor.ot"] = state(
        "sensor.ot", "holding", reason="comfort satisfied", api_key="private", prompt="private"
    )
    after = snapshot(states, BASE, config, "°C")
    assert before["zones"] == after["zones"]
    assert after["intent"]["rooms"]["study"]["decision_sensor"]["attributes"] == {
        "reason": "comfort satisfied"
    }


async def test_storage_roundtrip_mapping_eras_and_live_precedence(hass, config):
    signature = source_signature(config, "°C")
    store = HistoryStore(hass, "test")
    await store.load(signature)
    point = snapshot({}, BASE, config, "°C")
    store.append(point)
    store.adjustments.append({"time": BASE.timestamp(), "note": "Valve checked"})
    store.merge([{**point, "zones": {}}])
    assert store.observations == [{k: v for k, v in point.items() if k != "intent"}]
    await store.save()
    restored = HistoryStore(hass, "test")
    await restored.load(signature)
    assert restored.observations == store.observations
    renamed = deepcopy(config)
    renamed["rooms"][0]["name"] = "New name"
    assert source_signature(renamed, "°C") == signature
    renamed["rooms"][0]["air_sensor"] = "sensor.new"
    await restored.load(source_signature(renamed, "°C"))
    assert restored.observations == []
    assert restored.previous_era["observations"] == store.observations
    assert restored.adjustments == store.adjustments


async def test_unsupported_storage_is_preserved(hass, config):
    store = HistoryStore(hass, "test")
    with (
        patch.object(store.backend, "async_load", AsyncMock(return_value={"schema": 999})),
        patch.object(store.backend, "async_save", AsyncMock()) as save,
    ):
        await store.load(source_signature(config, "°C"))
        store.append(snapshot({}, BASE, config, "°C"))
        await store.save()
    assert store.status == "storage_read_only"
    save.assert_not_called()


async def test_save_retry_and_mutation_during_save(hass, config):
    store = HistoryStore(hass, "test")
    await store.load(source_signature(config, "°C"))
    with patch.object(store.backend, "async_save", AsyncMock(side_effect=OSError("disk full"))):
        await store.save()
    assert store.status == "save_failed"
    saved = []

    async def saving(data):
        saved.append(deepcopy(data))
        store.append(snapshot({}, BASE, config, "°C"))

    with patch.object(store.backend, "async_save", saving):
        await store.save()
    assert unpack_history(saved[0])["observations"] == []
    assert len(store.observations) == 1
    await store.save()
    restored = HistoryStore(hass, "test")
    await restored.load(source_signature(config, "°C"))
    assert restored.observations == store.observations


async def test_enabled_analytics_report_journal_coverage_and_unload(hass, config, sources):
    config["analytics_enabled"] = True
    entry = await setup(hass, config)
    history = entry.runtime_data.analytics
    await history.task
    assert history.backfill_status == "recorder_unavailable"
    assert (
        hass.states.get(entity_id(hass, entry, "room:study:analytics_within_band")).state
        == "unknown"
    )
    await hass.services.async_call(
        DOMAIN,
        "record_adjustment",
        {"note": "Checked radiator", "kind": "lockshield", "room_id": "study"},
        blocking=True,
    )
    report = await hass.services.async_call(
        DOMAIN, "get_report", {}, blocking=True, return_response=True
    )
    assert report["adjustments"][0]["note"] == "Checked radiator"
    assert report["rooms"] == [{"id": "study", "name": "Study"}]
    assert report["analysis"]["window_end"] == report["comparison"]["current_end"]
    assert "Checked radiator" not in str(hass.states.async_all())
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "record_adjustment", {"note": "   "}, blocking=True)
    assert await hass.config_entries.async_unload(entry.entry_id)
    assert history.closed
    assert history.store.observations[-1]["zones"] == {}
    count = len(history.store.observations)
    hass.states.async_set("sensor.flow", 99)
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=20))
    await hass.async_block_till_done()
    assert len(history.store.observations) == count
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "get_report", {}, blocking=True, return_response=True
        )


async def test_backfill_does_not_block_observer_and_is_cancelled_on_unload(hass, config, sources):
    config["analytics_enabled"] = True
    waiting = asyncio.Event()

    async def backfill(*args):
        await waiting.wait()

    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill", backfill
    ):
        entry = await setup(hass, config)
        history = entry.runtime_data.analytics
        assert not history.task.done()
        assert hass.states.get(entity_id(hass, entry, "room:study:air")).state == "18.0"
        assert await hass.config_entries.async_unload(entry.entry_id)
    assert history.task.cancelled()


async def test_backfill_failure_keeps_observations_and_never_calls_devices(hass, config, sources):
    config["analytics_enabled"] = True
    with (
        patch(
            "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
            AsyncMock(side_effect=RuntimeError("Recorder failed")),
        ),
        patch("homeassistant.core.ServiceRegistry.async_call") as calls,
    ):
        entry = await setup(hass, config)
        history = entry.runtime_data.analytics
        await history.task
        assert history.backfill_status == "failed"
        assert history.store.observations
        calls.assert_not_called()


def test_chunked_replay_carries_real_timestamps_across_boundary(config):
    carry = {}
    history = {
        "climate.study": [
            state("climate.study", "auto", minute=1430, current_temperature=18, temperature=20)
        ]
    }
    split = BASE + timedelta(days=1)
    reconstruct(history, BASE, split, config, "°C", carry)
    points, _ = reconstruct({}, split, split + timedelta(hours=1), config, "°C", carry)
    assert points[0]["zones"]["study"]["temperature"] == 18
    assert points[-1]["zones"]["study"]["temperature"] is None
    assert points[-1]["zones"]["study"]["target"] == 20


@pytest.mark.parametrize(
    "key,value",
    [
        ("time", float("nan")),
        ("zones", {"study": {"temperature": "broken"}}),
        ("context_valid_until", {"supply": "broken"}),
    ],
)
async def test_corrupt_payload_preserved(hass, config, key, value):
    store = HistoryStore(hass, "test")
    point = snapshot({}, BASE, config, "°C")
    point[key] = value
    data = {"schema": 1, "observations": [point], "adjustments": []}
    with (
        patch.object(store.backend, "async_load", AsyncMock(return_value=data)),
        patch.object(store.backend, "async_save", AsyncMock()) as save,
    ):
        await store.load(source_signature(config, "°C"))
        await store.save()
    assert store.status == "storage_read_only"
    save.assert_not_called()


def test_retention_keeps_one_boundary_point_and_reports_count_cap(hass, config):
    store = HistoryStore(hass, "test")
    store.observations = [{"time": day * 86400} for day in range(20)]
    store.prune(20 * 86400)
    assert store.observations[0]["time"] == 4 * 86400
    with patch("custom_components.home_heating_optimisation.analytics.store.MAX_POINTS", 2):
        store.limit()
    assert len(store.observations) == 2
    assert store.truncated


async def test_options_enable_history_with_context_and_keep_observation_id(hass, config, sources):
    entry = await setup(hass, config)
    old_id = entity_id(hass, entry, "room:study:air")
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "mapping"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"zones": ["climate.study"]}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"name": "Study", "decision_sensor": "sensor.ot"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "analytics_enabled": True,
            "analysis_window_days": 3,
            "boiler_decision_sensor": "sensor.boiler",
        },
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    await entry.runtime_data.analytics.task
    assert entry.runtime_data.analytics.data["analysis"]["analysis_window_days"] == 3
    assert "sensor.ot" in entry.runtime_data.analytics.sources
    assert "sensor.boiler" in entry.runtime_data.analytics.sources
    assert entity_id(hass, entry, "room:study:air") == old_id
    report = entry.runtime_data.analytics.report()
    stats = report["analysis"]["zone_stats"]["study"]
    assert stats["within_band"] is None
    assert "within_band" in stats["suppressed_metrics"]
