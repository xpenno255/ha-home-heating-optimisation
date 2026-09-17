"""Explicit import and reversible handover, without importing actuator ownership."""

import hashlib
import json
import re
import shutil
from copy import deepcopy
from pathlib import Path

from homeassistant.config_entries import ConfigEntryDisabler, ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er

from ..const import effective_config
from .runtime import LEGACY

ROOM_SEED = ("trust_k", "cap_up", "cap_down", "ramses_schedule", "ramses_schedule_saved_at")
BOILER_SEED = ("design_flow", "design_outdoor", "return_ceiling", "dhw_delta", "enabled")


def configuration(entry):
    if entry.domain == "boiler_flow_control":
        return deepcopy(dict(entry.options or entry.data))
    return deepcopy({**entry.data, **entry.options})


def seed(runtime, keys):
    store = getattr(runtime, "_store", None)
    return {key: deepcopy(store.get(key)) for key in keys if store and store.get(key) is not None}


def mqtt_bindings(hass, entity_ids):
    """Discover only simple EMS-ESP field templates; arbitrary templates are not evaluated."""
    from homeassistant.components.mqtt.debug_info import info_for_device

    registry = er.async_get(hass)
    bindings = []
    devices = {
        e.device_id
        for eid in entity_ids
        if (e := registry.async_get(eid)) and e.platform == "mqtt" and e.device_id
    }
    for device in devices:
        for entity in info_for_device(hass, device).get("entities", []):
            if entity["entity_id"] not in entity_ids:
                continue
            cfg = entity.get("discovery_data", {}).get("payload", {})
            if cfg.get("origin", {}).get("name") != "EMS-ESP":
                continue
            match = re.fullmatch(
                r"\{\{value_json\['([a-zA-Z0-9_]+)'\] if value_json\['\1'\] is defined else (?:0|false|'off')\}\}",
                cfg.get("value_template", ""),
            )
            if not match:
                continue
            topic = cfg.get("state_topic")
            if not isinstance(topic, str) or "+" in topic or "#" in topic:
                continue
            bindings.append(
                {
                    "entity_id": entity["entity_id"],
                    "topic": topic,
                    "field": match[1],
                    "kind": "binary"
                    if entity["entity_id"].startswith("binary_sensor.")
                    else "number",
                }
            )
    return sorted(bindings, key=lambda b: b["entity_id"])


def preview(hass, entry):
    old = effective_config(entry)
    if old.get("control"):
        return {"already_imported": True, "control": old["control"]}
    rooms = hass.config_entries.async_entries("ot_thermostat_control")
    hubs = [e for e in rooms if e.data.get("entry_type") == "hub"]
    boilers = hass.config_entries.async_entries("boiler_flow_control")
    if len(hubs) != 1 or len(boilers) != 1:
        raise ServiceValidationError("Exactly one OT hub and one boiler controller are required")
    selected = [hubs[0], boilers[0]]
    for legacy in selected:
        if legacy.version != (2 if legacy.domain == "ot_thermostat_control" else 1):
            raise ServiceValidationError("Unsupported legacy configuration version")
        if legacy.state is not ConfigEntryState.LOADED:
            raise ServiceValidationError("Load the legacy hub and boiler controller first")
    control = {
        "schema": 1,
        "hub": configuration(hubs[0]),
        "rooms": {},
        "boiler": {
            "config": configuration(boilers[0]),
            "seed": seed(boilers[0].runtime_data, BOILER_SEED),
        },
        "global_enabled": hubs[0].runtime_data.global_enabled,
    }
    for room in old["rooms"]:
        matches = [e for e in rooms if configuration(e).get("primary_climate") == room["climate"]]
        if len(matches) != 1:
            raise ServiceValidationError(
                f"Room {room['name']} has no unique legacy thermostat match"
            )
        legacy = matches[0]
        c = legacy.runtime_data
        if legacy.state is not ConfigEntryState.LOADED or c.geometry is None:
            raise ServiceValidationError("Load all legacy room controllers and their surveys first")
        if legacy.version != 2:
            raise ServiceValidationError("Unsupported legacy room configuration version")
        cfg = configuration(legacy)
        if cfg.get("room_file"):
            raise ServiceValidationError(
                "Custom room_file overrides require an explicit survey migration"
            )
        cfg["mode"] = "shadow"
        # Capture the actual selected sensor, rather than reviving a retired config field.
        cfg["air_temp_sensor"] = (
            c.data.air_temp_source if c.data.air_temp_source.startswith("sensor.") else None
        )
        control["rooms"][room["id"]] = {
            "config": cfg,
            "seed": seed(c, ROOM_SEED),
            "enabled": c.enabled,
            "occupancy_enabled": c.occupancy_enabled,
            "air_source": c.data.air_temp_source,
        }
        selected.append(legacy)
    if {e.entry_id for e in rooms} != {
        e.entry_id for e in selected if e.domain == "ot_thermostat_control"
    }:
        raise ServiceValidationError("Import must include every legacy controlled room")
    control["legacy_entries"] = [e.entry_id for e in selected]
    control["legacy_versions"] = {
        e.entry_id: {"version": e.version, "minor_version": e.minor_version} for e in selected
    }
    control["source_hash"] = hashlib.sha256(
        json.dumps([configuration(e) for e in selected], sort_keys=True).encode()
    ).hexdigest()
    control["boiler"]["seed"].update(boilers[0].runtime_data._tunables)
    for room in control["rooms"].values():
        room["seed"].update(
            next(
                e.runtime_data._tunables
                for e in rooms
                if configuration(e).get("primary_climate") == room["config"]["primary_climate"]
            )
        )
    bcfg = control["boiler"]["config"]
    bindings = mqtt_bindings(
        hass,
        {
            bcfg[k]
            for k in ("heating_active_entity", "burner_power_entity", "current_flow_entity")
            if bcfg.get(k)
        },
    )
    return {
        "already_imported": False,
        "control": control,
        "mqtt_sources": bindings,
        "room_count": len(control["rooms"]),
        "mode": "shadow",
        "legacy_entries_remain_enabled": True,
    }


