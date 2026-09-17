"""Registry identity changes preserve an independently owned loaded controller."""

from homeassistant.helpers import entity_registry as er

from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start


async def test_loaded_control_rename_reloads_sources_without_actuator_writes(
    hass, controlled, sources
):
    registry = er.async_get(hass)
    registry.async_get_or_create("sensor", "test_platform", "air", suggested_object_id="air")
    entry, controls, calls = await start(hass, controlled)
    await handover(controls)
    before_ids = {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    }
    registry.async_update_entity("sensor.air", new_entity_id="sensor.renamed_air")
    hass.states.async_set("sensor.renamed_air", 18, {"unit_of_measurement": "°C"})
    hass.states.async_remove("sensor.air")
    await hass.async_block_till_done()
    restored = entry.runtime_data.controls
    # The registry first marks the synthetic entity unavailable; simulate its next report.
    hass.states.async_set("sensor.renamed_air", 18, {"unit_of_measurement": "°C"})
    await restored.refresh()
    assert restored is not controls
    assert restored.settings.get("ownership") == "ready"
    assert restored.rooms["study"].data.air_temp_source == "sensor.renamed_air"
    assert (
        entry.data["control"]["rooms"]["study"]["config"]["air_temp_sensor"] == "sensor.renamed_air"
    )
    assert entry.data["rooms"][0]["air_sensor"] is None  # intentional observer thermostat source
    assert restored.rooms["study"].mode == "shadow" and restored.boiler.override == "shadow"
    assert restored.rooms["study"].data.confirmed_target is None
    assert {
        e.unique_id: e.entity_id
        for e in er.async_entries_for_config_entry(registry, entry.entry_id)
    } == before_ids
    assert calls == []


async def test_observation_only_room_is_not_rewired_to_absent_controller(hass, controlled, sources):
    controlled["rooms"].append(
        {
            "id": "observed",
            "name": "Observed only",
            "climate": "climate.observed",
            "decision_sensor": "sensor.external_decision",
        }
    )
    entry, controls, calls = await start(hass, controlled)
    room = next(r for r in entry.runtime_data.config["rooms"] if r["id"] == "observed")
    assert room["decision_sensor"] == "sensor.external_decision"
    assert "comfort_target_sensor" not in room
    assert "observed" not in controls.rooms
    assert ("observed", "state") not in controls.entities
    assert calls == []
