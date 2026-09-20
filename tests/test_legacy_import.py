"""Radiator Analytics history import: read-only preview, private copy, retirement."""

import hashlib
import json
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.advisor.evidence import build_evidence, encode
from custom_components.home_heating_optimisation.analytics import legacy_import
from custom_components.home_heating_optimisation.analytics.coordinator import calculate
from custom_components.home_heating_optimisation.analytics.store import HistoryStore
from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_integration import setup

BASE = datetime(2026, 3, 1, tzinfo=timezone.utc)
NOTE = "Lockshield opened half a turn after the plumber visit"


def legacy_point(minute, zone="climate.study", temperature=18.0, target=20.0):
    at = (BASE + timedelta(minutes=minute)).timestamp()
    return {
        "time": at,
        "zones": {
            zone: {
                "temperature": temperature,
                "target": target,
                "active": True,
                "demand": 0.5,
                "demand_valid_until": at + 1800,
                "valid_until": at + 1800,
                "temperature_updated": at,
            }
        },
        "context": {
            "outdoor": 4.0,
            "supply": 55.0,
            "return": 45.0,
            "heating_active": True,
            "dhw_active": False,
        },
    }


def legacy_payload(zone="climate.study", points=6):
    return {
        "schema": 2,
        "observations": [legacy_point(m * 5, zone) for m in range(points)],
        "adjustments": [
            {"time": BASE.timestamp() + 60, "note": NOTE, "zone": zone, "kind": "lockshield"},
            {"time": BASE.timestamp() + 120, "note": "Boiler curve lowered", "kind": "other"},
        ],
        "legacy_sessions": [{"zone_id": zone, "duration_minutes": 30}],
        "source_config": {"monitored_zones": [zone]},
        "last_backfill": BASE.timestamp(),
        "backfill_status": "complete",
    }


def write_legacy(tmp_path, payload=None, raw=None, version=1):
    storage = tmp_path / ".storage"
    storage.mkdir(exist_ok=True)
    path = storage / "radiator_analytics"
    if raw is None:
        raw = json.dumps(
            {
                "version": version,
                "minor_version": 1,
                "key": "radiator_analytics",
                "data": payload if payload is not None else legacy_payload(),
            }
        ).encode()
    path.write_bytes(raw)
    return path, hashlib.sha256(raw).hexdigest()


async def start(hass, config, tmp_path):
    hass.config.config_dir = str(tmp_path)
    config = deepcopy(config)
    config["analytics_enabled"] = True
    entry = await setup(hass, config)
    await entry.runtime_data.analytics.task
    return entry


async def preview(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "preview_history_import", data, blocking=True, return_response=True
    )


async def do_import(hass, **data):
    return await hass.services.async_call(
        DOMAIN, "import_history", {"confirm": True, **data}, blocking=True, return_response=True
    )


async def test_preview_reads_valid_store_without_modifying_it(hass, config, sources, tmp_path):
    path, checksum = write_legacy(tmp_path)
    before = path.read_bytes()
    await start(hass, config, tmp_path)
    result = await preview(hass)
    assert result["status"] == "ready"
    assert result["checksum"] == checksum
    assert result["envelope_version"] == 1 and result["payload_schema"] == 2
    zone = result["zones"]["climate.study"]
    assert zone == {
        "room_id": "study",
        "status": "mapped",
        "basis": "climate_entity",
        "observation_count": 6,
        "first_time": BASE.timestamp(),
        "last_time": (BASE + timedelta(minutes=25)).timestamp(),
    }
    assert result["counts"]["importable_observations"] == 6
    assert result["counts"]["importable_notes"] == 2
    assert result["counts"]["archived_sessions"] == 1
    assert result["mapping"] == {"climate.study": "study"}
    assert "_import" not in result
    assert path.read_bytes() == before
    assert NOTE not in json.dumps(result)


