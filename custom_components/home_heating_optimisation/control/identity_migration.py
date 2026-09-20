"""Preview and reversible transfer of compatible legacy output identities.

Only entity identities whose physical meaning, unit and statistics semantics are
unchanged are transferred. Estimated or revised metrics keep their new HHO IDs and
the legacy history stays under its original ID ("archive"). Actuator ownership is
never touched here; the handover transaction in ``migration.py`` owns that.
"""

import asyncio
import json
import logging
from copy import deepcopy

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..const import DOMAIN, effective_config
from .migration import configuration
from .runtime import BOILER_SENSORS, LEGACY, ROOM_SENSORS

LOGGER = logging.getLogger(__name__)
SCHEMA = 1
MAX_ITEMS = 500
SUPPORTED_VERSIONS = {"ot_thermostat_control": 2, "boiler_flow_control": 1}
NO_EQUIVALENT = "no consolidated equivalent; legacy history retained under its own ID"

# legacy key -> (hho key, action, reason). A missing key is a control input and is skipped.
OT_ROOM = {
    "state": ("state", "transfer", "same decision state"),
    "air_setpoint": ("air_setpoint", "transfer", "same corrected air target"),
    "would_write": ("would_write", "transfer", "same shadow write target"),
    "air_temp": ("air_temp", "transfer", "same selected room air measurement"),
    "schedule_setpoint": ("schedule_setpoint", "transfer", "same schedule setpoint"),
    "flow_temp_used": ("flow_temp_used", "transfer", "same flow temperature input"),
    "target_ot": (None, "archive", "estimated comfort target; definition revised"),
    "operative_temp": (None, "archive", "estimated operative temperature; not a measurement"),
    "offset_final": (None, "archive", "offset definition and unit changed"),
    "mrt_steady_state": (None, "archive", NO_EQUIVALENT),
    "offset_physical": (None, "archive", NO_EQUIVALENT),
    "radiator_output_w": (None, "archive", NO_EQUIVALENT),
    "outdoor_temp": (None, "archive", NO_EQUIVALENT),
    "solar_k": (None, "archive", NO_EQUIVALENT),
    "occupancy_status": (None, "archive", NO_EQUIVALENT),
    "last_write": (None, "archive", "legacy write memory is not adopted"),
    "last_run": (None, "archive", NO_EQUIVALENT),
}
OT_HUB = {
    "running_mean": (None, "archive", NO_EQUIVALENT),
    "flow_temp_used": (None, "archive", NO_EQUIVALENT),
    "outdoor_used": (None, "archive", NO_EQUIVALENT),
}
BFC = {
    "mode": ("mode", "transfer", "same boiler mode state"),
    "flow_setpoint": ("flow_setpoint", "transfer", "same flow setpoint decision"),
    "return_temperature_used": ("return_temperature_used", "transfer", "same return input"),
    "cycles_10min": ("cycles_10min", "transfer", "same burner start count"),
    "cycling_status": ("cycling_status", "transfer", "same cycling state"),
    "dhw_status": ("dhw_status", "transfer", "same DHW state"),
    "dhw_active": ("dhw", "transfer", "same DHW active signal"),
    "demand_filtered": (None, "archive", NO_EQUIVALENT),
    "last_burn_seconds": (None, "archive", NO_EQUIVALENT),
    "last_write": (None, "archive", "legacy write memory is not adopted"),
}
LEGACY_UNITS = {
    "air_setpoint": "°C",
    "would_write": "°C",
    "air_temp": "°C",
    "schedule_setpoint": "°C",
    "flow_temp_used": "°C",
    "flow_setpoint": "°C",
    "return_temperature_used": "°C",
}
GUIDANCE = (
    "Transferred IDs keep their history and statistics; dashboards and automations using "
    "them need no change.",
    "Archived IDs keep their legacy history untouched; update consumers to the new HHO IDs "
    "and remove the legacy entities only when that history is no longer needed.",
    "Consumers are listed, never edited. Review templates and YAML dashboards by hand.",
    "The integration reloads after a transfer so its own references follow the new IDs.",
)


