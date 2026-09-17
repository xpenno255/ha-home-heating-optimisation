"""core.geometry against synthetic survey fixtures."""

from pathlib import Path

import pytest

from custom_components.home_heating_optimisation.control.comfort.core.geometry import (
    load_all_rooms,
    load_house,
    load_room,
)
from custom_components.home_heating_optimisation.control.comfort.core.model import (
    Boundary,
    Environment,
    ModelParams,
    operative_temperature,
    required_air_temperature,
    steady_state_mrt,
)

FIXTURES = Path(__file__).parent / "fixtures"
HOUSE_DIR = FIXTURES


def test_house_constructions_and_bearings():
    h = load_house(FIXTURES / "house.yaml")
    assert h.u_value("external_wall_main") == pytest.approx(1.5)
    assert h.u_value("nonsense") is None
    assert h.bearing("north") == pytest.approx(15.0)
    assert h.bearing("S") == pytest.approx(195.0)
    assert h.front_bearing_deg == pytest.approx(195.0)


def test_living_room_surfaces():
    h = load_house(FIXTURES / "house.yaml")
    r = load_room(FIXTURES / "rooms" / "living_room.yaml", h)
    assert r.room_id == "living_room"
    names = {s.name for s in r.surfaces}
    assert "bay_french_doors_north" in names and "window_south" in names
    # Glazing is netted off its wall without double-counting the opening area.
    north_wall = next(s for s in r.surfaces if s.name.startswith("north_outside"))
    bay = next(s for s in r.surfaces if s.name == "bay_french_doors_north")
    assert bay.area_m2 == pytest.approx(4, abs=0.01)
    assert north_wall.area_m2 == pytest.approx(12 - 4, abs=0.05)
    assert r.glazed_area_m2 == pytest.approx(4 + 2, abs=0.05)
    assert any(s.boundary is Boundary.ROOF for s in r.surfaces)  # the bay's own roof
    assert 100 < r.total_area_m2 < 110
    assert r.zone_id == "01:000001_01"
    assert r.preferred_air_temperature_entity == "sensor.example_living_air"
    assert len(r.emitters) == 2 and sum(e.output_dt50_w for e in r.emitters) == 3500
    floor = next(s for s in r.surfaces if s.boundary is Boundary.GROUND)
    assert floor.tilt_deg == 0.0 and floor.bearing_deg is None
    assert not [w for w in r.warnings if "skipped" in w], r.warnings


def test_loaded_living_room_inverse_reproduces_target():
    h = load_house(FIXTURES / "house.yaml")
    r = load_room(FIXTURES / "rooms" / "living_room.yaml", h)
    env = Environment(t_out=0.0, sun_elevation_deg=-10.0, cloud_fraction=1.0)
    c = required_air_temperature(
        r.surfaces, env, 20.0, ModelParams(trust_k=1.0, step=0.01, cap_up=5)
    )
    assert c.offset_physical > 0
    mrt = steady_state_mrt(r.surfaces, env, c.t_air_required).mrt
    assert operative_temperature(c.t_air_required, mrt) == pytest.approx(20.0, abs=1e-9)


def test_utility_has_roof_and_garage_surfaces():
    h = load_house(FIXTURES / "house.yaml")
    r = load_room(FIXTURES / "rooms" / "utility.yaml", h)
    kinds = {s.boundary for s in r.surfaces}
    assert Boundary.ROOF in kinds and Boundary.UNHEATED_SPACE in kinds
    garage = next(s for s in r.surfaces if s.boundary is Boundary.UNHEATED_SPACE)
    assert garage.adjacent == "garage"
    # 1980s walls at U 0.6 offset the roof and garage: per unit area the utility
    # ends up close to the living room, not worse. Just require a real deficit.
    env = Environment(t_out=0.0, sun_elevation_deg=-10.0, cloud_fraction=1.0)
    p = ModelParams(trust_k=1.0, step=0.01, cap_up=5)
    c = required_air_temperature(r.surfaces, env, 18.0, p)
    assert c.offset_physical > 0
    mrt = steady_state_mrt(r.surfaces, env, c.t_air_required, p).mrt
    assert operative_temperature(c.t_air_required, mrt) == pytest.approx(18.0, abs=1e-9)
    assert not [w for w in r.warnings if "skipped" in w], r.warnings


