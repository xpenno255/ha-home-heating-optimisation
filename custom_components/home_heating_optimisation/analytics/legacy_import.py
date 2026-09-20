"""Read-only preview and explicit import of Radiator Analytics history.

The legacy store is never modified by preview or import. Compatible per-sample
observations are copied, with their original timestamps, into a separate
``imported_eras`` collection that the analyzer never reads as current history.
Pre-aggregated legacy session metrics are archived, not re-labelled as measurements.
"""

import hashlib
import json
import logging
import os
from copy import deepcopy
from datetime import datetime, timezone

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util

from .const import MAX_ADJUSTMENTS, MAX_ARCHIVED_SESSIONS, MAX_IMPORTED_ERAS, MAX_POINTS
from .store import finite, validate_point

LOGGER = logging.getLogger(__name__)

SOURCE = "radiator_analytics"
LEGACY_FILE = ".storage/radiator_analytics"
SUPPORTED_ENVELOPE = 1
SUPPORTED_SCHEMA = 2
LEGACY_SEMANTICS = "radiator_analytics/hvac_action_demand/last_updated_freshness"
ZONE_KEYS = (
    "temperature",
    "target",
    "active",
    "demand",
    "valid_until",
    "demand_valid_until",
    "temperature_updated",
)
CONTEXT_KEYS = ("outdoor", "supply", "return", "heating_active", "dhw_active")
LEGACY_KINDS = ("lockshield", "boiler_setting", "sensor_change", "other")


def legacy_path(hass):
    return hass.config.path(*LEGACY_FILE.split("/"))


def read_legacy(path):
    """Executor: raw bytes, checksum and decoded payload; never writes."""
    try:
        with open(path, "rb") as handle:
            raw = handle.read()
    except FileNotFoundError:
        return {"status": "missing"}
    except OSError as err:
        return {"status": "unreadable", "error": type(err).__name__}
    checksum = hashlib.sha256(raw).hexdigest()
    try:
        envelope = json.loads(raw)
    except ValueError:
        return {"status": "corrupt", "checksum": checksum, "size_bytes": len(raw)}
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        return {"status": "corrupt", "checksum": checksum, "size_bytes": len(raw)}
    payload = envelope["data"]
    versions = {
        "envelope_version": envelope.get("version"),
        "payload_schema": payload.get("schema"),
    }
    if versions["envelope_version"] != SUPPORTED_ENVELOPE or (
        versions["payload_schema"] != SUPPORTED_SCHEMA
    ):
        return {
            "status": "unsupported_version",
            "checksum": checksum,
            "size_bytes": len(raw),
            **versions,
        }
    if not isinstance(payload.get("observations", []), list) or not isinstance(
        payload.get("adjustments", []), list
    ):
        return {"status": "corrupt", "checksum": checksum, "size_bytes": len(raw), **versions}
    return {
        "status": "ready",
        "checksum": checksum,
        "size_bytes": len(raw),
        "payload": payload,
        **versions,
    }


def slug(zone):
    return zone.split(".", 1)[1] if "." in zone else zone


def normalise(name):
    return " ".join(str(name).replace("_", " ").lower().split())


def propose_mapping(zones, rooms, zone_names):
    """Exact climate/id match first, then unique case-insensitive name, else unmapped."""
    by_climate = {r.get("climate"): r["id"] for r in rooms if r.get("climate")}
    by_id = {r["id"]: r["id"] for r in rooms}
    by_name = {}
    for room in rooms:
        by_name.setdefault(normalise(room.get("name", "")), []).append(room["id"])
    proposal = {}
    for zone in zones:
        entry = {"room_id": None, "status": "unmapped", "basis": None}
        if zone in by_climate:
            entry.update(room_id=by_climate[zone], status="mapped", basis="climate_entity")
        elif slug(zone) in by_id:
            entry.update(room_id=by_id[slug(zone)], status="mapped", basis="room_id")
        else:
            candidates = set()
            for name in (zone_names.get(zone), slug(zone)):
                if name is not None:
                    candidates.update(by_name.get(normalise(name), []))
            if len(candidates) == 1:
                entry.update(room_id=candidates.pop(), status="mapped", basis="name")
            elif candidates:
                entry.update(status="needs_mapping", candidates=sorted(candidates))
        proposal[zone] = entry
    return proposal