class IdentityStore:
    """Journal of transferred identities; read-only on corruption, bounded in size."""

    def __init__(self, hass, entry_id):
        self.backend = Store(hass, SCHEMA, f"{DOMAIN}.{entry_id}.identity_migration")
        self.items = {}
        self.status = "not_loaded"

    @staticmethod
    def valid(data):
        if not isinstance(data, dict) or data.get("schema") != SCHEMA:
            return False
        items = data.get("items")
        if not isinstance(items, dict) or len(items) > MAX_ITEMS:
            return False
        for item in items.values():
            if not isinstance(item, dict) or not all(
                isinstance(item.get(k), str)
                for k in (
                    "legacy_entity_id",
                    "hho_entity_id_before",
                    "hho_unique_id",
                    "hho_domain",
                    "stage",
                )
            ):
                return False
            if not isinstance(item.get("snapshot"), dict):
                return False
        return True

    async def async_load(self):
        try:
            data = await self.backend.async_load()
        except Exception:
            LOGGER.exception("Identity migration journal unreadable")
            self.status = "storage_read_only"
            return
        if data is None:
            self.status = "ready"
        elif self.valid(data):
            self.items = deepcopy(data["items"])
            self.status = "ready"
        else:
            LOGGER.error("Identity migration journal invalid; refusing to write")
            self.status = "storage_read_only"

    async def async_save(self):
        if self.status == "storage_read_only":
            raise HomeAssistantError("Identity migration journal is read-only")
        if len(self.items) > MAX_ITEMS:
            raise HomeAssistantError("Identity migration journal is full")
        await self.backend.async_save({"schema": SCHEMA, "items": deepcopy(self.items)})


async def store_for(heating):
    store = getattr(heating, "identity_store", None)
    if store is None:
        store = IdentityStore(heating.hass, heating.config_entry.entry_id)
        await store.async_load()
        heating.identity_store = store
    return store


def journal(heating, **data):
    j = getattr(heating, "journal", None)
    if j is not None:
        j.record("migration", scope="system", origin="service", data=data)


def snapshot(entry):
    """Copy the registry fields needed to recreate a legacy entity."""
    return {
        "entity_id": entry.entity_id,
        "unique_id": entry.unique_id,
        "platform": entry.platform,
        "domain": entry.domain,
        "config_entry_id": entry.config_entry_id,
        "device_id": entry.device_id,
        "name": entry.name,
        "original_name": entry.original_name,
        "icon": entry.icon,
        "original_icon": entry.original_icon,
        "area_id": entry.area_id,
        "labels": sorted(entry.labels),
        "disabled_by": entry.disabled_by,
        "hidden_by": entry.hidden_by,
        "entity_category": entry.entity_category,
        "device_class": entry.device_class,
        "original_device_class": entry.original_device_class,
        "unit_of_measurement": entry.unit_of_measurement,
        "has_entity_name": entry.has_entity_name,
        "supported_features": entry.supported_features,
        "translation_key": entry.translation_key,
        "capabilities": dict(entry.capabilities) if entry.capabilities else None,
    }


def _mentions(value, needles):
    if isinstance(value, str):
        return any(n in value for n in needles)
    if isinstance(value, dict):
        return any(_mentions(v, needles) for v in value.values())
    if isinstance(value, list):
        return any(_mentions(v, needles) for v in value)
    return False


