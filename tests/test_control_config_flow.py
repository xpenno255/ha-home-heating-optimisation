"""Control options are standalone, explicit and safe while shadowed."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace

from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.configuration import (
    ControlConfigError,
    editable_control,
    validate_control_rooms,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start
from tests.test_integration import setup
from tests.test_survey import survey_files as survey_files


class Settings:
    """In-memory settings store used to inspect lifecycle writes."""

    def __init__(self, **values):
        self.ready = True
        self.values = values
        self.saved = 0

    def get(self, key, default=None):
        return self.values.get(key, default)

    def set(self, key, value):
        self.values[key] = value

    async def async_save(self):
        self.saved += 1


def entry(hass, config):
    item = MockConfigEntry(domain=DOMAIN, title="Heating", unique_id=DOMAIN, data=config)
    item.add_to_hass(hass)
    return item


async def choose_control(hass, item):
    flow = await hass.config_entries.options.async_init(item.entry_id)
    return await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "control"}
    )


async def test_standalone_setup_builds_runtime_schema_in_shadow(hass, config, survey_files):
    hass.config.config_dir = str(survey_files.parent)
    config = deepcopy(config)
    config["survey_directory"] = "house"
    item = entry(hass, config)
    flow = await choose_control(hass, item)
    assert flow["step_id"] == "control_hub"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "house_dir": "../outside",
            "outdoor_temp_sensor": "sensor.control_outdoor",
            "flow_temp_entity": "sensor.control_flow",
        },
    )
    assert flow["errors"] == {"base": "control_invalid_survey"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "house_dir": "house",
            "outdoor_temp_sensor": "sensor.control_outdoor",
            "flow_temp_entity": "sensor.control_flow",
        },
    )
    assert flow["step_id"] == "control_boiler"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "flow_setpoint_entity": "number.boiler_flow_target",
            "outdoor_temp_entity": "sensor.control_outdoor",
            "flow_min": 32,
            "flow_max": 58,
            "dhw_flow_min": 52,
            "dhw_flow_max": 68,
            "dhw_return_ceiling": 57,
            "manual_hold_minutes": 45,
        },
    )
    assert flow["step_id"] == "control_rooms"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "primary_climate": "climate.study",
            "room_id": "study",
            "air_temp_sensor": "sensor.control_air",
            "zone_setpoint_min": 9,
            "zone_setpoint_max": 23,
            "manual_hold_minutes": 90,
        },
    )
    assert flow["type"] == "create_entry"
    control = flow["data"]["control"]
    assert control["origin"] == "standalone"
    assert control["legacy_entries"] == []
    assert control["boiler"]["config"]["flow_min"] == 32
    assert control["boiler"]["config"]["manual_hold_minutes"] == 45
    assert control["rooms"]["study"]["config"]["air_temp_sensor"] == "sensor.control_air"
    assert control["rooms"]["study"]["config"]["mode"] == "shadow"
    assert control["rooms"]["study"]["config"]["zone_setpoint_min"] == 9
    assert control["rooms"]["study"]["config"]["zone_setpoint_max"] == 23
    assert control["rooms"]["study"]["config"]["override_duration"] == 60
    assert "asymmetry_enabled" not in control["rooms"]["study"]["config"]
    assert control["rooms"]["study"]["seed"]["cap_down"] == 1.5


async def test_invalid_boiler_limit_relationship_is_rejected(hass, config, survey_files):
    hass.config.config_dir = str(survey_files.parent)
    config = deepcopy(config)
    config["survey_directory"] = "house"
    item = entry(hass, config)
    flow = await choose_control(hass, item)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "house_dir": "house",
            "outdoor_temp_sensor": "sensor.outdoor",
            "flow_temp_entity": "sensor.flow",
        },
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "flow_setpoint_entity": "number.target",
            "outdoor_temp_entity": "sensor.outdoor",
            "flow_min": 70,
            "flow_max": 40,
        },
    )
    assert flow["step_id"] == "control_boiler"
    assert flow["errors"] == {"base": "control_invalid_limits"}


async def test_import_edit_retains_provenance_and_revokes_changed_actuator(
    hass, config, survey_files
):
    hass.config.config_dir = str(survey_files.parent)
    config = deepcopy(config)
    control = editable_control(config)
    control.update(
        {
            "origin": "imported",
            "source_hash": "legacy-hash",
            "legacy_entries": ["legacy-room"],
            "observer_before": {"rooms": deepcopy(config["rooms"])},
        }
    )
    control["hub"].update(
        house_dir="house",
        outdoor_temp_sensor="sensor.outdoor",
        flow_temp_entity="sensor.flow",
    )
    control["boiler"]["config"].update(
        flow_setpoint_entity="number.old_target", outdoor_temp_entity="sensor.outdoor"
    )
    control["rooms"]["study"]["config"]["air_temp_sensor"] = "sensor.control_air"
    control["rooms"]["study"]["seed"].update(trust_k=0.73, cap_up=1.1)
    config["control"] = control
    item = entry(hass, config)
    settings = Settings(ownership="ready", modes={"study": "shadow", "boiler": "shadow"})
    item.runtime_data = SimpleNamespace(
        controls=SimpleNamespace(
            hub=None,
            boiler=SimpleNamespace(override="shadow", enabled=True),
            rooms={"study": SimpleNamespace(mode="shadow", enabled=True, occupancy_enabled=True)},
            settings=settings,
            lock=asyncio.Lock(),
        )
    )
    flow = await choose_control(hass, item)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "control_rooms"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "primary_climate": "climate.new_study",
            "room_id": "study",
            "air_temp_sensor": "sensor.new_control_air",
        },
    )
    assert flow["type"] == "create_entry"
    edited = flow["data"]["control"]
    assert edited["source_hash"] == "legacy-hash"
    assert edited["observer_before"] == control["observer_before"]
    assert edited["rooms"]["study"]["seed"]["trust_k"] == 0.73
    assert edited["rooms"]["study"]["seed"]["cap_up"] == 1.1
    assert edited["rooms"]["study"]["config"]["air_temp_sensor"] == "sensor.new_control_air"
    assert settings.values["modes"] == {}
    assert settings.values["ownership"] == "unclaimed"
    assert settings.saved == 1


async def test_active_or_persisted_active_control_cannot_be_edited(hass, config):
    config = deepcopy(config)
    config["control"] = editable_control(config)
    item = entry(hass, config)
    item.runtime_data = SimpleNamespace(
        controls=SimpleNamespace(
            boiler=SimpleNamespace(override="shadow"),
            rooms={"study": SimpleNamespace(mode="shadow")},
            settings=Settings(ownership="ready", modes={"study": "active"}),
        )
    )
    flow = await choose_control(hass, item)
    assert flow["type"] == "abort"
    assert flow["reason"] == "control_active"


async def test_observation_mapping_allows_additions_but_keeps_controlled_room(hass, config):
    config = deepcopy(config)
    config["control"] = editable_control(config)
    item = entry(hass, config)
    flow = await hass.config_entries.options.async_init(item.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "mapping"}
    )
    refused = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"zones": ["climate.other"]}
    )
    assert refused["errors"] == {"base": "controlled_rooms_locked"}
    accepted = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"zones": ["climate.study", "climate.other"]}
    )
    assert accepted["step_id"] == "room"


def test_observer_sensor_change_does_not_mutate_control_sensor(config):
    config = deepcopy(config)
    config["rooms"][0]["air_sensor"] = "sensor.observer_air"
    config["control"] = editable_control(config)
    config["control"]["rooms"]["study"]["config"]["air_temp_sensor"] = "sensor.control_air"
    changed_observer = deepcopy(config)
    changed_observer["rooms"][0]["air_sensor"] = "sensor.new_observer_air"
    changed_observer["rooms"].append({"id": "other", "name": "Other", "climate": "climate.other"})
    edited = editable_control(changed_observer)
    assert edited["rooms"]["study"]["config"]["air_temp_sensor"] == "sensor.control_air"
    assert "other" not in edited["rooms"]


def test_duplicate_control_actuator_or_survey_binding_is_rejected(config):
    control = editable_control(deepcopy(config))
    duplicate = deepcopy(control["rooms"]["study"])
    control["rooms"]["other"] = duplicate
    try:
        validate_control_rooms(control)
    except ControlConfigError as err:
        assert err.code == "control_duplicate_room"
    else:
        raise AssertionError("duplicate control mapping accepted")


async def test_loaded_shadow_boiler_edit_reaches_runtime_without_writes(
    hass, controlled, sources, survey_files
):
    hass.config.config_dir = str(survey_files.parent)
    controlled = deepcopy(controlled)
    controlled["control"]["hub"]["house_dir"] = "house"
    controlled["control"]["rooms"]["study"]["config"]["room_id"] = "study"
    item, controls, calls = await start(hass, controlled)
    await handover(controls)
    assert controls.settings.get("ownership") == "ready"
    controls.boiler.enabled = False
    controls.rooms["study"].enabled = False
    controls.rooms["study"].occupancy_enabled = False

    flow = await choose_control(hass, item)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "control_boiler"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "enabled": False,
            "flow_setpoint_entity": "number.flow_setpoint",
            "outdoor_temp_entity": "sensor.outdoor",
            "heat_demand_entity": "sensor.study_demand",
            "flow_min": 41,
            "flow_max": 59,
            "manual_hold_minutes": 75,
        },
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    restored = item.runtime_data.controls
    assert restored.boiler._config["flow_min"] == 41
    assert restored.boiler._config["flow_max"] == 59
    assert restored.boiler._config["manual_hold_minutes"] == 75
    assert not restored.boiler.enabled
    assert not restored.rooms["study"].enabled
    assert not restored.rooms["study"].occupancy_enabled
    assert restored.rooms["study"]._config["air_temp_sensor"] == "sensor.air"
    assert restored.settings.get("ownership") == "ready"
    assert restored.boiler.override == "shadow"
    assert restored.rooms["study"].mode == "shadow"
    assert calls == []


async def test_loaded_standalone_setup_reaches_both_shadow_coordinators(
    hass, config, sources, survey_files
):
    hass.config.config_dir = str(survey_files.parent)
    hass.states.async_set("sensor.control_air", 18, {"unit_of_measurement": "°C"})
    item = await setup(hass, {**deepcopy(config), "survey_directory": "house"})
    calls = []

    async def number(call):
        calls.append(("number", dict(call.data)))

    async def climate(call):
        calls.append(("climate", dict(call.data)))

    hass.services.async_register("number", "set_value", number)
    hass.services.async_register("ramses_cc", "set_zone_mode", climate)
    flow = await choose_control(hass, item)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "house_dir": "house",
            "outdoor_temp_sensor": "sensor.outdoor",
            "flow_temp_entity": "sensor.flow",
        },
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "flow_setpoint_entity": "number.flow_setpoint",
            "outdoor_temp_entity": "sensor.outdoor",
            "current_flow_entity": "sensor.flow",
            "return_temp_entity": "sensor.return",
            "heating_active_entity": "binary_sensor.heating",
            "heat_demand_entity": "sensor.study_demand",
            "flow_min": 35,
            "flow_max": 60,
        },
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "primary_climate": "climate.study",
            "room_id": "study",
            "air_temp_sensor": "sensor.control_air",
            "zone_setpoint_min": 8,
            "zone_setpoint_max": 24,
        },
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    controls = item.runtime_data.controls
    assert controls.config["origin"] == "standalone"
    assert controls.config["legacy_entries"] == []
    assert controls.boiler is not None
    assert set(controls.rooms) == {"study"}
    assert controls.boiler.override == "shadow"
    assert controls.rooms["study"].mode == "shadow"
    assert controls.rooms["study"]._config["air_temp_sensor"] == "sensor.control_air"
    policy = controls.rooms["study"]._policy_params()
    assert policy.zone_setpoint_min == 8
    assert policy.zone_setpoint_max == 24
    assert controls.settings.get("ownership") == "unclaimed"
    assert calls == []