@pytest.mark.parametrize(
    "kind,status",
    [("missing", "missing"), ("corrupt", "corrupt"), ("unsupported", "unsupported_version")],
)
async def test_preview_reports_missing_corrupt_and_unsupported(
    hass, config, sources, tmp_path, kind, status
):
    if kind == "corrupt":
        write_legacy(tmp_path, raw=b"{not json")
    elif kind == "unsupported":
        write_legacy(tmp_path, payload={**legacy_payload(), "schema": 1})
    await start(hass, config, tmp_path)
    result = await preview(hass)
    assert result["status"] == status
    assert "zones" not in result
    with pytest.raises(ServiceValidationError, match=status):
        await do_import(hass)


async def test_mapping_by_name_and_unmapped_zone(hass, config, sources, tmp_path):
    config = deepcopy(config)
    config["rooms"].append(
        {"id": "living", "name": "Lounge", "climate": "climate.lounge_trv", "air_sensor": None}
    )
    payload = legacy_payload()
    payload["observations"].append(legacy_point(30, "climate.old_lounge"))
    payload["observations"].append(legacy_point(35, "climate.garage"))
    write_legacy(tmp_path, payload)
    hass.states.async_set("climate.old_lounge", "auto", {"friendly_name": "lounge"})
    await start(hass, config, tmp_path)
    result = await preview(hass)
    assert result["zones"]["climate.old_lounge"]["room_id"] == "living"
    assert result["zones"]["climate.old_lounge"]["basis"] == "name"
    assert result["zones"]["climate.garage"]["status"] == "unmapped"
    assert result["counts"]["observations_with_unmapped_zone"] == 1
    assert result["status"] == "ready"
    explicit = await preview(hass, mapping={"climate.garage": "living"})
    assert explicit["zones"]["climate.garage"]["basis"] == "explicit"
    with pytest.raises(ServiceValidationError):
        await preview(hass, mapping={"climate.garage": "no_such_room"})


async def test_same_name_rooms_need_explicit_mapping(hass, config, sources, tmp_path):
    config = deepcopy(config)
    config["rooms"] += [
        {"id": "front", "name": "Bedroom", "climate": "climate.front", "air_sensor": None},
        {"id": "back", "name": "Bedroom", "climate": "climate.back", "air_sensor": None},
    ]
    payload = legacy_payload("climate.bedroom")
    write_legacy(tmp_path, payload)
    await start(hass, config, tmp_path)
    result = await preview(hass)
    assert result["status"] == "needs_mapping"
    assert result["zones"]["climate.bedroom"]["status"] == "needs_mapping"
    assert result["zones"]["climate.bedroom"]["candidates"] == ["back", "front"]
    with pytest.raises(ServiceValidationError, match="needs_mapping"):
        await do_import(hass)
    done = await do_import(hass, mapping={"climate.bedroom": "back"})
    assert done["imported"] is True
    assert done["mapping"] == {"climate.bedroom": "back"}


async def test_import_keeps_original_timestamps_and_analysis_unchanged(
    hass, config, sources, tmp_path
):
    path, checksum = write_legacy(tmp_path)
    original = path.read_bytes()
    entry = await start(hass, config, tmp_path)
    history = entry.runtime_data.analytics
    store = history.store
    now = dt_util.utcnow()
    live_before = deepcopy(store.observations)
    analysis_before = calculate(
        list(store.observations), list(store.adjustments), history.config, now, "UTC"
    )
    result = await do_import(hass)
    assert result["imported"] is True
    assert result["imported_observations"] == 6
    assert result["imported_notes"] == 2
    assert result["archived_sessions"] == 1
    era = store.imported_eras[0]
    assert [p["time"] for p in era["observations"]] == [
        (BASE + timedelta(minutes=5 * m)).timestamp() for m in range(6)
    ]
    assert era["observations"][0]["zones"] == {
        "study": {
            "temperature": 18.0,
            "target": 20.0,
            "active": True,
            "demand": 0.5,
            "demand_valid_until": BASE.timestamp() + 1800,
            "valid_until": BASE.timestamp() + 1800,
            "temperature_updated": BASE.timestamp(),
        }
    }
    assert era["signature"]["checksum"] == checksum
    assert era["signature"]["source"] == "radiator_analytics"
    assert era["signature"]["mapping"] == {"climate.study": "study"}
    assert era["archived_sessions"] == [{"zone_id": "climate.study", "duration_minutes": 30}]
    assert store.observations == live_before
    analysis_after = calculate(
        list(store.observations), list(store.adjustments), history.config, now, "UTC"
    )
    assert analysis_after == analysis_before
    notes = [n for n in store.adjustments if n.get("kind") == "imported"]
    assert len(notes) == 2
    assert notes[0] == {
        "time": BASE.timestamp() + 60,
        "note": NOTE,
        "kind": "imported",
        "source": "radiator_analytics",
        "legacy_kind": "lockshield",
        "room_id": "study",
    }
    assert notes[1]["room_id"] is None
    assert path.read_bytes() == original

    # Restart: the imported era and notes survive a reload of the private store.
    await store.save()
    restored = HistoryStore(hass, entry.entry_id)
    await restored.load(store.signature)
    assert restored.imported_eras == store.imported_eras
    assert [n for n in restored.adjustments if n.get("kind") == "imported"] == notes
    assert history.quality()["imported_era_count"] == 1
    assert history.quality()["imported_observation_count"] == 6