async def consumers(hass, entry, entity_ids):
    """List, never edit, loaded automations, scripts, dashboards and HHO's own configuration."""
    found = {eid: [] for eid in entity_ids}

    def note(eid, kind, ident):
        ref = {"kind": kind, "id": ident}
        if ref not in found[eid]:
            found[eid].append(ref)

    for domain in ("automation", "script"):
        component = hass.data.get(domain)
        for entity in getattr(component, "entities", []):
            raw = getattr(entity, "raw_config", None) or {}
            for eid in entity_ids:
                if _mentions(raw, [eid]):
                    note(eid, domain, entity.entity_id)
    lovelace = hass.data.get("lovelace")
    for url_path, dashboard in getattr(lovelace, "dashboards", {}).items():
        try:
            config = await dashboard.async_load(False)
        except Exception:  # missing, auto-generated or unreadable dashboards are not consumers
            continue
        for eid in entity_ids:
            if _mentions(config, [eid]):
                note(eid, "dashboard", url_path or "lovelace")
    own = json.dumps(effective_config(entry))
    for eid in entity_ids:
        if f'"{eid}"' in own:
            note(eid, DOMAIN, entry.entry_id)
    return found


async def statistics_present(hass, entity_ids):
    if "recorder" not in hass.config.components:
        return {}
    try:
        from homeassistant.components.recorder import get_instance
        from homeassistant.components.recorder.statistics import get_metadata

        metadata = await get_instance(hass).async_add_executor_job(
            lambda: get_metadata(hass, statistic_ids=set(entity_ids))
        )
    except Exception:
        LOGGER.warning("Could not read statistics metadata", exc_info=True)
        return {}
    return {eid: eid in metadata for eid in entity_ids}


def _room_scopes(controls):
    """Legacy room entries match HHO rooms through the controlled thermostat."""
    if not controls or not controls.config:
        return {}
    return {
        spec["config"].get("primary_climate"): scope
        for scope, spec in controls.config["rooms"].items()
    }


def _mapping_for(legacy_entry, reg, scopes):
    prefix = f"{legacy_entry.entry_id}_"
    uid = str(reg.unique_id)
    if not uid.startswith(prefix):
        return None, None, "skip", "unique ID is not a legacy output", None
    key = uid[len(prefix) :]
    if legacy_entry.domain == "boiler_flow_control":
        table, scope = BFC, "boiler"
    elif legacy_entry.data.get("entry_type") == "hub":
        if not key.startswith("hub_"):
            return None, None, "skip", "control input, not an output", None
        key = key[4:]
        table, scope = OT_HUB, None
    else:
        table = OT_ROOM
        scope = scopes.get(configuration(legacy_entry).get("primary_climate"))
        if key in table and scope is None:
            return key, None, "skip", "no matching consolidated room", None
    if key not in table:
        return key, scope, "skip", "control input, not an output", None
    hho_key, action, reason = table[key]
    return key, scope, action, reason, hho_key


def _hho_domain(hho_key):
    return "binary_sensor" if hho_key == "dhw" else "sensor"


