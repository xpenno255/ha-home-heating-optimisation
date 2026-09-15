"""Survey safety, mapping, provenance and HA lifecycle without source writes."""

from copy import deepcopy
from unittest.mock import patch

import pytest
import yaml
from homeassistant.exceptions import HomeAssistantError

from custom_components.home_heating_optimisation.analytics.store import source_signature
from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.survey import (
    SurveyError,
    evidence,
    load_survey,
    suggest_mappings,
)
from tests.test_integration import entity_id, setup


@pytest.fixture
def survey_files(tmp_path):
    folder = tmp_path / "house"
    (folder / "rooms").mkdir(parents=True)
    house = {
        "house": {
            "confidence": "surveyed",
            "face_bearings_deg": {"N": 7},
            "address_hint": "PRIVATE ADDRESS",
        },
        "network": {"password": "PRIVATE NETWORK"},
        "constructions": {"brick": {"u_value": 1.5, "confidence": "estimated"}},
    }
    room = {
        "room": {"id": "study", "name": "Study", "floor": 0, "heated": True},
        "geometry": {
            "floor_area_m2": 12,
            "height_m": 2.4,
            "confidence": "surveyed",
            "measured_on": "2026-09-01",
        },
        "boundaries": {
            "faces": [
                {"face": "N", "boundary": "outside", "construction": "brick", "gross_area_m2": 8}
            ]
        },
        "openings": {
            "items": [{"type": "window", "face": "N", "area_m2": 2, "construction": "brick"}]
        },
        "heating": {
            "climate_primary": "climate.study",
            "emitters": [{"type": "radiator", "output_dt50_w": 900, "confidence": "estimated"}],
        },
        "usage": {"occupied_pattern": "PRIVATE ROUTINE"},
        "photos": ["PRIVATE PHOTO"],
    }
    (folder / "house.yaml").write_text(yaml.safe_dump(house))
    (folder / "rooms/study.yaml").write_text(yaml.safe_dump(room))
    return folder


def test_existing_schema_confidence_privacy_and_no_writes(survey_files, config):
    before = {p: p.read_bytes() for p in survey_files.rglob("*.yaml")}
    result = load_survey(survey_files, survey_files.parent)
    assert result["status"] == "ready"
    assert result["rooms"]["study"]["geometry"]["floor_area_m2"] == 12
    assert result["rooms"]["study"]["geometry"]["confidence"] == "surveyed"
    assert (
        result["rooms"]["study"]["boundaries"][0]["construction_properties"]["confidence"]
        == "estimated"
    )
    assert "PRIVATE" not in str(result)
    assert suggest_mappings(result, config) == {"study": "study"}
    assert before == {p: p.read_bytes() for p in before}
    config["survey_rooms"] = {"study": "study"}
    report = evidence(result, config)
    assert report["mapped_room_count"] == 1
    assert "climate.study" not in str(report)
    assert "bindings" not in report


@pytest.mark.parametrize(
    "contents",
    [
        "room: [broken]",
        "room: {id: study}\ngeometry: {floor_area_m2: -3}",
        "room: {id: study}\ngeometry: {floor_area_m2: .nan}",
        "room: {id: study}\ngeometry: {confidence: [surveyed]}",
        "room: {id: study}\nroom: {id: other}",
        "room: &room {id: study}\ncopy: *room",
        "!!python/object/apply:os.system ['echo unsafe']",
        "schema_version: 99",
    ],
)
def test_invalid_yaml_or_schema_rejected(survey_files, contents):
    (survey_files / "rooms/study.yaml").write_text(contents)
    with pytest.raises(SurveyError):
        load_survey(survey_files, survey_files.parent)


def test_duplicate_rooms_and_escaping_paths_rejected(survey_files, tmp_path):
    (survey_files / "rooms/duplicate.yaml").write_bytes(
        (survey_files / "rooms/study.yaml").read_bytes()
    )
    with pytest.raises(SurveyError, match="duplicate_room_id"):
        load_survey(survey_files, tmp_path)
    with pytest.raises(SurveyError, match="outside_config"):
        load_survey(survey_files, survey_files / "rooms")
    (survey_files / "rooms/duplicate.yaml").unlink()
    outside = tmp_path / "outside.yaml"
    outside.write_text("room: {id: outside}")
    (survey_files / "rooms/link.yaml").symlink_to(outside)
    with pytest.raises(SurveyError, match="outside_directory"):
        load_survey(survey_files, tmp_path)