def convert_point(point, mapping):
    """Copy a legacy observation into the current shape; None means skip."""
    if (
        not isinstance(point, dict)
        or not finite(point.get("time"))
        or not isinstance(point.get("zones"), dict)
        or not isinstance(point.get("context"), dict)
    ):
        return None, "invalid"
    zones = {}
    for zone, values in point["zones"].items():
        room = mapping.get(zone)
        if room is None or not isinstance(values, dict):
            continue
        zones[room] = {k: values.get(k) for k in ZONE_KEYS}
    converted = {
        "time": point["time"],
        "zones": zones,
        "context": {k: point["context"].get(k) for k in CONTEXT_KEYS},
    }
    try:
        validate_point(converted)
    except ValueError:
        return None, "invalid"
    return converted, None


def convert_note(note, mapping):
    if not isinstance(note, dict) or not finite(note.get("time")):
        return None
    text = note.get("note")
    if not isinstance(text, str) or not text.strip():
        return None
    zone = note.get("zone")
    return {
        "time": note["time"],
        "note": text.strip()[:500],
        "kind": "imported",
        "source": SOURCE,
        "legacy_kind": note.get("kind") if note.get("kind") in LEGACY_KINDS else "other",
        "room_id": mapping.get(zone) if isinstance(zone, str) else None,
    }


def classify(loaded, rooms, zone_names, mapping_override, imported_checksums):
    """Executor: pure classification of a decoded legacy payload."""
    result = {
        k: loaded.get(k)
        for k in ("status", "checksum", "size_bytes", "envelope_version", "payload_schema")
    }
    result["source"] = SOURCE
    result["path"] = LEGACY_FILE
    if loaded["status"] != "ready":
        return result
    payload = loaded["payload"]
    observations = payload.get("observations", [])
    zones = sorted(
        {
            z
            for p in observations
            if isinstance(p, dict) and isinstance(p.get("zones"), dict)
            for z in p["zones"]
        }
        | {n.get("zone") for n in payload.get("adjustments", []) if isinstance(n, dict)} - {None}
    )
    proposal = propose_mapping(zones, rooms, zone_names)
    room_ids = {r["id"] for r in rooms}
    for zone, room in (mapping_override or {}).items():
        if room is None:
            proposal[zone] = {"room_id": None, "status": "unmapped", "basis": "explicit"}
        elif room in room_ids:
            proposal[zone] = {"room_id": room, "status": "mapped", "basis": "explicit"}
        else:
            raise ServiceValidationError(f"mapping for {zone} must name a configured room id")
    mapping = {z: e["room_id"] for z, e in proposal.items() if e["room_id"] is not None}
    for zone, entry in proposal.items():
        spans = [
            p["time"]
            for p in observations
            if isinstance(p, dict) and finite(p.get("time")) and zone in (p.get("zones") or {})
        ]
        entry["observation_count"] = len(spans)
        entry["first_time"] = min(spans) if spans else None
        entry["last_time"] = max(spans) if spans else None
    converted, skipped = [], {"invalid": 0, "duplicate_time": 0}
    seen = set()
    for point in observations:
        item, reason = convert_point(point, mapping)
        if item is None:
            skipped[reason] += 1
        elif item["time"] in seen:
            skipped["duplicate_time"] += 1
        else:
            seen.add(item["time"])
            converted.append(item)
    converted.sort(key=lambda p: p["time"])
    truncated = len(converted) > MAX_POINTS
    converted = converted[-MAX_POINTS:]
    notes, skipped_notes = [], 0
    for note in payload.get("adjustments", []):
        item = convert_note(note, mapping)
        if item is None:
            skipped_notes += 1
        else:
            notes.append(item)
    sessions = payload.get("legacy_sessions", [])
    sessions = sessions if isinstance(sessions, list) else []
    unmapped_zone_points = sum(
        1
        for p in observations
        if isinstance(p, dict)
        and isinstance(p.get("zones"), dict)
        and any(z not in mapping for z in p["zones"])
    )
    result.update(
        {
            "zones": proposal,
            "mapping": mapping,
            "legacy_source_config": deepcopy(payload.get("source_config")),
            "counts": {
                "observations": len(observations),
                "importable_observations": len(converted),
                "truncated": truncated,
                "observations_with_unmapped_zone": unmapped_zone_points,
                "skipped_observations": skipped,
                "notes": len(payload.get("adjustments", [])),
                "importable_notes": len(notes),
                "skipped_notes": skipped_notes,
                "archived_sessions": min(len(sessions), MAX_ARCHIVED_SESSIONS),
                "archived_sessions_dropped": max(0, len(sessions) - MAX_ARCHIVED_SESSIONS),
            },
            "classification": {
                "observations": "compatible: per-sample zone/context readings with the same "
                "field semantics; copied with original timestamps into a private imported era",
                "legacy_sessions": "archive: pre-aggregated session metrics; retained for "
                "inspection, never analysed",
                "adjustments": "compatible: private notes imported with original time",
            },
            "span": {
                "first_time": converted[0]["time"] if converted else None,
                "last_time": converted[-1]["time"] if converted else None,
            },
        }
    )
    if loaded["checksum"] in imported_checksums:
        result["status"] = "already_imported"
    elif any(e["status"] == "needs_mapping" for e in proposal.values()):
        result["status"] = "needs_mapping"
    elif not converted and not notes:
        result["status"] = "nothing_to_import"
    result["_import"] = {
        "observations": converted,
        "notes": notes,
        "sessions": deepcopy(sessions[-MAX_ARCHIVED_SESSIONS:]),
        "truncated": truncated,
    }
    return result


