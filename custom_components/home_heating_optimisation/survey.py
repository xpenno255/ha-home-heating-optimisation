"""Read-only, bounded import of the existing house.yaml + rooms/*.yaml format.

Only thermal/layout fields enter evidence. Raw documents and household/network
metadata never leave the loader. This is surveyed context, not a heat-loss solver.
"""

from __future__ import annotations

import hashlib
import math
import re
from datetime import date
from pathlib import Path

import yaml
from homeassistant.util import dt as dt_util

MAX_FILE_BYTES = 256 * 1024
MAX_FILES = 128
MAX_TOTAL_BYTES = 4 * 1024 * 1024
CONFIDENCE = {"surveyed", "estimated", "unknown"}
BOUNDARIES = {"outside", "ground", "heated_room", "unheated_space", "loft", "roof"}


class SurveyError(ValueError):
    """A safe diagnostic code, never source text or a filesystem path."""


class SurveyLoader(yaml.SafeLoader):
    """Reject duplicate keys and aliases instead of accepting ambiguous surveys."""

    def compose_node(self, parent, index):
        if self.check_event(yaml.AliasEvent):
            raise SurveyError("yaml_alias_not_supported")
        self.node_count = getattr(self, "node_count", 0) + 1
        if self.node_count > 10000:
            raise SurveyError("survey_too_complex")
        return super().compose_node(parent, index)

    def construct_mapping(self, node, deep=False):
        keys = [self.construct_object(key, deep=deep) for key, _ in node.value]
        if any(not isinstance(key, str) for key in keys) or len(keys) != len(set(keys)):
            raise SurveyError("invalid_or_duplicate_yaml_key")
        return super().construct_mapping(node, deep=deep)


def mapping(value):
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise SurveyError("expected_mapping")
    return value


def items(value):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > 256:
        raise SurveyError("invalid_collection")
    return [mapping(v) for v in value]


def label(value):
    if not isinstance(value, (str, int)) or isinstance(value, bool) or len(str(value)) > 255:
        raise SurveyError("invalid_label")
    return str(value)


def numeric(value, *, maximum=100000, zero=False):
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise SurveyError("invalid_number")
    if value < 0 or (not zero and value == 0) or value > maximum:
        raise SurveyError("number_out_of_range")
    return value


def provenance(data):
    confidence = data.get("confidence", "unknown")
    if not isinstance(confidence, str) or confidence not in CONFIDENCE:
        raise SurveyError("invalid_confidence")
    result = {"confidence": confidence}
    if (measured := data.get("measured_on")) is not None:
        try:
            result["measured_on"] = date.fromisoformat(str(measured)).isoformat()
        except ValueError as err:
            raise SurveyError("invalid_measurement_date") from err
    return result


def measurements(data, keys):
    return {key: numeric(data.get(key)) for key in keys}


def adjacency(value):
    if value is None:
        return []
    if isinstance(value, dict):
        result = [
            {"room_id": label(k), "area_fraction": numeric(v, maximum=1)} for k, v in value.items()
        ]
        if (
            not result
            or any(r["area_fraction"] is None for r in result)
            or not math.isclose(sum(r["area_fraction"] for r in result), 1, abs_tol=0.001)
        ):
            raise SurveyError("invalid_adjacency_fractions")
        return result
    if not isinstance(value, str):
        raise SurveyError("invalid_adjacency")
    names = list(
        dict.fromkeys(re.sub(r"\s*\([^)]*\)", "", v).strip() for v in value.split(",") if v.strip())
    )
    return [{"room_id": label(n), "area_fraction": 1 if len(names) == 1 else None} for n in names]