def test_window_contacts_split_from_door_contacts(tmp_path):
    h = load_house(FIXTURES / "house.yaml")
    (tmp_path / "rooms").mkdir()
    (tmp_path / "rooms" / "k.yaml").write_text(
        """
room: {id: k, name: K}
geometry: {floor_area_m2: 10, height_assumed_m: 2.4}
boundaries:
  faces:
    - {face: north, boundary: outside, construction: external_wall_main, gross_area_m2: 12}
    - {face: floor, boundary: ground, construction: ground_floor_suspended, gross_area_m2: 10}
openings:
  items:
    - {id: bifold, type: glazed_door, face: north, area_m2: 4.6, construction: glazed_door_alu_dg,
       contact_sensor: [binary_sensor.bifold_a, binary_sensor.bifold_b]}
heating:
  emitters: [{plan_number: 3, type: radiator, output_dt50_w: 1000}]
sensors:
  contacts: [binary_sensor.bifold_a, binary_sensor.bifold_b, binary_sensor.kitchen_door, binary_sensor.01_x_window_open]
"""
    )
    r = load_room(tmp_path / "rooms" / "k.yaml", h)
    assert r.window_contacts == ["binary_sensor.bifold_a", "binary_sensor.bifold_b"]
    assert r.adjacent_door_contacts == ["binary_sensor.kitchen_door"]
    assert r.warnings == []


def test_bad_input_is_reported_not_fatal(tmp_path):
    h = load_house(FIXTURES / "house.yaml")
    (tmp_path / "rooms").mkdir()
    (tmp_path / "rooms" / "bad.yaml").write_text(
        """
room: {id: bad, name: Bad}
boundaries:
  faces:
    - {face: north, boundary: outside, construction: not_a_construction, gross_area_m2: 12}
    - {face: east, boundary: outside, construction: external_wall_main}
    - {face: floor, boundary: heated_room, gross_area_m2: 10}
openings:
  items:
    - {id: w, type: window, face: north}
"""
    )
    r = load_room(tmp_path / "rooms" / "bad.yaml", h)
    assert len(r.surfaces) == 1
    assert any("unknown construction" in w for w in r.warnings)
    assert any("gross_area_m2 missing" in w for w in r.warnings)
    assert any("no area" in w for w in r.warnings)
    assert any("floor_area_m2 missing" in w for w in r.warnings)


def test_shipped_house_loads_every_room_without_skips():
    house, rooms = load_all_rooms(HOUSE_DIR)
    assert set(rooms) == {"living_room", "utility"}
    for r in rooms.values():
        skipped = [w for w in r.warnings if "skipped" in w or "unknown construction" in w]
        assert not skipped, (r.room_id, skipped)
        assert r.total_area_m2 > 0


def test_loaded_partition_uses_construction_and_all_neighbours(tmp_path):
    path = tmp_path / "room.yaml"
    path.write_text("""
room: {id: room}
geometry: {floor_area_m2: 10}
boundaries:
  faces:
    - face: east
      boundary: heated_room
      construction: wall_to_unheated
      gross_area_m2: 10
      adjacent: {kitchen: 0.25, hall: 0.75}
""")
    room = load_room(path, load_house(FIXTURES / "house.yaml"))
    surface = room.surfaces[0]
    assert surface.u_value == 1.5
    assert surface.adjacent_fractions == {"kitchen": 0.25, "hall": 0.75}
    warm = Environment(t_out=0, adjacent_temps={"kitchen": 20, "hall": 20})
    cold_hall = Environment(t_out=0, adjacent_temps={"kitchen": 20, "hall": 10})
    assert required_air_temperature(room.surfaces, warm, 20).offset_physical == pytest.approx(0)
    assert required_air_temperature(room.surfaces, cold_hall, 20).offset_physical > 0


def test_opening_orientation_shading_and_face_aliases(tmp_path):
    path = tmp_path / "room.yaml"
    path.write_text("""
room: {id: room}
geometry: {floor_area_m2: 10}
boundaries:
  faces:
    - {face: south, boundary: outside, construction: external_wall_main, gross_area_m2: 10}
    - {face: roof, boundary: roof, construction: roof_extension, gross_area_m2: 10}
openings:
  items:
    - {id: window, type: window, face: S, area_m2: 2, construction: window_timber_dg,
       covering: curtains, covering_closed_at_night: true}
    - {id: skylight, type: rooflight, face: roof, area_m2: 1, construction: window_timber_dg}
""")
    room = load_room(path, load_house(FIXTURES / "house.yaml"))
    surfaces = {s.name: s for s in room.surfaces}
    assert surfaces["window"].shade_factor == 1  # night habit must not shade daytime
    assert surfaces["skylight"].tilt_deg == 0
    assert surfaces["south_outside_0"].area_m2 == 8
    assert surfaces["roof_roof_1"].area_m2 == 9
    assert room.total_area_m2 == 20