async def plan(hass, entry):
    heating = entry.runtime_data
    controls = heating.controls
    registry = er.async_get(hass)
    store = await store_for(heating)
    scopes = _room_scopes(controls)
    mappings, blockers, unsupported = [], [], []
    for domain in LEGACY:
        for legacy_entry in hass.config_entries.async_entries(domain):
            supported = legacy_entry.version == SUPPORTED_VERSIONS[domain]
            if not supported:
                unsupported.append(legacy_entry.entry_id)
            if legacy_entry.disabled_by is None:
                blockers.append(f"legacy entry {legacy_entry.title} is still enabled")
            for reg in er.async_entries_for_config_entry(registry, legacy_entry.entry_id):
                key, scope, action, reason, hho_key = _mapping_for(legacy_entry, reg, scopes)
                item = {
                    "legacy_entry_id": legacy_entry.entry_id,
                    "legacy_platform": reg.platform,
                    "legacy_key": key,
                    "legacy_entity_id": reg.entity_id,
                    "legacy_unique_id": reg.unique_id,
                    "legacy_disabled_by": reg.disabled_by,
                    "scope": scope,
                    "hho_key": hho_key,
                    "hho_unique_id": None,
                    "hho_entity_id_current": None,
                    "action": action,
                    "compatible": action == "transfer",
                    "reason": reason,
                    "collision": False,
                    "stage": None,
                }
                if not supported:
                    item.update(action="skip", compatible=False, reason="unsupported version")
                if item["action"] == "transfer":
                    units = ROOM_SENSORS if scope != "boiler" else BOILER_SENSORS
                    hho_unit = units.get(hho_key)
                    legacy_unit = reg.unit_of_measurement or LEGACY_UNITS.get(key)
                    if hho_unit != legacy_unit:
                        item.update(action="archive", compatible=False, reason="unit mismatch")
                if (
                    item["action"] == "transfer"
                    and reg.disabled_by == er.RegistryEntryDisabler.USER
                ):
                    item.update(action="skip", reason="legacy entity disabled by user")
                if item["action"] == "transfer":
                    dom = _hho_domain(hho_key)
                    item["hho_unique_id"] = f"{entry.entry_id}:control:{scope}:{hho_key}"
                    item["hho_entity_id_current"] = registry.async_get_entity_id(
                        dom, DOMAIN, item["hho_unique_id"]
                    )
                    if item["hho_entity_id_current"] is None:
                        item.update(action="skip", reason="consolidated entity not registered")
                    elif item["hho_entity_id_current"] == reg.entity_id:
                        item.update(action="skip", reason="already transferred")
                    elif reg.domain != dom:
                        item.update(action="skip", compatible=False, reason="domain differs")
                mappings.append(item)
    # Journalled items whose legacy registry entry is already gone (interrupted or done).
    seen = {m["legacy_unique_id"] for m in mappings}
    for uid, record in store.items.items():
        if uid in seen:
            for m in mappings:
                if m["legacy_unique_id"] == uid:
                    m["stage"] = record["stage"]
            continue
        holder = registry.async_get(record["legacy_entity_id"])
        current = registry.async_get_entity_id(
            record["hho_domain"], DOMAIN, record["hho_unique_id"]
        )
        item = {
            **{k: record.get(k) for k in ("legacy_entry_id", "legacy_platform", "legacy_key")},
            "legacy_entity_id": record["legacy_entity_id"],
            "legacy_unique_id": uid,
            "legacy_disabled_by": None,
            "scope": record.get("scope"),
            "hho_key": record.get("hho_key"),
            "hho_unique_id": record["hho_unique_id"],
            "hho_entity_id_current": current,
            "action": "transfer",
            "compatible": True,
            "reason": "resume journalled transfer",
            "collision": False,
            "stage": record["stage"],
        }
        if record["stage"] in ("renamed", "rolled_back") or current == record["legacy_entity_id"]:
            item.update(action="skip", reason=f"already {record['stage']}")
        elif current is None:
            item.update(action="skip", reason="consolidated entity not registered")
        elif (holder is not None and holder.unique_id != record["hho_unique_id"]) or (
            holder is None
            and (state := hass.states.get(record["legacy_entity_id"])) is not None
            and not state.attributes.get("restored")
        ):
            item.update(collision=True)
            blockers.append(f"{record['legacy_entity_id']} is held by another entity")
        mappings.append(item)
    mappings.sort(key=lambda m: m["legacy_entity_id"])
    legacy_ids = [m["legacy_entity_id"] for m in mappings]
    stats = await statistics_present(hass, legacy_ids)
    refs = await consumers(hass, entry, legacy_ids)
    for m in mappings:
        m["has_statistics"] = stats.get(m["legacy_entity_id"])
        m["consumers"] = refs.get(m["legacy_entity_id"], [])
    ownership = controls.settings.get("ownership", "unclaimed") if controls else "unclaimed"
    if ownership != "ready":
        blockers.append("complete legacy handover first")
    if (
        controls
        and controls.boiler
        and (
            controls.boiler.override == "auto"
            or any(c.mode == "active" for c in controls.rooms.values())
        )
    ):
        blockers.append("return consolidated controls to shadow first")
    if store.status != "ready":
        blockers.append("identity migration journal is read-only")
    transfers = [m for m in mappings if m["action"] == "transfer"]
    if unsupported:
        status = "unsupported"
    elif blockers:
        status = "blocked"
    elif transfers:
        status = "ready"
    else:
        status = "nothing_to_transfer"
    return {
        "status": status,
        "storage": store.status,
        "ownership": ownership,
        "unsupported_entries": unsupported,
        "blockers": blockers,
        "counts": {
            "transfer": len(transfers),
            "archive": sum(m["action"] == "archive" for m in mappings),
            "skip": sum(m["action"] == "skip" for m in mappings),
        },
        "mappings": mappings,
        "statistics": "long-term statistics and history follow the entity ID; the legacy "
        "series continues under the transferred ID",
        "guidance": list(GUIDANCE),
    }