def clean_room(data, fallback, constructions, warnings):
    info = mapping(data.get("room"))
    rid = label(info.get("id", fallback))
    if not rid.strip():
        raise SurveyError("missing_room_id")
    heated = info.get("heated")
    if heated is not None and type(heated) is not bool:
        raise SurveyError("invalid_heated_flag")
    geometry = mapping(data.get("geometry"))
    room = {
        "id": rid,
        "name": label(info.get("name", rid)),
        "heated": heated,
        "floor": label(info["floor"]) if info.get("floor") is not None else None,
        "geometry": {
            **measurements(
                geometry,
                (
                    "length_m",
                    "width_m",
                    "height_m",
                    "height_assumed_m",
                    "floor_area_m2",
                    "volume_m3",
                ),
            ),
            **provenance(geometry),
        },
        "boundaries": [],
        "openings": [],
        "emitters": [],
    }
    if geometry.get("floor_area_m2") is None:
        warnings.append({"code": "missing_floor_area", "room_id": rid})
    for section, collection, destination in (
        ("boundaries", "faces", "boundaries"),
        ("openings", "items", "openings"),
        ("heating", "emitters", "emitters"),
    ):
        block = mapping(data.get(section))
        for item in items(block.get(collection)):
            out = {**provenance({**block, **item})}
            for key in ("face", "boundary", "construction", "type", "wall", "orientation"):
                if item.get(key) is not None:
                    out[key] = label(item[key])
            if section == "boundaries":
                out["gross_area_m2"] = numeric(item.get("gross_area_m2"))
                out["adjacent"] = adjacency(item.get("adjacent"))
                if out.get("boundary") not in BOUNDARIES:
                    warnings.append({"code": "unknown_boundary", "room_id": rid})
                if out["gross_area_m2"] is None:
                    warnings.append({"code": "missing_boundary_area", "room_id": rid})
                if len(out["adjacent"]) > 1 and out["adjacent"][0]["area_fraction"] is None:
                    warnings.append({"code": "unspecified_adjacency_fractions", "room_id": rid})
            else:
                out.update(
                    measurements(
                        item,
                        ("height_m", "width_m", "length_m", "area_m2")
                        if section == "openings"
                        else ("height_m", "width_m", "length_m", "output_dt50_w"),
                    )
                )
                if section == "openings":
                    for key, maximum in (("tilt_deg", 180), ("g_value", 1), ("shade_factor", 1)):
                        out[key] = numeric(item.get(key), maximum=maximum, zero=True)
                elif out["output_dt50_w"] is None:
                    warnings.append({"code": "missing_emitter_rating", "room_id": rid})
            if section in ("boundaries", "openings"):
                key = out.get("construction")
                out["construction_properties"] = constructions.get(key)
                if key not in constructions:
                    warnings.append({"code": "unknown_construction", "room_id": rid})
            room[destination].append(out)
    heating = mapping(data.get("heating"))
    # Used only for deterministic mapping suggestions, not changing selected sensors.
    bindings = [label(heating[k]) for k in ("climate_primary", "climate_backup") if heating.get(k)]
    return room, bindings


