"""Unit conversions, source validity and temperature/demand semantics."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.core import State

from custom_components.home_heating_optimisation.observations import make_snapshot, read

NOW = datetime(2026, 9, 15, 12, tzinfo=timezone.utc)


def source(value, unit="°C", age=0):
    return State(
        "sensor.input",
        str(value),
        {"unit_of_measurement": unit},
        last_reported=NOW - timedelta(seconds=age),
    )


@pytest.mark.parametrize(
    ("value", "unit", "expected"), [(68, "°F", 20), (293.15, "K", 20), (20, "°C", 20)]
)
def test_temperature_conversion(value, unit, expected):
    reading = read({"sensor.input": source(value, unit)}, "sensor.input", NOW)
    assert reading.value == pytest.approx(expected)
    assert reading.quality == "ok"


@pytest.mark.parametrize(
    ("value", "unit"), [("nan", "°C"), ("inf", "°C"), (20, "bananas"), (200, "°C"), (20, None)]
)
def test_invalid_temperatures(value, unit):
    reading = read({"sensor.input": source(value, unit)}, "sensor.input", NOW)
    assert reading.value is None
    assert reading.quality == "invalid"


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (50, "%", 0.5),
        (0.5, None, 0.5),
        (0, "%", 0),
        (100, "%", 1),
        (50, None, None),
        (-1, "%", None),
        (101, "%", None),
        (0.5, "W", None),
    ],
)
def test_demand_units(value, unit, expected):
    reading = read({"sensor.input": source(value, unit)}, "sensor.input", NOW, kind="demand")
    assert reading.value == expected


def test_quality_and_static_targets():
    assert read({}, None, NOW).quality == "not_configured"
    assert read({}, "sensor.input", NOW).quality == "missing"
    assert (
        read({"sensor.input": source("unavailable")}, "sensor.input", NOW).quality == "unavailable"
    )
    assert read({"sensor.input": source(20, age=1801)}, "sensor.input", NOW).quality == "stale"
    assert read({"sensor.input": source(20, age=-1)}, "sensor.input", NOW).quality == "invalid"
    assert (
        read({"sensor.input": source(20, age=90000)}, "sensor.input", NOW, max_age=None).value == 20
    )


@pytest.mark.parametrize(
    ("heating", "dhw", "expected"),
    [
        ("on", "on", "mixed"),
        ("on", "off", "heating"),
        ("off", "on", "dhw"),
        ("off", "off", "idle"),
        ("off", "unavailable", "unknown"),
    ],
)
def test_operating_state_preserves_unknown(config, heating, dhw, expected):
    states = {
        entity: State(entity, value, last_reported=NOW)
        for entity, value in (("binary_sensor.heating", heating), ("binary_sensor.dhw", dhw))
    }
    assert make_snapshot(states, config, NOW, "°C").operating_state == expected


def test_independent_air_does_not_change_command_or_regulating_sensor(config):
    config["rooms"][0]["air_sensor"] = "sensor.input"
    states = {
        "sensor.input": source(66.2, "°F"),
        "climate.study": State(
            "climate.study",
            "auto",
            {"temperature": 68, "current_temperature": 70},
            last_reported=NOW,
        ),
    }
    room = make_snapshot(states, config, NOW, "°F").rooms[0]
    assert room.air.value == pytest.approx(19)
    assert room.target.value == pytest.approx(20)
    assert room.deficit == 1
    assert states["climate.study"].attributes["current_temperature"] == 70


def test_off_room_has_no_deficit_and_missing_demand_is_not_zero(config):
    states = {
        "climate.study": State(
            "climate.study",
            "off",
            {"temperature": 20, "current_temperature": 18},
            last_reported=NOW,
        )
    }
    room = make_snapshot(states, config, NOW, "°C").rooms[0]
    assert room.deficit is None
    assert room.demand.value is None


def test_recent_identical_report_is_fresh():
    state = State(
        "sensor.input",
        "20",
        {"unit_of_measurement": "°C"},
        last_updated=NOW - timedelta(hours=4),
        last_reported=NOW,
    )
    assert read({"sensor.input": state}, "sensor.input", NOW).quality == "ok"
