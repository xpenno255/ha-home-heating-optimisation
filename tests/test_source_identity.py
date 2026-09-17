"""Entity registry rename continuity stays within declared source fields."""

from copy import deepcopy

from homeassistant.config_entries import ConfigEntryDisabler
from homeassistant.core import Event
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.source_identity import (
    async_handle_entity_registry_updated,
    async_register_source_identity,
    update_source_references,
)


def config_with_sources():
    old = "sensor.old_input"
    return {
        "outdoor_temperature": old,
        "boiler_decision_sensor": old,
        "rooms": [
            {
                "id": "stable-room-id",
                "name": "Old input is harmless text",
                "climate": "climate.old_zone",
                "air_sensor": old,
                "demand_sensor": old,
                "comfort_target_sensor": old,
            }
        ],
        "description": f"Keep {old} in free text",
        "template": "{{ states('sensor.old_input') }}",
        "control": {
            "hub": {"outdoor_temp_sensor": old, "notes": old},
            "rooms": {
                "stable-room-id": {
                    "air_source": old,
                    "config": {
                        "primary_climate": "climate.old_zone",
                        "backup_climate": "climate.old_backup",
                        "air_temp_sensor": old,
                        "window_sensors": ["binary_sensor.window", old],
                        "name": old,
                    },
                    "seed": {"history": old},
                }
            },
            "boiler": {
                "config": {
                    "flow_setpoint_entity": "number.flow",
                    "outdoor_temp_entity": old,
                    "zone_demand_entities": [old, "sensor.other"],
                    "notes": old,
                }
            },
            "observer_before": {
                "outdoor_temperature": old,
                "rooms": [{"id": "stable-room-id", "air_sensor": old}],
            },
        },
        "mqtt_sources": [{"entity_id": old, "topic": "ems/state", "field": "flow"}],
    }


def test_update_source_references_is_exact_and_preserves_identity():
    config = config_with_sources()
    updated = update_source_references(config, "sensor.old_input", "sensor.new_input")

    assert updated["outdoor_temperature"] == "sensor.new_input"
    assert updated["boiler_decision_sensor"] == "sensor.new_input"
    room = updated["rooms"][0]
    assert room["id"] == "stable-room-id"
    assert room["air_sensor"] == "sensor.new_input"
    assert room["demand_sensor"] == "sensor.new_input"
    assert room["comfort_target_sensor"] == "sensor.new_input"
    assert room["climate"] == "climate.old_zone"

    control = updated["control"]
    assert control["hub"]["outdoor_temp_sensor"] == "sensor.new_input"
    control_room = control["rooms"]["stable-room-id"]
    assert control_room["air_source"] == "sensor.new_input"
    assert control_room["config"]["air_temp_sensor"] == "sensor.new_input"
    assert control_room["config"]["window_sensors"] == ["binary_sensor.window", "sensor.new_input"]
    assert control["boiler"]["config"]["outdoor_temp_entity"] == "sensor.new_input"
    assert control["boiler"]["config"]["zone_demand_entities"] == [
        "sensor.new_input",
        "sensor.other",
    ]
    assert updated["mqtt_sources"][0]["entity_id"] == "sensor.new_input"

    # Text, templates, persistence seeds, and the rollback copy are untouched.
    assert updated["description"] == config["description"]
    assert updated["template"] == config["template"]
    assert updated["control"]["hub"]["notes"] == "sensor.old_input"
    assert updated["control"]["rooms"]["stable-room-id"]["config"]["name"] == "sensor.old_input"
    assert updated["control"]["rooms"]["stable-room-id"]["seed"] == {"history": "sensor.old_input"}
    assert updated["control"]["observer_before"] == config["control"]["observer_before"]
    assert config["control"]["observer_before"]["outdoor_temperature"] == "sensor.old_input"


async def test_registry_rename_updates_disabled_data_and_options(hass):
    old = "sensor.old_input"
    new = "sensor.new_input"
    data = config_with_sources()
    options = deepcopy(data)
    entry = MockConfigEntry(
        domain=DOMAIN,
        data=data,
        options=options,
        disabled_by=ConfigEntryDisabler.USER,
    )
    entry.add_to_hass(hass)
    unsubscribe = async_register_source_identity(hass)

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", "test_platform", "old_input", suggested_object_id="old_input"
    )
    registry.async_update_entity(old, new_entity_id=new)
    await hass.async_block_till_done()

    assert entry.disabled_by is ConfigEntryDisabler.USER
    assert entry.data["outdoor_temperature"] == new
    assert entry.options["outdoor_temperature"] == new
    assert entry.data["control"]["observer_before"]["outdoor_temperature"] == old
    assert entry.options["control"]["observer_before"]["outdoor_temperature"] == old
    unsubscribe()


async def test_unrelated_rename_and_deletion_are_ignored(hass):
    config = config_with_sources()
    entry = MockConfigEntry(domain=DOMAIN, data=config)
    entry.add_to_hass(hass)
    original = deepcopy(dict(entry.data))

    unrelated = Event(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        {
            "action": "update",
            "old_entity_id": "sensor.unrelated",
            "entity_id": "sensor.renamed",
        },
    )
    removed = Event(
        er.EVENT_ENTITY_REGISTRY_UPDATED,
        {"action": "remove", "entity_id": "sensor.old_input"},
    )
    assert await async_handle_entity_registry_updated(hass, unrelated) == 0
    assert await async_handle_entity_registry_updated(hass, removed) == 0
    assert dict(entry.data) == original