def _available(hass, registry, entity_id, allow_unique_id):
    """True when only the intended entity (or nothing live) holds this entity ID."""
    holder = registry.async_get(entity_id)
    if holder is not None:
        return holder.unique_id == allow_unique_id
    state = hass.states.get(entity_id)
    if state is not None and state.attributes.get("restored"):
        hass.states.async_remove(entity_id)
        return True
    return state is None


async def execute(hass, entry, plan_result=None):
    heating = entry.runtime_data
    controls = heating.controls
    result = plan_result or await plan(hass, entry)
    if result["status"] == "unsupported":
        raise ServiceValidationError("Unsupported legacy configuration version")
    if result["status"] != "ready":
        raise ServiceValidationError(
            "; ".join(result["blockers"]) or "Nothing eligible to transfer"
        )
    for domain in LEGACY:
        for legacy_entry in hass.config_entries.async_entries(domain):
            if (
                legacy_entry.disabled_by is None
                or legacy_entry.state is not ConfigEntryState.NOT_LOADED
            ):
                raise ServiceValidationError("Legacy controllers must be disabled and unloaded")
    store = await store_for(heating)
    registry = er.async_get(hass)
    done, renamed = [], False
    async with controls.lock:
        if controls.settings.get("ownership") != "ready":
            raise ServiceValidationError("complete legacy handover first")
        for m in result["mappings"]:
            if m["action"] != "transfer":
                continue
            uid = m["legacy_unique_id"]
            record = store.items.get(uid)
            legacy = registry.async_get(m["legacy_entity_id"])
            fresh = legacy is not None and legacy.unique_id == uid
            if record is None or (fresh and record["stage"] != "started"):
                if not fresh:
                    raise HomeAssistantError(f"{m['legacy_entity_id']} changed during migration")
                record = {
                    "legacy_entry_id": m["legacy_entry_id"],
                    "legacy_platform": m["legacy_platform"],
                    "legacy_key": m["legacy_key"],
                    "legacy_entity_id": m["legacy_entity_id"],
                    "scope": m["scope"],
                    "hho_key": m["hho_key"],
                    "hho_unique_id": m["hho_unique_id"],
                    "hho_domain": legacy.domain,
                    "hho_entity_id_before": m["hho_entity_id_current"],
                    "snapshot": snapshot(legacy),
                    "stage": "started",
                    "started_at": dt_util.utcnow().isoformat(),
                }
                store.items[uid] = record
                # Journal intent before any registry change so a crash is recoverable.
                await store.async_save()
                journal(heating, step="start", **_public(record))
            if record["stage"] == "started":
                if legacy is not None and legacy.unique_id == uid:
                    registry.async_remove(legacy.entity_id)
                record["stage"] = "legacy_removed"
                await store.async_save()
            if record["stage"] == "legacy_removed":
                current = registry.async_get_entity_id(
                    record["hho_domain"], DOMAIN, record["hho_unique_id"]
                )
                target = record["legacy_entity_id"]
                if current is None:
                    raise HomeAssistantError(f"Consolidated entity for {target} is missing")
                if current != target:
                    if not _available(hass, registry, target, record["hho_unique_id"]):
                        raise HomeAssistantError(f"{target} is held by another entity")
                    registry.async_update_entity(current, new_entity_id=target)
                    renamed = True
                record["stage"] = "renamed"
                record["renamed_at"] = dt_util.utcnow().isoformat()
                await store.async_save()
                journal(heating, step="renamed", **_public(record))
            done.append(record["legacy_entity_id"])
    if renamed:
        # Own configuration references follow the new IDs on reload; controls stay in shadow.
        await asyncio.sleep(0)
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return {"status": "transferred", "transferred": done, "reload_scheduled": renamed}


