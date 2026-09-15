"""State availability, acquisition recency, downtime and bounded context storage."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from custom_components.home_heating_optimisation.analytics.analyzer import compute_analytics
from custom_components.home_heating_optimisation.analytics.backfill import reconstruct
from custom_components.home_heating_optimisation.analytics.store import (
    HistoryStore,
    pack_history,
    source_signature,
    unpack_history,
)
from custom_components.home_heating_optimisation.survey import load_survey
from tests.test_history import BASE, state
from tests.test_survey import survey_files  # noqa: F401


def states():
    return {
        "climate.study": [state("climate.study", "auto", current_temperature=18, temperature=20)],
        "sensor.study_demand": [state("sensor.study_demand", 80, unit_of_measurement="%")],
    }


def test_unchanged_available_state_and_recent_changes_are_distinct(config):
    config["history_state_policy"] = "recorded_state"
    end = BASE + timedelta(days=1)
    points, _ = reconstruct(states(), BASE, end, config, "°C")
    result = compute_analytics(points, ["study"], 1, now=end).zone_stats["study"]
    assert result.coverage == result.demand_coverage == 100
    assert result.recent_change_coverage == result.demand_recent_change_coverage == 2.1
    assert result.heating_rate_avg is None  # Timer snapshots are not temperature reports.


def test_run_gap_and_missing_post_restart_state_are_unknown(config):
    config["history_state_policy"] = "recorded_state"
    end = BASE + timedelta(hours=3)
    runs = [
        (BASE, BASE + timedelta(hours=1), True),
        (BASE + timedelta(hours=2), end + timedelta(seconds=1), True),
    ]
    points, _ = reconstruct(states(), BASE, end, config, "°C", runs=runs)
    assert all(
        p["zones"]["study"]["temperature"] is None
        for p in points
        if p["time"] >= (BASE + timedelta(hours=1)).timestamp()
    )
    result = compute_analytics(points, ["study"], 1, now=end).zone_stats["study"]
    assert result.observed_hours == 1


def test_unclean_run_uses_conservative_expiry(config):
    config["history_state_policy"] = "recorded_state"
    end = BASE + timedelta(hours=3)
    points, _ = reconstruct(
        states(), BASE, end, config, "°C", runs=[(BASE, end + timedelta(seconds=1), False)]
    )
    assert points[-1]["zones"]["study"]["temperature"] is None


def test_dense_controller_and_numeric_events_keep_room_edges(config):
    history = states()
    config["rooms"][0]["decision_sensor"] = "sensor.decision"
    history["sensor.decision"] = [state("sensor.decision", i, minute=i / 60) for i in range(3600)]
    history["sensor.flow"] = [
        state("sensor.flow", 40 + i % 10, minute=i / 60, unit_of_measurement="°C")
        for i in range(0, 3600, 5)
    ]
    history["climate.study"].append(
        state("climate.study", "auto", minute=12.25, current_temperature=18, temperature=16)
    )
    history["sensor.study_demand"].append(state("sensor.study_demand", "unavailable", minute=12.5))
    points, limited = reconstruct(history, BASE, BASE + timedelta(hours=1), config, "°C")
    assert not limited and len(points) < 90
    by_time = {p["time"]: p for p in points}
    assert by_time[(BASE + timedelta(minutes=12.25)).timestamp()]["zones"]["study"]["target"] == 16
    assert by_time[(BASE + timedelta(minutes=12.5)).timestamp()]["zones"]["study"]["active"] is None
    assert sum("intent" in p for p in points) == 13


async def test_context_separated_compressed_and_legacy_migrated(hass, config):
    points, _ = reconstruct(states(), BASE, BASE + timedelta(hours=2), config, "°C")
    original = deepcopy(points)
    store = HistoryStore(hass, "compact")
    await store.load(source_signature(config, "°C"))
    store.merge(points)
    assert all("intent" not in p for p in store.observations)
    assert store.decision_context
    await store.save()
    loaded = HistoryStore(hass, "compact")
    await loaded.load(source_signature(config, "°C"))
    assert loaded.observations == store.observations
    assert loaded.decision_context == store.decision_context
    assert points == original
    payload = {
        "schema": 1,
        "observations": points,
        "adjustments": [],
        "signature": source_signature(config, "°C"),
    }
    with patch.object(store.backend, "async_load", return_value=payload):
        await store.load(source_signature(config, "°C"))
    assert store.status == "ready"
    assert all("intent" not in p for p in store.observations)
    packed = pack_history({"observations": original, "decision_context": [], "previous_era": None})
    assert unpack_history(packed)["observations"] == original


def test_survey_defaults_are_labelled_and_unsurveyed_spaces_are_references(survey_files):  # noqa: F811
    import yaml

    path = survey_files / "house.yaml"
    house = yaml.safe_load(path.read_text())
    house["constructions"]["internal_wall"] = {"u_value": 1.5, "confidence": "estimated"}
    path.write_text(yaml.safe_dump(house))
    path = survey_files / "rooms/study.yaml"
    room = yaml.safe_load(path.read_text())
    room["boundaries"]["faces"].append(
        {
            "face": "E",
            "boundary": "heated_room",
            "gross_area_m2": 4,
            "adjacent": "toilet (first floor), landing",
        }
    )
    path.write_text(yaml.safe_dump(room))
    model = load_survey(survey_files, survey_files.parent)
    wall = model["rooms"]["study"]["boundaries"][1]
    assert wall["construction"] == "internal_wall"
    assert wall["construction_source"] == "house_default"
    assert wall["construction_properties"]["confidence"] == "estimated"
    assert all(a["area_fraction"] is None for a in wall["adjacent"])
    assert "toilet_first_floor" in model["referenced_spaces"]
    assert model["status"] == "ready" and not model["warnings"]
    assert model["advisories"]


def test_replay_sampling_stays_identical_when_reload_time_moves(config):
    """An overlapping Recorder rebuild must not create a second sample grid."""
    from datetime import timedelta

    from homeassistant.core import State
    from homeassistant.util import dt as dt_util

    start = dt_util.parse_datetime("2026-09-01T00:00:00+00:00")
    config = {**config, "history_state_policy": "recorded_state"}
    history = {
        "climate.study": [
            State(
                "climate.study",
                "heat",
                {"current_temperature": 18, "temperature": 20},
                last_updated=start,
            )
        ],
        "sensor.flow": [
            State(
                "sensor.flow",
                str(40 + i % 3),
                {"unit_of_measurement": "°C"},
                last_updated=start + timedelta(seconds=i),
            )
            for i in range(7200)
        ],
    }
    first, _ = reconstruct(
        history, start + timedelta(seconds=17), start + timedelta(seconds=7017), config, "°C"
    )
    second, _ = reconstruct(
        history, start + timedelta(seconds=47), start + timedelta(seconds=7047), config, "°C"
    )
    lower, upper = (
        (start + timedelta(minutes=5)).timestamp(),
        (start + timedelta(minutes=115)).timestamp(),
    )
    a = {p["time"]: p for p in first if lower <= p["time"] <= upper}
    b = {p["time"]: p for p in second if lower <= p["time"] <= upper}
    assert a == b
    assert len(a) < 200
