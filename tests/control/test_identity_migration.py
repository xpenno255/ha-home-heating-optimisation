"""Legacy output identities move only where meaning matches, reversibly and idempotently."""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.identity_migration import (
    execute,
    plan,
    rollback,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start

HHO_STATE = "sensor.home_heating_optimisation_control_study_state"
HHO_FLOW = "sensor.home_heating_optimisation_control_boiler_flow_setpoint"
HHO_DHW = "binary_sensor.home_heating_optimisation_control_boiler_dhw"

# (entry, platform, key, domain, unit, object_id)
OUTPUTS = [
    ("room", "state", "sensor", None, "living_state"),
    ("room", "air_setpoint", "sensor", "°C", "living_air_setpoint"),
    ("room", "operative_temp", "sensor", "°C", "living_operative_temperature"),
    ("room", "offset_final", "sensor", "°C", "living_offset"),
    ("room", "primary_climate", "sensor", None, "living_input"),
    ("boiler", "flow_setpoint", "sensor", "°C", "boiler_flow_setpoint"),
    ("boiler", "dhw_active", "binary_sensor", None, "boiler_dhw_active"),
    ("boiler", "last_write", "sensor", None, "boiler_last_write"),
]


def legacy_outputs(hass, c, disabled=True, versions=(2, 1)):
    registry = er.async_get(hass)
    disabled_by = ConfigEntryDisabler.USER if disabled else None
    room = MockConfigEntry(
        domain="ot_thermostat_control",
        title="Living",
        data=dict(c.rooms["study"]._config),
        version=versions[0],
        disabled_by=disabled_by,
    )
    boiler = MockConfigEntry(
        domain="boiler_flow_control",
        title="Boiler",
        data=dict(c.boiler._config),
        version=versions[1],
        disabled_by=disabled_by,
    )
    entries = {"room": room, "boiler": boiler}
    for e in entries.values():
        e.add_to_hass(hass)
    ids = {}
    for which, key, domain, unit, object_id in OUTPUTS:
        e = entries[which]
        reg = registry.async_get_or_create(
            domain,
            e.domain,
            f"{e.entry_id}_{key}",
            suggested_object_id=object_id,
            config_entry=e,
            unit_of_measurement=unit,
            original_name=key,
        )
        ids[key] = reg.entity_id
    return entries, ids


async def ready(hass, controlled):
    entry, c, calls = await start(hass, controlled)
    entries, ids = legacy_outputs(hass, c)
    await handover(c)
    return entry, c, calls, entries, ids


def by_legacy(result):
    return {m["legacy_entity_id"]: m for m in result["mappings"]}


async def test_plan_maps_compatible_and_archives_revised_or_skips_inputs(hass, controlled, sources):
    entry, c, _, entries, ids = await ready(hass, controlled)
    result = await plan(hass, entry)
    m = by_legacy(result)
    assert result["status"] == "ready" and result["blockers"] == []
    assert m["sensor.living_state"]["action"] == "transfer"
    assert m["sensor.living_state"]["hho_entity_id_current"] == HHO_STATE
    assert m["sensor.living_state"]["hho_unique_id"] == f"{entry.entry_id}:control:study:state"
    assert m["binary_sensor.boiler_dhw_active"]["hho_entity_id_current"] == HHO_DHW
    assert m["sensor.living_operative_temperature"]["action"] == "archive"
    assert m["sensor.living_offset"]["action"] == "archive"
    assert m["sensor.boiler_last_write"]["action"] == "archive"
    assert m["sensor.living_input"]["action"] == "skip"
    assert not m["sensor.living_offset"]["compatible"]
    assert result["counts"] == {"transfer": 4, "archive": 3, "skip": 1}
    assert all(m["has_statistics"] is None for m in result["mappings"])  # no recorder loaded
    # Nothing changed: preview is read-only.
    assert er.async_get(hass).async_get(HHO_STATE).unique_id.endswith(":control:study:state")
    assert all(er.async_get(hass).async_get(eid) is not None for eid in ids.values())


async def test_unit_mismatch_archives_and_user_disabled_or_renamed_legacy(
    hass, controlled, sources
):
    entry, c, _, entries, ids = await ready(hass, controlled)
    registry = er.async_get(hass)
    registry.async_update_entity(ids["flow_setpoint"], unit_of_measurement="K")
    registry.async_update_entity(ids["air_setpoint"], disabled_by=er.RegistryEntryDisabler.USER)
    registry.async_update_entity(ids["state"], new_entity_id="sensor.my_living_state")
    result = await plan(hass, entry)
    m = by_legacy(result)
    assert m["sensor.boiler_flow_setpoint"]["action"] == "archive"
    assert m["sensor.boiler_flow_setpoint"]["reason"] == "unit mismatch"
    assert m["sensor.living_air_setpoint"]["action"] == "skip"
    assert m["sensor.my_living_state"]["action"] == "transfer"
    await execute(hass, entry, result)
    await hass.async_block_till_done()
    assert registry.async_get("sensor.my_living_state").unique_id.endswith(":control:study:state")
    assert registry.async_get(HHO_FLOW) is not None


async def test_unsupported_version_is_reported_and_refused(hass, controlled, sources):
    entry, c, _ = await start(hass, controlled)
    entries, _ = legacy_outputs(hass, c, versions=(9, 1))
    await handover(c)
    result = await plan(hass, entry)
    assert result["status"] == "unsupported"
    assert result["unsupported_entries"] == [entries["room"].entry_id]
    assert by_legacy(result)["sensor.living_state"]["reason"] == "unsupported version"
    with pytest.raises(ServiceValidationError, match="Unsupported"):
        await execute(hass, entry)


async def test_refused_before_handover_while_enabled_or_active(hass, controlled, sources):
    entry, c, _ = await start(hass, controlled)
    entries, _ = legacy_outputs(hass, c, disabled=False)
    result = await plan(hass, entry)
    assert result["status"] == "blocked"
    assert any("handover" in b for b in result["blockers"])
    assert any("still enabled" in b for b in result["blockers"])
    with pytest.raises(ServiceValidationError):
        await execute(hass, entry)
    for e in entries.values():
        await hass.config_entries.async_set_disabled_by(e.entry_id, ConfigEntryDisabler.USER)
    await handover(c)
    await c.set_mode("boiler", "auto")
    result = await plan(hass, entry)
    assert result["status"] == "blocked" and any("shadow" in b for b in result["blockers"])
    with pytest.raises(ServiceValidationError, match="shadow"):
        await execute(hass, entry)
    with pytest.raises(ServiceValidationError, match="shadow"):
        await rollback(hass, entry)
    assert c.settings.get("ownership") == "ready"


async def test_consumer_inventory_lists_without_editing(hass, controlled, sources):
    entry, c, _, entries, ids = await ready(hass, controlled)
    raw = {
        "actions": [
            {"action": "notify.send", "data": {"message": "{{ states('sensor.living_state') }}"}}
        ]
    }
    hass.data["automation"] = SimpleNamespace(
        entities=[SimpleNamespace(entity_id="automation.report", is_on=True, raw_config=raw)]
    )
    dashboard = SimpleNamespace(
        async_load=AsyncMock(
            return_value={"views": [{"cards": [{"entity": "sensor.boiler_flow_setpoint"}]}]}
        )
    )
    broken = SimpleNamespace(async_load=AsyncMock(side_effect=RuntimeError("not found")))
    hass.data["lovelace"] = SimpleNamespace(dashboards={None: dashboard, "extra": broken})
    try:
        result = await plan(hass, entry)
    finally:
        hass.data.pop("automation")
        hass.data.pop("lovelace")
    m = by_legacy(result)
    assert m["sensor.living_state"]["consumers"] == [
        {"kind": "automation", "id": "automation.report"}
    ]
    assert m["sensor.boiler_flow_setpoint"]["consumers"] == [
        {"kind": "dashboard", "id": "lovelace"}
    ]
    assert m["sensor.living_operative_temperature"]["consumers"] == []
    assert raw["actions"][0]["data"]["message"] == "{{ states('sensor.living_state') }}"


async def test_execute_renames_persists_and_survives_reload(
    hass, controlled, sources, hass_storage
):
    entry, c, _, entries, ids = await ready(hass, controlled)
    registry = er.async_get(hass)
    result = await execute(hass, entry)
    await hass.async_block_till_done()
    assert result["status"] == "transferred" and result["reload_scheduled"]
    assert sorted(result["transferred"]) == [
        "binary_sensor.boiler_dhw_active",
        "sensor.boiler_flow_setpoint",
        "sensor.living_air_setpoint",
        "sensor.living_state",
    ]
    state = registry.async_get("sensor.living_state")
    assert state.platform == DOMAIN and state.unique_id == f"{entry.entry_id}:control:study:state"
    assert registry.async_get(HHO_STATE) is None
    assert (
        registry.async_get_entity_id(
            "sensor", "ot_thermostat_control", f"{entries['room'].entry_id}_state"
        )
        is None
    )
    assert hass.states.get("sensor.living_state") is not None
    assert hass.states.get("binary_sensor.boiler_dhw_active") is not None
    # Archived metrics keep their legacy registry entry and history.
    assert (
        registry.async_get("sensor.living_operative_temperature").platform
        == "ot_thermostat_control"
    )
    # The reloaded integration follows the transferred IDs and stays in shadow.
    heating = entry.runtime_data
    assert heating.config["boiler_decision_sensor"] == "sensor.boiler_flow_setpoint"
    assert heating.config["dhw_active"] == "binary_sensor.boiler_dhw_active"
    assert heating.controls.boiler.override == "shadow"
    assert heating.controls.settings.get("ownership") == "ready"
    stored = hass_storage[f"{DOMAIN}.{entry.entry_id}.identity_migration"]["data"]
    assert {i["stage"] for i in stored["items"].values()} == {"renamed"}
    again = await plan(hass, entry)
    assert again["status"] == "nothing_to_transfer"
    assert by_legacy(again)["sensor.living_state"]["reason"] == "already renamed"


async def test_interrupted_transfer_resumes_without_duplicates_and_detects_collision(
    hass, controlled, sources
):
    entry, c, _, entries, ids = await ready(hass, controlled)
    registry = er.async_get(hass)
    original = registry.async_update_entity
    calls = []

    def fail_once(entity_id, **kwargs):
        if "new_entity_id" in kwargs and not calls:
            calls.append(entity_id)
            raise RuntimeError("crash mid-rename")
        return original(entity_id, **kwargs)

    with patch.object(registry, "async_update_entity", side_effect=fail_once):
        with pytest.raises(RuntimeError):
            await execute(hass, entry)
    await hass.async_block_till_done()
    store = entry.runtime_data.identity_store
    stages = {i["legacy_entity_id"]: i["stage"] for i in store.items.values()}
    assert stages["binary_sensor.boiler_dhw_active"] == "legacy_removed"
    assert registry.async_get("binary_sensor.boiler_dhw_active") is None
    assert registry.async_get(HHO_DHW) is not None
    # Another entity now holds the freed ID: the resume must refuse, not overwrite.
    hass.states.async_set("binary_sensor.boiler_dhw_active", "on")
    result = await plan(hass, entry)
    assert result["status"] == "blocked"
    assert by_legacy(result)["binary_sensor.boiler_dhw_active"]["collision"]
    with pytest.raises(ServiceValidationError, match="held"):
        await execute(hass, entry, result)
    hass.states.async_remove("binary_sensor.boiler_dhw_active")
    result = await execute(hass, entry)
    await hass.async_block_till_done()
    assert sorted(result["transferred"])[0] == "binary_sensor.boiler_dhw_active"
    holders = [
        e
        for e in registry.entities.values()
        if e.unique_id == f"{entry.entry_id}:control:boiler:dhw"
    ]
    assert [e.entity_id for e in holders] == ["binary_sensor.boiler_dhw_active"]
    assert registry.async_get(HHO_DHW) is None


async def test_rollback_restores_legacy_entries_and_hho_ids(hass, controlled, sources):
    entry, c, _, entries, ids = await ready(hass, controlled)
    registry = er.async_get(hass)
    registry.async_update_entity(ids["state"], name="Living decision", area_id="lounge")
    await execute(hass, entry)
    await hass.async_block_till_done()
    result = await rollback(hass, entry)
    await hass.async_block_till_done()
    assert result["status"] == "identities_restored"
    assert registry.async_get(HHO_STATE).unique_id == f"{entry.entry_id}:control:study:state"
    assert hass.states.get(HHO_STATE) is not None
    legacy = registry.async_get("sensor.living_state")
    assert legacy.platform == "ot_thermostat_control"
    assert legacy.unique_id == f"{entries['room'].entry_id}_state"
    assert legacy.name == "Living decision" and legacy.area_id == "lounge"
    assert legacy.disabled_by == er.RegistryEntryDisabler.CONFIG_ENTRY
    assert entry.runtime_data.config["boiler_decision_sensor"] == HHO_FLOW
    again = await plan(hass, entry)
    assert again["status"] == "ready"
    assert by_legacy(again)["sensor.living_state"]["stage"] == "rolled_back"
    # A second migration after rollback starts a fresh transfer.
    await execute(hass, entry, again)
    await hass.async_block_till_done()
    assert registry.async_get("sensor.living_state").platform == DOMAIN


async def test_corrupt_journal_is_read_only_and_save_failure_leaves_ownership(
    hass, controlled, sources, hass_storage
):
    entry, c, _, entries, ids = await ready(hass, controlled)
    key = f"{DOMAIN}.{entry.entry_id}.identity_migration"
    hass_storage[key] = {"version": 1, "key": key, "data": {"schema": 1, "items": "bad"}}
    result = await plan(hass, entry)
    assert result["storage"] == "storage_read_only" and result["status"] == "blocked"
    assert by_legacy(result)["sensor.living_state"]["action"] == "transfer"
    with pytest.raises(ServiceValidationError, match="read-only"):
        await execute(hass, entry)
    entry.runtime_data.identity_store = None
    hass_storage.pop(key)
    store = await __import__(
        "custom_components.home_heating_optimisation.control.identity_migration",
        fromlist=["store_for"],
    ).store_for(entry.runtime_data)
    with patch.object(store.backend, "async_save", side_effect=OSError("disk full")):
        with pytest.raises(OSError):
            await execute(hass, entry)
    registry = er.async_get(hass)
    assert registry.async_get("sensor.living_state").platform == "ot_thermostat_control"
    assert registry.async_get(HHO_STATE) is not None
    assert c.settings.get("ownership") == "ready"
    await c.set_mode("boiler", "auto")
