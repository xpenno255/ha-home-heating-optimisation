"""Real HA setup, configuration, updates, expiry, reload and unload."""

from datetime import timedelta
from unittest.mock import patch

from homeassistant.config_entries import SOURCE_USER, ConfigEntryState
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.home_heating_optimisation.const import DOMAIN, effective_config
from custom_components.home_heating_optimisation.diagnostics import (
    async_get_config_entry_diagnostics,
)


def entity_id(hass, entry, suffix, domain="sensor"):
    return er.async_get(hass).async_get_entity_id(domain, DOMAIN, f"{entry.entry_id}:{suffix}")


async def setup(hass, config):
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", unique_id=DOMAIN, data=config)
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def test_full_user_flow_and_duplicate(hass, sources):
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"zones": []})
    assert result["errors"] == {"base": "no_rooms"}
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {"zones": ["climate.study"]}
    )
    assert result["step_id"] == "room"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": " "})
    assert result["errors"] == {"base": "invalid_name"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {"name": "Study"})
    assert result["step_id"] == "system"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    again = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert again["type"] == "abort"


async def test_observation_updates_unknown_and_no_control_calls(hass, config, sources):
    entry = await setup(hass, config)
    air = entity_id(hass, entry, "room:study:air")
    deficit = entity_id(hass, entry, "room:study:deficit")
    mode = entity_id(hass, entry, "system:operating_state")
    assert hass.states.get(air).state == "18.0"
    assert float(hass.states.get(deficit).state) == 2
    assert hass.states.get(mode).state == "heating"
    assert float(hass.states.get(entity_id(hass, entry, "system:input_availability")).state) == 100
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        hass.states.async_set("sensor.flow", "unavailable")
        hass.states.async_set("binary_sensor.dhw", "on")
        hass.states.async_set(
            "climate.study", "auto", {"current_temperature": 19.5, "temperature": 20}
        )
        await hass.async_block_till_done()
        assert hass.states.get(mode).state == "mixed"
        assert float(hass.states.get(deficit).state) == 0.5
        flow = hass.states.get(entity_id(hass, entry, "system:flow_temperature"))
        assert flow.state == "unknown"
        assert flow.attributes["quality"] == "unavailable"
        hass.states.async_set("binary_sensor.dhw", "unavailable")
        await hass.async_block_till_done()
        assert hass.states.get(mode).state == "unknown"
        assert (
            hass.states.get(entity_id(hass, entry, "system:dhw_active", "binary_sensor")).state
            == "unknown"
        )
        calls.assert_not_called()


async def test_silent_input_expiry_and_recovery(hass, config, sources, freezer):
    entry = await setup(hass, config)
    freezer.move_to(dt_util.utcnow() + timedelta(minutes=31))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    air = hass.states.get(entity_id(hass, entry, "room:study:air"))
    assert air.state == "unknown"
    assert air.attributes["quality"] == "stale"
    assert float(hass.states.get(entity_id(hass, entry, "room:study:target")).state) == 20
    assert float(hass.states.get(entity_id(hass, entry, "system:flow_setpoint")).state) == 55
    # Same value is a fresh report; the expiry timer also handles state_reported-only updates.
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18, "temperature": 20})
    freezer.move_to(dt_util.utcnow() + timedelta(seconds=31))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert float(hass.states.get(entity_id(hass, entry, "room:study:air")).state) == 18


async def test_options_clear_mappings_preserve_identity_and_remove_room(hass, config, sources):
    config["rooms"].append({"id": "old", "name": "Old room", "climate": "climate.old"})
    entry = await setup(hass, config)
    original_air_id = entity_id(hass, entry, "room:study:air")
    old_air_id = entity_id(hass, entry, "room:old:air")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": ["climate.study"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Renamed study"}
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    assert effective_config(entry)["flow_temperature"] is None
    assert effective_config(entry)["rooms"][0]["demand_sensor"] is None
    assert entity_id(hass, entry, "room:study:air") == original_air_id
    assert er.async_get(hass).async_get(old_air_id) is None
    assert hass.states.get(old_air_id) is None


async def test_unload_stops_updates_and_reload_preserves_ids(hass, config, sources):
    entry = await setup(hass, config)
    old_coordinator = entry.runtime_data
    original_air_id = entity_id(hass, entry, "room:study:air")
    assert await hass.config_entries.async_unload(entry.entry_id)
    with patch.object(old_coordinator, "snapshot") as refresh:
        hass.states.async_set(
            "climate.study", "auto", {"current_temperature": 17, "temperature": 20}
        )
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=1))
        await hass.async_block_till_done()
        refresh.assert_not_called()
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entity_id(hass, entry, "room:study:air") == original_air_id
    assert float(hass.states.get(original_air_id).state) == 17


async def test_missing_sources_do_not_prevent_setup_and_diagnostics_are_redacted(hass, config):
    entry = await setup(hass, config)
    assert entry.state is ConfigEntryState.LOADED
    data = await async_get_config_entry_diagnostics(hass, entry)
    assert data["input_availability_percent"] == 0
    assert "study" not in str(data).lower()
    assert "sensor." not in str(data)
    assert "climate." not in str(data)


async def test_self_source_rejected(hass, config, sources):
    entry = await setup(hass, config)
    own_air = entity_id(hass, entry, "room:study:air")
    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"zones": ["climate.study"]}
    )
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {"name": "Study", "air_sensor": own_air}
    )
    assert result["errors"] == {"base": "invalid_source"}