async def test_imported_notes_stay_out_of_diagnostics_evidence_and_states(
    hass, config, sources, tmp_path
):
    write_legacy(tmp_path)
    entry = await start(hass, config, tmp_path)
    await do_import(hass)
    heating = entry.runtime_data
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert NOTE not in json.dumps(diagnostics)
    assert diagnostics["analytics"]["imported_era_count"] == 1
    report = heating.analytics.report()
    assert any(n["note"] == NOTE for n in report["adjustments"])
    evidence = build_evidence(report, heating.house_report(), {}, "weekly_review")
    assert NOTE not in encode(evidence)
    assert "journal_note_text" in evidence["omitted"]
    assert NOTE not in str(hass.states.async_all())


async def test_repeat_import_is_skipped_by_checksum(hass, config, sources, tmp_path):
    write_legacy(tmp_path)
    entry = await start(hass, config, tmp_path)
    store = entry.runtime_data.analytics.store
    first = await do_import(hass)
    assert first["imported"] is True
    again = await do_import(hass)
    assert again["status"] == "already_imported" and again["imported"] is False
    assert len(store.imported_eras) == 1
    assert sum(n.get("kind") == "imported" for n in store.adjustments) == 2
    assert (await preview(hass))["status"] == "already_imported"


async def test_interrupted_import_leaves_store_and_legacy_untouched(
    hass, config, sources, tmp_path, hass_storage
):
    path, _ = write_legacy(tmp_path)
    original = path.read_bytes()
    entry = await start(hass, config, tmp_path)
    store = entry.runtime_data.analytics.store
    await store.save()
    key = store.backend.key
    saved = deepcopy(hass_storage[key])
    adjustments = deepcopy(store.adjustments)
    with patch.object(store.backend, "async_save", AsyncMock(side_effect=OSError("disk"))):
        with pytest.raises(HomeAssistantError, match="unchanged"):
            await do_import(hass)
    assert store.imported_eras == []
    assert store.adjustments == adjustments
    assert hass_storage[key] == saved
    assert path.read_bytes() == original
    # Storage recovers and the retry imports normally.
    result = await do_import(hass)
    assert result["imported"] is True


async def test_import_requires_confirmation_and_ready_storage(hass, config, sources, tmp_path):
    write_legacy(tmp_path)
    entry = await start(hass, config, tmp_path)
    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN, "import_history", {}, blocking=True, return_response=True
        )
    entry.runtime_data.analytics.store.status = "storage_read_only"
    with pytest.raises(HomeAssistantError, match="read-only"):
        await do_import(hass)


