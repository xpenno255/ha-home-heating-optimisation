"""The rooms overview sensor behind the HHO rooms card."""

from custom_components.home_heating_optimisation.card import URL, async_register_card
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.control.overview import ENTITY_ID
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start


async def test_overview_carries_each_room_and_the_boiler(hass, controlled, sources):  # noqa: F811
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 18.0, "temperature": 20, "heat_demand": 0.35},
    )
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    c.rooms["study"]._overdrive_settings = lambda: settings
    await c.refresh()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state is not None and state.state == "1"
    [room] = state.attributes["rooms"]
    data = c.rooms["study"].data
    assert room["id"] == "study" and room["name"] == c.rooms["study"].room_name
    assert room["air"] == round(data.air_temp, 2) and room["target"] == round(data.air_setpoint, 2)
    assert room["overdrive"] == 1.0 and room["demand"] == 35 and room["calling"] is True
    assert room["preheat"] is None
    assert set(state.attributes["house"]) >= {"outdoor", "flow", "flow_target", "return", "burner"}
    assert "hot_water" in state.attributes


async def test_card_registration_skips_without_a_frontend(hass):
    await async_register_card(hass)
    assert URL.endswith("hho-rooms-card.js")
    assert not hass.data.get("home_heating_optimisation_card_registered")


async def test_hot_water_view_reads_the_cylinder_and_cloud_schedule(hass, controlled, sources):  # noqa: F811
    from custom_components.home_heating_optimisation.control.overview import hot_water_view

    entry, c, calls = await start(hass, controlled)
    hass.states.async_set("sensor.cyl", "47.24")
    hass.states.async_set(
        "water_heater.hw", "auto", {"temperature": 50, "mode": {"mode": "follow_schedule"}}
    )
    hass.states.async_set(
        "water_heater.cloud",
        "auto",
        {
            "status": {
                "setpoints": {
                    "this_sp_state": "Off",
                    "next_sp_from": "2026-11-03T04:30:00+00:00",
                    "next_sp_state": "On",
                }
            }
        },
    )
    c.heating.dhw_cutoff.cfg = {
        "water_heater_entity": "water_heater.hw",
        "cylinder_temp_entity": "sensor.cyl",
        "cloud_entity": "water_heater.cloud",
    }
    view = hot_water_view(c)
    assert view["temp"] == 47.2 and view["target"] == 50.0 and view["mode"] == "follow_schedule"
    assert view["scheduled_on"] is False and view["next_state"] == "On"