def _public(record):
    return {
        k: record.get(k)
        for k in ("legacy_entity_id", "hho_unique_id", "hho_entity_id_before", "scope", "hho_key")
    }


async def rollback(hass, entry):
    """Rename HHO entities back and recreate the journalled legacy registry entries."""
    heating = entry.runtime_data
    controls = heating.controls
    if controls.boiler and (
        controls.boiler.override == "auto"
        or any(c.mode == "active" for c in controls.rooms.values())
    ):
        raise ServiceValidationError("return consolidated controls to shadow first")
    store = await store_for(heating)
    if store.status != "ready":
        raise HomeAssistantError("Identity migration journal is read-only")
    registry = er.async_get(hass)
    restored, renamed = [], False
    async with controls.lock:
        for uid, record in list(store.items.items()):
            if record["stage"] == "rolled_back":
                continue
            snap = record["snapshot"]
            current = registry.async_get_entity_id(
                record["hho_domain"], DOMAIN, record["hho_unique_id"]
            )
            if current == record["legacy_entity_id"]:
                before = record["hho_entity_id_before"]
                if not _available(hass, registry, before, record["hho_unique_id"]):
                    raise HomeAssistantError(f"{before} is held by another entity")
                registry.async_update_entity(current, new_entity_id=before)
                renamed = True
            legacy_entry = hass.config_entries.async_get_entry(record["legacy_entry_id"])
            if registry.async_get_entity_id(snap["domain"], snap["platform"], uid) is None:
                if legacy_entry is None:
                    raise HomeAssistantError("Legacy entry was removed; cannot restore its entity")
                created = registry.async_get_or_create(
                    snap["domain"],
                    snap["platform"],
                    uid,
                    suggested_object_id=snap["entity_id"].split(".", 1)[1],
                    config_entry=legacy_entry,
                    device_id=snap.get("device_id"),
                    original_name=snap.get("original_name"),
                    original_icon=snap.get("original_icon"),
                    original_device_class=snap.get("original_device_class"),
                    unit_of_measurement=snap.get("unit_of_measurement"),
                    entity_category=snap.get("entity_category"),
                    has_entity_name=bool(snap.get("has_entity_name")),
                    supported_features=snap.get("supported_features") or 0,
                    translation_key=snap.get("translation_key"),
                    capabilities=snap.get("capabilities"),
                )
                if created.entity_id != snap["entity_id"] and _available(
                    hass, registry, snap["entity_id"], uid
                ):
                    created = registry.async_update_entity(
                        created.entity_id, new_entity_id=snap["entity_id"]
                    )
                registry.async_update_entity(
                    created.entity_id,
                    name=snap.get("name"),
                    icon=snap.get("icon"),
                    area_id=snap.get("area_id"),
                    labels=set(snap.get("labels") or []),
                )
            record["stage"] = "rolled_back"
            record["rolled_back_at"] = dt_util.utcnow().isoformat()
            await store.async_save()
            journal(heating, step="rolled_back", **_public(record))
            restored.append(record["legacy_entity_id"])
    if renamed:
        await asyncio.sleep(0)
        hass.config_entries.async_schedule_reload(entry.entry_id)
    return {"status": "identities_restored", "restored": restored, "reload_scheduled": renamed}