def test_partial_adjacency_no_invented_shares_and_ambiguous_bindings(survey_files, config):
    path = survey_files / "rooms/study.yaml"
    room = yaml.safe_load(path.read_text())
    room["boundaries"]["faces"].append(
        {"face": "E", "boundary": "heated_room", "adjacent": "hall, landing", "gross_area_m2": 4}
    )
    path.write_text(yaml.safe_dump(room))
    other = deepcopy(room)
    other["room"]["id"] = "landing"
    (survey_files / "rooms/landing.yaml").write_text(yaml.safe_dump(other))
    result = load_survey(survey_files, survey_files.parent)
    assert result["status"] == "partial"
    assert suggest_mappings(result, config) == {}
    adjacent = result["rooms"]["study"]["boundaries"][1]["adjacent"]
    assert all(a["area_fraction"] is None for a in adjacent)
    assert [a["resolved"] for a in adjacent] == [False, True]


def test_survey_options_do_not_reset_measurement_history(config):
    before = source_signature(config, "°C")
    config.update(survey_directory="house", survey_rooms={"study": "study"})
    assert source_signature(config, "°C") == before


async def test_setup_reload_failure_recovers_and_report_without_analytics(
    hass, config, sources, survey_files
):
    hass.config.config_dir = str(survey_files.parent)
    config.update(survey_directory="house", survey_rooms={"study": "study"})
    entry = await setup(hass, config)
    status_id = entity_id(hass, entry, "system:house_model")
    assert hass.states.get(status_id).state == "ready"
    report = await hass.services.async_call(
        DOMAIN, "get_house_model", {}, blocking=True, return_response=True
    )
    old_revision = report["revision"]
    path = survey_files / "rooms/study.yaml"
    original = path.read_text()
    path.write_text("room: [broken]")
    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(DOMAIN, "reload_house_model", {}, blocking=True)
    assert hass.states.get(status_id).state == "error"
    assert entry.runtime_data.house_report()["rooms"] == {}
    assert hass.states.get(entity_id(hass, entry, "room:study:air")).state == "18.0"
    path.write_text(original + "\n# reviewed\n")
    await hass.services.async_call(DOMAIN, "reload_house_model", {}, blocking=True)
    assert entry.runtime_data.house_report()["revision"] != old_revision
    assert hass.states.get(status_id).state == "ready"


async def test_options_mapping_suggestions_and_clear(hass, config, sources, survey_files):
    hass.config.config_dir = str(survey_files.parent)
    entry = await setup(hass, config)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "mapping"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"zones": ["climate.study"]}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Study"})
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"survey_directory": "house"}
    )
    assert flow["step_id"] == "survey_rooms"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"survey_room": "study"}
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.house_report()["mapped_room_count"] == 1
    report = entry.runtime_data.house_report()
    with patch("homeassistant.core.ServiceRegistry.async_call") as calls:
        await entry.runtime_data.load_house()
        calls.assert_not_called()
    assert entry.runtime_data.house_report()["revision"] == report["revision"]


async def test_report_includes_current_survey_without_affecting_analytics(
    hass, config, sources, survey_files
):
    hass.config.config_dir = str(survey_files.parent)
    config.update(analytics_enabled=True, survey_directory="house", survey_rooms={"study": "study"})
    entry = await setup(hass, config)
    await entry.runtime_data.analytics.task
    before = deepcopy(entry.runtime_data.analytics.store.observations)
    report = await hass.services.async_call(
        DOMAIN, "get_report", {}, blocking=True, return_response=True
    )
    assert report["house_model"]["room_mapping"] == {"study": "study"}
    assert "PRIVATE" not in str(report)
    assert entry.runtime_data.analytics.store.observations == before


def test_bounded_file_and_adjacency_validation(survey_files):
    with patch("custom_components.home_heating_optimisation.survey.MAX_FILE_BYTES", 10):
        with pytest.raises(SurveyError, match="too_large"):
            load_survey(survey_files, survey_files.parent)
    path = survey_files / "rooms/study.yaml"
    room = yaml.safe_load(path.read_text())
    room["boundaries"]["faces"][0]["adjacent"] = {"hall": 0.8, "landing": 0.8}
    path.write_text(yaml.safe_dump(room))
    with pytest.raises(SurveyError, match="adjacency_fractions"):
        load_survey(survey_files, survey_files.parent)


async def test_invalid_directory_flow_and_explicit_disable(hass, config, sources, survey_files):
    hass.config.config_dir = str(survey_files.parent)
    config.update(survey_directory="house", survey_rooms={"study": "study"})
    entry = await setup(hass, config)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "mapping"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"zones": ["climate.study"]}
    )
    flow = await hass.config_entries.options.async_configure(flow["flow_id"], {"name": "Study"})
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"survey_directory": "missing"}
    )
    assert flow["errors"] == {"base": "invalid_survey"}
    assert entry.runtime_data.house_report()["mapped_room_count"] == 1
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"survey_directory": ""}
    )
    assert flow["type"] == "create_entry"
    await hass.async_block_till_done()
    assert entry.runtime_data.survey["status"] == "not_configured"
    assert (survey_files / "house.yaml").exists()
