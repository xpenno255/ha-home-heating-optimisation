"""Turn House survey YAML (house.yaml + rooms/<area>.yaml) into model inputs.

Pure Python plus PyYAML. Validation is deliberately loud: a room file with a
missing area or unknown construction produces `RoomGeometry.warnings`, and the
caller decides whether that is fatal.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .model import Boundary, Emitter, Surface

COMPASS = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
FACE_ALIASES = {
    "north": "N",
    "north_east": "NE",
    "east": "E",
    "south_east": "SE",
    "south": "S",
    "south_west": "SW",
    "west": "W",
    "north_west": "NW",
}
DEFAULT_BEARINGS = {"N": 0, "NE": 45, "E": 90, "SE": 135, "S": 180, "SW": 225, "W": 270, "NW": 315}
FACE_NAMES = {v: k for k, v in FACE_ALIASES.items()}
GLAZED_TYPES = {"window", "glazed_door", "rooflight", "fixed_glazed_return"}


def _face(value: str) -> str:
    """Use the same key for bearings and opening subtraction."""
    return FACE_ALIASES.get(str(value).lower(), str(value).upper())


def _adjacencies(value: Any, warnings: list[str], ctx: str) -> dict[str, float]:
    if not value:
        return {}
    if isinstance(value, dict):
        return {str(room): float(fraction) for room, fraction in value.items()}
    # Historic surveys use e.g. 'bedroom, landing' or 'hall (side lobby)'.
    rooms = list(
        dict.fromkeys(
            re.sub(r"\s*\([^)]*\)", "", room).strip()
            for room in str(value).split(",")
            if room.strip()
        )
    )
    if len(rooms) > 1:
        warnings.append(f"{ctx}: equal adjacent area shares assumed; survey fractions when known")
    return {room: 1 / len(rooms) for room in rooms}


@dataclass(frozen=True)
class Construction:
    key: str
    u_value: float
    confidence: str = "unknown"
    description: str | None = None


@dataclass(frozen=True)
class House:
    constructions: dict[str, Construction]
    face_bearings: dict[str, float]
    front_bearing_deg: float | None
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    def bearing(self, face: str) -> float | None:
        key = FACE_ALIASES.get(str(face).lower(), str(face).upper())
        return self.face_bearings.get(key)

    def u_value(self, key: str | None) -> float | None:
        if key is None:
            return None
        c = self.constructions.get(key)
        return c.u_value if c else None


@dataclass(frozen=True)
class RoomGeometry:
    room_id: str
    name: str
    floor_area_m2: float | None
    surfaces: list[Surface]
    emitters: list[Emitter]
    preferred_air_temperature_entity: str | None
    zone_id: str | None
    climate_primary: str | None
    climate_backup: str | None
    window_contacts: list[str]
    adjacent_door_contacts: list[str]
    asymmetry_enabled: bool
    warnings: list[str]
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def total_area_m2(self) -> float:
        return sum(s.area_m2 for s in self.surfaces)

    @property
    def glazed_area_m2(self) -> float:
        return sum(s.area_m2 for s in self.surfaces if s.glazed)


def load_house(path: str | Path) -> House:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    cons: dict[str, Construction] = {}
    for key, c in (data.get("constructions") or {}).items():
        if not isinstance(c, dict) or c.get("u_value") is None:
            continue
        cons[key] = Construction(
            key, float(c["u_value"]), str(c.get("confidence", "unknown")), c.get("description")
        )
    house = data.get("house") or {}
    bearings = {
        k: float(v) for k, v in (house.get("face_bearings_deg") or DEFAULT_BEARINGS).items()
    }
    # Fill any compass points not given by rotating the default grid.
    if "N" in bearings:
        rot = bearings["N"]
        for k, v in DEFAULT_BEARINGS.items():
            bearings.setdefault(k, (v + rot) % 360)
    fb = house.get("front_elevation_bearing_deg")
    return House(cons, bearings, float(fb) if fb is not None else None, data)


def _boundary(value: str | None, warnings: list[str], ctx: str) -> Boundary | None:
    if value is None:
        warnings.append(f"{ctx}: boundary missing")
        return None
    try:
        return Boundary(str(value))
    except ValueError:
        warnings.append(f"{ctx}: unknown boundary '{value}'")
        return None


def _height(room: dict[str, Any]) -> float | None:
    g = room.get("geometry") or {}
    return g.get("height_m") or g.get("height_assumed_m")


def load_room(path: str | Path, house: House) -> RoomGeometry:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8")) or {}
    warnings: list[str] = []
    info = data.get("room") or {}
    rid = str(info.get("id") or Path(path).stem)
    name = str(info.get("name") or rid)
    geometry = data.get("geometry") or {}
    floor_area = geometry.get("floor_area_m2")

    # Openings first, so wall areas can be netted.
    openings = ((data.get("openings") or {}).get("items")) or []
    opening_area_by_face: dict[str, float] = {}
    surfaces: list[Surface] = []
    for o in openings:
        oid = str(o.get("id", "opening"))
        face = _face(o.get("face", ""))
        area = o.get("area_m2")
        if area is None and o.get("width_m") and o.get("height_m"):
            area = float(o["width_m"]) * float(o["height_m"])
        if area is None:
            warnings.append(f"{rid}/{oid}: no area, skipped")
            continue
        u = house.u_value(o.get("construction"))
        if u is None:
            warnings.append(f"{rid}/{oid}: unknown construction '{o.get('construction')}', skipped")
            continue
        # A night-time habit says nothing about daytime shading. An explicit
        # shade_factor is a fixed surveyed obstruction/transmission multiplier.
        tilt = float(
            o.get(
                "tilt_deg",
                0.0 if face in ("ROOF", "CEILING") or o.get("type") == "rooflight" else 90.0,
            )
        )
        if tilt != 0 and house.bearing(face) is None:
            warnings.append(f"{rid}/{oid}: tilted opening bearing unknown; direct solar ignored")
        surfaces.append(
            Surface(
                name=oid,
                area_m2=float(area),
                u_value=u,
                boundary=Boundary.OUTSIDE,
                bearing_deg=house.bearing(face),
                tilt_deg=tilt,
                glazed=str(o.get("type")) in GLAZED_TYPES,
                g_value=float(o.get("g_value", 0.6)),
                shade_factor=float(o.get("shade_factor", 1.0)),
            )
        )
        opening_area_by_face[face] = opening_area_by_face.get(face, 0.0) + float(area)

    faces = ((data.get("boundaries") or {}).get("faces")) or []
    for i, f in enumerate(faces):
        face = _face(f.get("face", ""))
        ctx = f"{rid}/face[{i}]:{face}"
        b = _boundary(f.get("boundary"), warnings, ctx)
        if b is None:
            continue
        gross = f.get("gross_area_m2")
        if gross is None:
            warnings.append(f"{ctx}: gross_area_m2 missing, skipped")
            continue
        net = (
            float(gross) - opening_area_by_face.pop(face, 0.0)
            if b is not Boundary.HEATED_ROOM
            else float(gross)
        )
        if net <= 0:
            warnings.append(f"{ctx}: openings exceed wall area, skipped")
            continue
        if b is Boundary.HEATED_ROOM:
            key = f.get("construction") or (
                "internal_floor" if face in ("FLOOR", "CEILING") else "internal_wall"
            )
            u = house.u_value(key)
            if u is None:
                if f.get("construction"):
                    raise ValueError(f"{ctx}: unknown construction '{key}'")
                u = 1.5  # conservative unsurveyed partition estimate, not an insulator
                warnings.append(f"{ctx}: internal U-value assumed 1.5 W/m²K")
        else:
            u = house.u_value(f.get("construction"))
            if u is None:
                warnings.append(
                    f"{ctx}: unknown construction '{f.get('construction')}' for a cold-facing surface"
                )
                continue
        tilt = 0.0 if face in ("FLOOR", "CEILING", "ROOF") else 90.0
        adjacent_fractions = _adjacencies(f.get("adjacent"), warnings, ctx)
        adjacent_id = next(iter(adjacent_fractions)) if len(adjacent_fractions) == 1 else None
        surfaces.append(
            Surface(
                name=f"{FACE_NAMES.get(face, face.lower())}_{b.value}_{i}",
                area_m2=net,
                u_value=u,
                boundary=b,
                bearing_deg=None if tilt == 0.0 else house.bearing(face),
                tilt_deg=tilt,
                adjacent=adjacent_id,
                adjacent_fractions=adjacent_fractions,
            )
        )
    for face, leftover in opening_area_by_face.items():
        warnings.append(
            f"{rid}: {leftover:.2f} m² of openings on face '{face}' with no matching wall"
        )

    heating = data.get("heating") or {}
    emitters: list[Emitter] = []
    for e in heating.get("emitters") or []:
        out = e.get("output_dt50_w")
        if out is None:
            warnings.append(f"{rid}/emitter {e.get('plan_number', '?')}: no output_dt50_w")
            continue
        emitters.append(
            Emitter(name=f"rad_{e.get('plan_number', len(emitters) + 1)}", output_dt50_w=float(out))
        )

    sensors = data.get("sensors") or {}
    contacts = [c for c in (sensors.get("contacts") or []) if not str(c).endswith("_window_open")]
    window_contacts: list[str] = []
    for o in openings:
        cs = o.get("contact_sensor")
        if isinstance(cs, list):
            window_contacts.extend(str(x) for x in cs)
        elif cs:
            window_contacts.append(str(cs))
    door_contacts = [c for c in contacts if c not in window_contacts]

    if floor_area is None:
        warnings.append(f"{rid}: floor_area_m2 missing")
    if not any(s.boundary is not Boundary.HEATED_ROOM for s in surfaces):
        warnings.append(f"{rid}: no external boundaries; correction depends on neighbouring rooms")

    return RoomGeometry(
        room_id=rid,
        name=name,
        floor_area_m2=float(floor_area) if floor_area is not None else None,
        surfaces=surfaces,
        emitters=emitters,
        preferred_air_temperature_entity=heating.get("preferred_air_temperature_entity"),
        zone_id=heating.get("zone_id"),
        climate_primary=heating.get("climate_primary"),
        climate_backup=heating.get("climate_backup"),
        window_contacts=window_contacts,
        adjacent_door_contacts=door_contacts,
        asymmetry_enabled=bool(((data.get("model") or {}).get("asymmetry_enabled")) or False),
        warnings=warnings,
        raw=data,
    )


def load_all_rooms(house_dir: str | Path) -> tuple[House, dict[str, RoomGeometry]]:
    """Load house.yaml and every rooms/*.yaml except the template."""
    house_dir = Path(house_dir)
    house = load_house(house_dir / "house.yaml")
    rooms: dict[str, RoomGeometry] = {}
    for p in sorted((house_dir / "rooms").glob("*.yaml")):
        if p.name.startswith("_"):
            continue
        r = load_room(p, house)
        rooms[r.room_id] = r
    return house, rooms
