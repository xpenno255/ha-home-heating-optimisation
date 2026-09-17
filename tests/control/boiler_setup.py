"""Run the original boiler release regressions through the consolidated setup."""

from types import SimpleNamespace

from custom_components.home_heating_optimisation.control.migration import handover
from tests.test_integration import setup


async def _setup(hass):
    for entity, value in [
        ("number.boiler_selflowtemp", 50),
        ("sensor.outdoor_temp", -3),
        ("sensor.heat_demand", 80),
        ("sensor.hw_relay_demand", 0),
    ]:
        hass.states.async_set(entity, value)
    cfg = {
        "rooms": [],
        "control": {
            "schema": 1,
            "hub": {},
            "rooms": {},
            "boiler": {
                "config": {
                    "flow_setpoint_entity": "number.boiler_selflowtemp",
                    "outdoor_temp_entity": "sensor.outdoor_temp",
                    "heat_demand_entity": "sensor.heat_demand",
                    "hw_relay_demand_entity": "sensor.hw_relay_demand",
                }
            },
            "legacy_entries": [],
        },
    }
    entry = await setup(hass, cfg)
    calls = []

    async def fake(call):
        calls.append(dict(call.data))
        hass.states.async_set(call.data["entity_id"], call.data["value"])

    hass.services.async_register("number", "set_value", fake)
    await handover(entry.runtime_data.controls)
    return SimpleNamespace(
        runtime_data=entry.runtime_data.controls.boiler, entry_id=entry.entry_id
    ), calls