def load_survey(directory, config_root):
    """All disk I/O stays in an HA executor; no copies, migrations or writes."""
    if not directory:
        return {"status": "not_configured", "rooms": {}, "bindings": {}, "warnings": []}
    root = Path(config_root).resolve()
    candidate = Path(directory)
    folder = (candidate if candidate.is_absolute() else root / candidate).resolve()
    if not folder.is_relative_to(root):
        raise SurveyError("survey_outside_config_directory")
    files = [folder / "house.yaml", *sorted((folder / "rooms").glob("*.yaml"))]
    files = [p for p in files if not p.name.startswith("_")]
    if len(files) < 2:
        raise SurveyError("missing_room_files")
    if len(files) > MAX_FILES:
        raise SurveyError("too_many_survey_files")
    digest = hashlib.sha256()
    documents = []
    total = 0
    try:
        for path in files:
            if not path.resolve().is_relative_to(folder):
                raise SurveyError("survey_file_outside_directory")
            with path.open("rb") as stream:
                content = stream.read(MAX_FILE_BYTES + 1)
            total += len(content)
            if len(content) > MAX_FILE_BYTES or total > MAX_TOTAL_BYTES:
                raise SurveyError("survey_too_large")
            digest.update(path.relative_to(folder).as_posix().encode() + b"\0" + content)
            data = mapping(yaml.load(content.decode("utf-8"), Loader=SurveyLoader))
            if type(data.get("schema_version", 1)) is not int or data.get("schema_version", 1) != 1:
                raise SurveyError("unsupported_survey_version")
            documents.append(data)
    except (OSError, UnicodeError, yaml.YAMLError, RecursionError) as err:
        raise SurveyError("cannot_read_survey") from err
    house = documents[0]
    constructions = {}
    for key, value in mapping(house.get("constructions")).items():
        data = mapping(value)
        constructions[label(key)] = {
            "u_value_w_m2k": numeric(data.get("u_value"), maximum=50),
            "description": label(data["description"])
            if data.get("description") is not None
            else None,
            **provenance(data),
        }
    facts = mapping(house.get("house"))
    orientation = {
        label(k): numeric(v, maximum=360, zero=True)
        for k, v in mapping(facts.get("face_bearings_deg")).items()
    }
    warnings, rooms, bindings = [], {}, {}
    for path, document in zip(files[1:], documents[1:]):
        room, links = clean_room(document, path.stem, constructions, warnings)
        if room["id"] in rooms:
            raise SurveyError("duplicate_room_id")
        rooms[room["id"]] = room
        bindings[room["id"]] = links
    for room in rooms.values():
        for boundary in room["boundaries"]:
            for adjacent in boundary["adjacent"]:
                adjacent["resolved"] = adjacent["room_id"] in rooms
                if not adjacent["resolved"]:
                    warnings.append({"code": "unresolved_adjacency", "room_id": room["id"]})
    return {
        "status": "partial" if warnings else "ready",
        "schema_version": 1,
        "revision": digest.hexdigest(),
        "loaded_at": dt_util.utcnow().isoformat(),
        "house": {
            "face_bearings_deg": orientation,
            "front_elevation_bearing_deg": numeric(
                facts.get("front_elevation_bearing_deg"), maximum=360, zero=True
            ),
            **provenance(facts),
        },
        "rooms": rooms,
        "bindings": bindings,
        "warnings": warnings,
    }


def suggest_mappings(survey, config):
    suggestions = {}
    for room in config["rooms"]:
        matches = [
            rid for rid, entities in survey["bindings"].items() if room["climate"] in entities
        ]
        if len(matches) == 1:
            suggestions[room["id"]] = matches[0]
    return suggestions


def evidence(survey, config):
    """Current survey revision, explicitly separate from historical observations."""
    selected = config.get("survey_rooms", {})
    links = {r["id"]: selected.get(r["id"]) for r in config["rooms"]}
    mapped = [v for v in links.values() if v in survey["rooms"]]
    missing = [k for k, v in links.items() if v and v not in survey["rooms"]]
    status = (
        "mapping_invalid"
        if missing and survey["status"] not in ("error", "not_configured")
        else survey["status"]
    )
    return {
        **{k: v for k, v in survey.items() if k != "bindings"},
        "status": status,
        "room_mapping": links,
        "mapped_room_count": len(mapped),
        "unmapped_configured_room_count": len(links) - len(mapped),
        "unmapped_survey_rooms": [r for r in survey["rooms"] if r not in mapped],
        "invalid_mapping_count": len(missing),
        "meaning": "Current surveyed building context; estimates retain confidence. Not measured heat loss or historical layout.",
    }


async def async_load_survey(hass, config):
    try:
        return await hass.async_add_executor_job(
            load_survey, config.get("survey_directory"), hass.config.config_dir
        )
    except (SurveyError, OSError, RuntimeError) as err:
        code = str(err) if isinstance(err, SurveyError) else "cannot_read_survey"
        return {
            "status": "error",
            "error_code": code,
            "rooms": {},
            "bindings": {},
            "warnings": [],
        }