def legacy_zones(payload):
    """Executor: every zone slug referenced by observations or notes."""
    zones = set()
    for point in payload.get("observations", []):
        if isinstance(point, dict) and isinstance(point.get("zones"), dict):
            zones.update(point["zones"])
    for note in payload.get("adjustments", []):
        if isinstance(note, dict) and isinstance(note.get("zone"), str):
            zones.add(note["zone"])
    return sorted(zones)


async def analyse(hass, coordinator, mapping_override=None):
    loaded = await hass.async_add_executor_job(read_legacy, legacy_path(hass))
    names = {}
    if loaded["status"] == "ready":
        for zone in await hass.async_add_executor_job(legacy_zones, loaded["payload"]):
            if (state := hass.states.get(zone)) is not None:
                names[zone] = state.name
    checksums = {
        e["signature"].get("checksum")
        for e in coordinator.store.imported_eras
        if isinstance(e.get("signature"), dict)
    }
    return await hass.async_add_executor_job(
        classify, loaded, coordinator.config["rooms"], names, mapping_override, checksums
    )


def public(result):
    return {k: v for k, v in result.items() if not k.startswith("_")}


async def preview(hass, coordinator, mapping=None):
    """Read-only: report status, checksum, zone spans, counts and a mapping proposal."""
    return public(await analyse(hass, coordinator, mapping))


def journal(heating, data):
    record = getattr(getattr(heating, "journal", None), "record", None)
    if record is None:
        return
    try:
        record("migration", scope="system", origin="service", data=data)
    except Exception:  # noqa: BLE001 - journal failures never affect the import
        LOGGER.debug("Journal unavailable for migration event", exc_info=True)