def copy_survey(hass, old):
    base = Path(hass.config.path()).resolve()
    raw = old.get("survey_directory")
    if not raw:
        raise ServiceValidationError("Configure the existing survey directory first")
    source = (base / raw).resolve()
    if not source.is_relative_to(base):
        raise ServiceValidationError("Survey must be inside the HA configuration directory")
    target = base / "home_heating_optimisation" / "house"
    files = [source / "house.yaml", *sorted((source / "rooms").glob("*.yaml"))]
    if not files[0].is_file() or len(files) < 2:
        raise ServiceValidationError("Survey is incomplete")
    for f in files:
        if not f.resolve().is_relative_to(source):
            raise ServiceValidationError("Survey contains a symlink outside its directory")
    if target.exists():
        if any(
            not (target / f.relative_to(source)).is_file()
            or (target / f.relative_to(source)).read_bytes() != f.read_bytes()
            for f in files
        ):
            raise ServiceValidationError("Destination survey differs; originals were preserved")
    else:
        staging = target.with_name("house.importing")
        staging.mkdir(parents=True, exist_ok=True)
        for f in files:
            dest = staging / f.relative_to(source)
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(f, dest)
        staging.rename(target)
    return str(target.relative_to(base))


async def import_controls(hass, entry):
    result = preview(hass, entry)
    if result["already_imported"]:
        return {"status": "already_imported"}
    old = deepcopy(effective_config(entry))
    directory = await hass.async_add_executor_job(copy_survey, hass, old)
    control = result["control"]
    control["hub"]["house_dir"] = directory
    # The old observer config is an explicit rollback snapshot, separate from actuator memory.
    control["observer_before"] = old
    options = {
        **old,
        "survey_directory": directory,
        "control": control,
        "mqtt_sources": result["mqtt_sources"],
    }
    hass.config_entries.async_update_entry(entry, options=options)
    return {
        "status": "imported_shadow",
        "room_count": result["room_count"],
        "mqtt_sources": result["mqtt_sources"],
        "survey_directory": directory,
    }


async def handover(controls):
    """Stop legacy entries; persist ownership last. Never turn active on automatically."""
    async with controls.lock:
        if not controls.config or not controls.ready:
            raise ServiceValidationError("Import and load consolidated controls first")
        if (
            any(c.mode == "active" for c in controls.rooms.values())
            or controls.boiler.override == "auto"
        ):
            raise ServiceValidationError("Return consolidated controls to shadow before handover")
        legacy_entries = [
            controls.hass.config_entries.async_get_entry(ident)
            for ident in controls.config["legacy_entries"]
        ]
        if any(e is None for e in legacy_entries):
            raise ServiceValidationError("A legacy entry was removed; review ownership")
        expected = controls.config.get("source_hash")
        current = hashlib.sha256(
            json.dumps([configuration(e) for e in legacy_entries], sort_keys=True).encode()
        ).hexdigest()
        if expected and current != expected:
            raise ServiceValidationError("Legacy configuration changed after import")
        for e in legacy_entries:
            if e.state is ConfigEntryState.LOADED:
                c = e.runtime_data
                if getattr(c, "mode", None) == "active" or getattr(c, "override", None) == "auto":
                    raise ServiceValidationError("Put legacy controllers in shadow before handover")
        controls.settings.set("ownership", "transition")
        await controls.settings.async_save()
        disabled = list(controls.settings.get("disabled_legacy", []))
        try:
            for ident in controls.config["legacy_entries"]:
                entry = controls.hass.config_entries.async_get_entry(ident)
                if entry is None or entry.domain not in LEGACY:
                    raise ServiceValidationError("Legacy configuration changed; review ownership")
                if entry.disabled_by is None:
                    # Journal intent first: rollback also recovers a crash during unload.
                    if ident not in disabled:
                        disabled.append(ident)
                    controls.settings.set("disabled_legacy", disabled)
                    await controls.settings.async_save()
                    await controls.hass.config_entries.async_set_disabled_by(
                        ident, ConfigEntryDisabler.USER
                    )
                if entry.state is not ConfigEntryState.NOT_LOADED:
                    raise ServiceValidationError("A legacy controller failed to unload")
            if controls.conflicts():
                raise ServiceValidationError("Another legacy controller is still enabled")
            controls.settings.set("ownership", "ready")
            controls.settings.set("modes", {})
            controls.settings.set("disabled_legacy", disabled)
            await controls.settings.async_save()
        except Exception:
            controls.settings.set("ownership", "interrupted")
            await controls.settings.async_save()
            raise
        return {"status": "handover_complete_shadow", "disabled_entries": disabled}


async def rollback(controls):
    async with controls.lock:
        controls.ready = False
        controls.settings.set("ownership", "rollback")
        controls.settings.set("modes", {})
        await controls.settings.async_save()
        controls.boiler.override = "shadow"
        for c in controls.rooms.values():
            c.mode = "shadow"
        await controls.refresh()
        for ident in controls.settings.get("disabled_legacy", []):
            entry = controls.hass.config_entries.async_get_entry(ident)
            if entry and entry.disabled_by is not None:
                await controls.hass.config_entries.async_set_disabled_by(ident, None)
        controls.settings.set("ownership", "unclaimed")
        await controls.settings.async_save()
        controls.ready = True
        return {"status": "legacy_restored_consolidated_shadow"}
