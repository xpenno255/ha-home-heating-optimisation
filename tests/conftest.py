"""Tests use an in-memory Home Assistant, never the live installation."""

import pytest


@pytest.fixture(autouse=True)
def custom_integrations(enable_custom_integrations):
    yield


@pytest.fixture
def config():
    return {
        "history_state_policy": "recent_change",
        "rooms": [
            {
                "id": "study",
                "name": "Study",
                "climate": "climate.study",
                "air_sensor": None,
                "demand_sensor": "sensor.study_demand",
            }
        ],
        "outdoor_temperature": "sensor.outdoor",
        "flow_temperature": "sensor.flow",
        "return_temperature": "sensor.return",
        "flow_setpoint": "number.flow_setpoint",
        "heating_active": "binary_sensor.heating",
        "dhw_active": "binary_sensor.dhw",
    }


@pytest.fixture
def sources(hass):
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18, "temperature": 20})
    for entity, value in (
        ("sensor.study_demand", 50),
        ("sensor.outdoor", 5),
        ("sensor.flow", 50),
        ("sensor.return", 40),
        ("number.flow_setpoint", 55),
    ):
        hass.states.async_set(
            entity, value, {"unit_of_measurement": "%" if "demand" in entity else "°C"}
        )
    hass.states.async_set("binary_sensor.heating", "on")
    hass.states.async_set("binary_sensor.dhw", "off")