async def execute(hass, heating, coordinator, mapping=None, confirm=False):
    """Copy compatible history into a private imported era; live history is untouched."""
    if confirm is not True:
        raise ServiceValidationError("Set confirm: true after reviewing preview_history_import")
    store = coordinator.store
    if store.status == "storage_read_only":
        raise HomeAssistantError("History storage is read-only; nothing was imported")
    result = await analyse(hass, coordinator, mapping)
    if result["status"] == "already_imported":
        return {**public(result), "imported": False}
    if result["status"] != "ready":
        raise ServiceValidationError(f"Legacy history cannot be imported: {result['status']}")
    staged = result.pop("_import")
    room_for_notes = len(store.adjustments) + len(staged["notes"]) <= MAX_ADJUSTMENTS
    notes = staged["notes"] if room_for_notes else []
    era = {
        "signature": {
            "source": SOURCE,
            "checksum": result["checksum"],
            "imported_at": dt_util.utcnow().timestamp(),
            "mapping": result["mapping"],
            "envelope_version": result["envelope_version"],
            "payload_schema": result["payload_schema"],
            "semantics": LEGACY_SEMANTICS,
            "source_config": result["legacy_source_config"],
        },
        "observations": staged["observations"],
        "truncated": staged["truncated"],
        "archived_sessions": staged["sessions"],
        "note_count": len(notes),
    }
    previous = (store.imported_eras, store.adjustments)
    store.imported_eras = (store.imported_eras + [era])[-MAX_IMPORTED_ERAS:]
    store.adjustments = sorted(store.adjustments + notes, key=lambda n: n["time"])
    await store.save()
    if store.status != "ready":
        store.imported_eras, store.adjustments = previous
        journal(heating, {"action": "history_import", "status": "save_failed"})
        raise HomeAssistantError("Import could not be saved; history storage is unchanged")
    summary = {
        **public(result),
        "imported": True,
        "imported_observations": len(era["observations"]),
        "imported_notes": len(notes),
        "notes_skipped_journal_full": 0 if room_for_notes else len(staged["notes"]),
        "archived_sessions": len(era["archived_sessions"]),
        "retained_eras": len(store.imported_eras),
    }
    journal(
        heating,
        {
            "action": "history_import",
            "status": "imported",
            "source": SOURCE,
            "checksum": result["checksum"],
            "observations": summary["imported_observations"],
            "notes": summary["imported_notes"],
            "archived_sessions": summary["archived_sessions"],
        },
    )
    return summary


def rename_retired(path):
    """Executor: rename only; the content is never rewritten."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    target = f"{path}.retired-{stamp}"
    counter = 1
    while os.path.exists(target):
        target = f"{path}.retired-{stamp}-{counter}"
        counter += 1
    os.rename(path, target)
    return target


async def retire_legacy_store(hass, heating, coordinator, confirm=False):
    """Rename the legacy store only after a matching import has been saved."""
    if confirm is not True:
        raise ServiceValidationError("Set confirm: true to retire the legacy store")
    if any(e.state.value == "loaded" for e in hass.config_entries.async_entries(SOURCE)):
        raise ServiceValidationError("Remove or disable the Radiator Analytics integration first")
    loaded = await hass.async_add_executor_job(read_legacy, legacy_path(hass))
    if loaded["status"] == "missing":
        raise ServiceValidationError("No legacy store is present")
    checksum = loaded.get("checksum")
    matched = any(
        isinstance(e.get("signature"), dict) and e["signature"].get("checksum") == checksum
        for e in coordinator.store.imported_eras
    )
    if not matched or coordinator.store.status != "ready":
        raise ServiceValidationError(
            "Import the current legacy store successfully before retiring it"
        )
    target = await hass.async_add_executor_job(rename_retired, legacy_path(hass))
    relative = os.path.relpath(target, hass.config.config_dir)
    journal(
        heating,
        {"action": "retire_legacy_store", "source": SOURCE, "checksum": checksum, "path": relative},
    )
    return {"status": "retired", "checksum": checksum, "retired_path": relative}