async def test_retire_refuses_until_matching_import_then_renames(hass, config, sources, tmp_path):
    path, checksum = write_legacy(tmp_path)
    original = path.read_bytes()
    await start(hass, config, tmp_path)
    with pytest.raises(ServiceValidationError, match="confirm"):
        await hass.services.async_call(
            DOMAIN, "retire_legacy_store", {}, blocking=True, return_response=True
        )
    with pytest.raises(ServiceValidationError, match="Import the current legacy store"):
        await hass.services.async_call(
            DOMAIN, "retire_legacy_store", {"confirm": True}, blocking=True, return_response=True
        )
    assert path.exists()
    await do_import(hass)
    # A store changed after import no longer matches the recorded checksum.
    path.write_bytes(original + b" ")
    with pytest.raises(ServiceValidationError, match="Import the current legacy store"):
        await hass.services.async_call(
            DOMAIN, "retire_legacy_store", {"confirm": True}, blocking=True, return_response=True
        )
    path.write_bytes(original)
    result = await hass.services.async_call(
        DOMAIN, "retire_legacy_store", {"confirm": True}, blocking=True, return_response=True
    )
    assert result["status"] == "retired" and result["checksum"] == checksum
    assert not path.exists()
    retired = list((tmp_path / ".storage").glob("radiator_analytics.retired-*"))
    assert len(retired) == 1 and retired[0].read_bytes() == original
    assert result["retired_path"] == f".storage/{retired[0].name}"
    with pytest.raises(ServiceValidationError, match="No legacy store"):
        await hass.services.async_call(
            DOMAIN, "retire_legacy_store", {"confirm": True}, blocking=True, return_response=True
        )


async def test_bounds_cap_points_and_retained_eras(hass, config, sources, tmp_path):
    write_legacy(tmp_path)
    entry = await start(hass, config, tmp_path)
    store = entry.runtime_data.analytics.store
    with patch("custom_components.home_heating_optimisation.analytics.legacy_import.MAX_POINTS", 4):
        result = await do_import(hass)
    assert result["counts"]["truncated"] is True
    assert result["imported_observations"] == 4
    assert store.imported_eras[0]["truncated"] is True
    assert (
        store.imported_eras[0]["observations"][0]["time"]
        == (BASE + timedelta(minutes=10)).timestamp()
    )
    checksums = [store.imported_eras[0]["signature"]["checksum"]]
    for extra in range(3):
        payload = legacy_payload(points=7 + extra)
        _, checksum = write_legacy(tmp_path, payload)
        checksums.append(checksum)
        assert (await do_import(hass))["imported"] is True
    assert [e["signature"]["checksum"] for e in store.imported_eras] == checksums[1:]
    assert len(store.imported_eras) == legacy_import.MAX_IMPORTED_ERAS


async def test_invalid_and_duplicate_points_are_skipped_not_imported(
    hass, config, sources, tmp_path
):
    payload = legacy_payload()
    payload["observations"].append({"time": "broken", "zones": {}, "context": {}})
    payload["observations"].append(deepcopy(payload["observations"][0]))
    broken = deepcopy(payload["observations"][1])
    broken["zones"]["climate.study"]["temperature"] = "hot"
    payload["observations"].append(broken)
    payload["adjustments"].append({"time": 5, "note": "   "})
    write_legacy(tmp_path, payload)
    await start(hass, config, tmp_path)
    result = await preview(hass)
    assert result["counts"]["skipped_observations"] == {"invalid": 2, "duplicate_time": 1}
    assert result["counts"]["importable_observations"] == 6
    assert result["counts"]["skipped_notes"] == 1


async def test_migration_events_are_journaled_defensively(hass, config, sources, tmp_path):
    write_legacy(tmp_path)
    entry = await start(hass, config, tmp_path)
    heating = entry.runtime_data
    recorded = []

    class Journal:
        def record(self, kind, **kwargs):
            recorded.append((kind, kwargs))

    heating.journal = Journal()
    await do_import(hass)
    assert recorded[0][0] == "migration"
    assert recorded[0][1]["data"]["status"] == "imported"
    assert NOTE not in json.dumps(recorded)

    class Broken:
        def record(self, *args, **kwargs):
            raise RuntimeError("journal down")

    heating.journal = Broken()
    write_legacy(tmp_path, legacy_payload(points=8))
    assert (await do_import(hass))["imported"] is True
