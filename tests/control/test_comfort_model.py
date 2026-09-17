"""Physical invariants and analytic limits for the steady-state enclosure model."""

import math
from dataclasses import replace

import pytest

from custom_components.home_heating_optimisation.control.comfort.core.model import (
    Boundary,
    Emitter,
    Environment,
    ModelParams,
    Surface,
    clear_sky_ghi,
    effective_u,
    irradiance_on_surface,
    operative_temperature,
    other_side_temperature,
    radiator_output_w,
    required_air_temperature,
    round_to_step,
    running_mean_outdoor,
    solar_components,
    steady_state_mrt,
)


def living_room_surfaces() -> list[Surface]:
    """Design note §3 worked example, 2.4 m ceilings, bearings N=0 for simplicity."""
    return [
        Surface("w_wall", 18.0, 1.7, Boundary.OUTSIDE, bearing_deg=270),
        Surface("n_bay_glass", 8.3, 1.5, Boundary.OUTSIDE, bearing_deg=0, glazed=True),
        Surface("n_wall", 1.6, 1.7, Boundary.OUTSIDE, bearing_deg=0),
        Surface("s_wall", 6.3, 1.7, Boundary.OUTSIDE, bearing_deg=180),
        Surface("s_window", 2.78, 1.4, Boundary.OUTSIDE, bearing_deg=180, glazed=True),
        Surface("floor", 30.1, 0.8, Boundary.GROUND, tilt_deg=0.0),
        Surface("e_wall", 18.0, 0.0, Boundary.HEATED_ROOM, adjacent="kitchen"),
        Surface("ceiling", 30.1, 0.0, Boundary.HEATED_ROOM, tilt_deg=0.0, adjacent="bedroom"),
    ]


FULL_TRUST = ModelParams(trust_k=1.0, cap_up=5.0, cap_down=5.0, step=0.01)
NIGHT = dict(sun_elevation_deg=-10.0, cloud_fraction=1.0)


@pytest.mark.parametrize("t_out", [-5.0, 0.0, 10.0])
def test_uniform_enclosure_matches_series_resistance(t_out):
    # Equal surfaces at equal temperatures exchange no NET radiation. Heating
    # reaches them by convection alone, then crosses the fabric to outdoors.
    surfaces = [Surface("enclosure", 100.0, 1.0, Boundary.OUTSIDE)]
    # Choose wind that reproduces reference external film resistance 0.04.
    env = Environment(t_out=t_out, wind_ms=(25 - 10.4) / 3.8, **NIGHT)
    h_c = FULL_TRUST.h_i - FULL_TRUST.h_r
    r_surface_to_outside = 1.0 - 1 / 7.7
    conductance = 1 / r_surface_to_outside
    # From 20 = (Ta + Ts)/2 and hc*(Ta-Ts) = K*(Ts-To).
    expected_air = (40 * (h_c + conductance) - conductance * t_out) / (2 * h_c + conductance)
    c = required_air_temperature(surfaces, env, 20.0, FULL_TRUST)
    assert c.t_air_required == pytest.approx(expected_air)
    surface = steady_state_mrt(surfaces, env, c.t_air_required, FULL_TRUST).surface_temps[
        "enclosure"
    ]
    assert h_c * (c.t_air_required - surface) == pytest.approx(conductance * (surface - t_out))


def test_setpoint_reproduces_target_ot():
    """At the unrounded required air temperature, OT equals the target exactly."""
    env = Environment(t_out=0.0, **NIGHT)
    c = required_air_temperature(living_room_surfaces(), env, 20.0, FULL_TRUST)
    mrt = steady_state_mrt(living_room_surfaces(), env, c.t_air_required).mrt
    assert operative_temperature(c.t_air_required, mrt) == pytest.approx(20.0, abs=1e-6)


def test_offset_monotonic_in_outdoor_temperature():
    prev = None
    for t_out in range(-10, 21, 2):
        c = required_air_temperature(
            living_room_surfaces(), Environment(t_out=t_out, **NIGHT), 20.0, FULL_TRUST
        )
        if prev is not None:
            assert c.offset_physical <= prev + 1e-9
        prev = c.offset_physical


def test_mild_weather_gives_negative_offset():
    """Warm outside and warm ground: the room feels warmer than its air, so air can drop."""
    env = Environment(t_out=22.0, t_ground=22.0, **NIGHT)
    c = required_air_temperature(living_room_surfaces(), env, 20.0, FULL_TRUST)
    assert c.offset_physical < 0


def test_interior_room_has_zero_offset():
    surfaces = [
        Surface("a", 10.0, 0.0, Boundary.HEATED_ROOM),
        Surface("b", 10.0, 0.0, Boundary.HEATED_ROOM),
        Surface("floor", 12.0, 0.0, Boundary.HEATED_ROOM, tilt_deg=0.0),
    ]
    c = required_air_temperature(surfaces, Environment(t_out=-5.0, **NIGHT), 19.0, FULL_TRUST)
    assert c.offset_physical == pytest.approx(0.0)
    assert c.air_setpoint == pytest.approx(19.0)


def test_heated_neighbour_colder_than_room_adds_offset():
    surfaces = [
        Surface("party", 10.0, 1.5, Boundary.HEATED_ROOM, adjacent="hall"),
        Surface("floor", 10.0, 0.0, Boundary.HEATED_ROOM, tilt_deg=0.0),
    ]
    warm = Environment(t_out=5.0, adjacent_temps={"hall": 20.0}, **NIGHT)
    cold = Environment(t_out=5.0, adjacent_temps={"hall": 15.0}, **NIGHT)
    assert (
        required_air_temperature(surfaces, cold, 20.0, FULL_TRUST).offset_physical
        > required_air_temperature(surfaces, warm, 20.0, FULL_TRUST).offset_physical
    )


def test_trust_and_caps_and_rounding():
    env = Environment(t_out=-15.0, **NIGHT)
    p = ModelParams(trust_k=0.8, cap_up=1.0, cap_down=1.5, step=0.5)
    c = required_air_temperature(living_room_surfaces(), env, 20.0, p)
    assert c.offset_trusted == pytest.approx(0.8 * c.offset_physical)
    assert c.capped
    assert c.offset_final == 1.0
    assert c.air_setpoint == 21.0


def test_wind_increases_offset():
    calm = Environment(t_out=0.0, wind_ms=0.0, **NIGHT)
    windy = Environment(t_out=0.0, wind_ms=10.0, **NIGHT)
    s = living_room_surfaces()
    assert (
        required_air_temperature(s, windy, 20.0, FULL_TRUST).offset_physical
        > required_air_temperature(s, calm, 20.0, FULL_TRUST).offset_physical
    )


def test_solar_reduces_offset_on_sunlit_face_only():
    s = living_room_surfaces()
    p = replace(FULL_TRUST, solar_cap_k=20.0)
    night = Environment(t_out=5.0, **NIGHT)
    # Low winter sun due south at noon, clear sky, measured GHI.
    sunny_south = Environment(
        t_out=5.0, ghi_wm2=300.0, sun_elevation_deg=20.0, sun_azimuth_deg=180.0
    )
    sunny_north = Environment(t_out=5.0, ghi_wm2=300.0, sun_elevation_deg=20.0, sun_azimuth_deg=0.0)
    off_night = required_air_temperature(s, night, 20.0, p).offset_physical
    off_south = required_air_temperature(s, sunny_south, 20.0, p).offset_physical
    off_north = required_air_temperature(s, sunny_north, 20.0, p).offset_physical
    assert off_south < off_night
    # The N bay is three times the S window, so sun from the north helps more.
    assert off_north < off_south
    assert steady_state_mrt(s, sunny_south, 20.0).solar_k > 0


def test_solar_is_capped():
    s = living_room_surfaces()
    blazing = Environment(t_out=5.0, ghi_wm2=1000.0, sun_elevation_deg=45.0, sun_azimuth_deg=0.0)
    p = ModelParams(solar_cap_k=0.7)
    assert steady_state_mrt(s, blazing, 20.0, p).solar_k == pytest.approx(0.7)


def test_asymmetry_term_positive_and_capped():
    env = Environment(t_out=-5.0, **NIGHT)
    p = ModelParams(trust_k=1.0, asymmetry_a=0.5, asymmetry_cap=0.3, cap_up=5.0, step=0.01)
    c = required_air_temperature(living_room_surfaces(), env, 20.0, p)
    assert 0 < c.offset_asymmetry <= 0.3


def test_clear_sky_shape():
    assert clear_sky_ghi(-5) == 0.0
    assert 0 < clear_sky_ghi(10) < clear_sky_ghi(30) < clear_sky_ghi(60) < 1100


def test_round_to_step():
    assert round_to_step(20.24, 0.5) == 20.0
    assert round_to_step(20.25, 0.5) == 20.5
    assert round_to_step(20.74, 0.5) == 20.5
    assert round_to_step(20.3, 0.1) == pytest.approx(20.3)


def test_radiator_output_derates_with_flow_temperature():
    rads = [Emitter("k3_1400", 3486), Emitter("k3_1600", 3984)]
    at_70 = radiator_output_w(rads, 70.0, 20.0)  # mean water 65, ΔT 45
    at_50 = radiator_output_w(rads, 50.0, 20.0)  # mean water 45, ΔT 25
    assert at_70 == pytest.approx(7470 * (45 / 50) ** 1.3, rel=1e-6)
    assert at_50 / 7470 == pytest.approx(0.406, abs=0.005)
    assert radiator_output_w(rads, 24.0, 20.0) == 0.0


def test_running_mean():
    assert running_mean_outdoor(None, 12.0) == 12.0
    assert running_mean_outdoor(10.0, 0.0) == pytest.approx(8.0)
    t = 10.0
    for _ in range(14):
        t = running_mean_outdoor(t, 0.0)
    assert t < 0.5  # a two-week cold spell converges on the new level


def test_leakiness_guard():
    absurd = [Surface("hole", 10.0, 200.0, Boundary.OUTSIDE)]
    with pytest.raises(ValueError):
        required_air_temperature(absurd, Environment(t_out=0.0, **NIGHT), 20.0, FULL_TRUST)


@pytest.mark.parametrize("solar", [0.0, 100.0])
def test_each_surface_and_whole_enclosure_conserve_energy(solar):
    surfaces = living_room_surfaces() + [Surface("garage", 8, 1.5, Boundary.UNHEATED_SPACE)]
    env = Environment(t_out=2, ghi_wm2=solar, sun_elevation_deg=30, wind_ms=3)
    p = replace(FULL_TRUST, solar_cap_k=20)
    air = 21.0
    result = steady_state_mrt(surfaces, env, air, p)
    area = sum(s.area_m2 for s in surfaces)
    power = sum(
        s.area_m2 * s.g_value * s.shade_factor * irradiance_on_surface(s, env)
        for s in surfaces
        if s.glazed
    )
    convective = conductive = radiation = 0.0
    for s in surfaces:
        ts = result.surface_temps[s.name]
        u = effective_u(s, env, p)
        k = 1 / (1 / u - 1 / 7.7) if u else 0
        q_conv = (p.h_i - p.h_r) * (air - ts)
        q_cond = k * (other_side_temperature(s, env, air, p) - ts)
        q_rad = p.h_r * (result.mrt - ts)
        assert q_conv + q_cond + q_rad + power / area == pytest.approx(0, abs=1e-10)
        convective += s.area_m2 * q_conv
        conductive += s.area_m2 * q_cond
        radiation += s.area_m2 * q_rad
    assert radiation == pytest.approx(0, abs=1e-9)
    assert convective + conductive + power == pytest.approx(0, abs=1e-9)
    assert result.mrt == pytest.approx(
        sum(s.area_m2 * result.surface_temps[s.name] for s in surfaces) / area
    )


@pytest.mark.parametrize("fraction", [0, 0.5, 1])
@pytest.mark.parametrize("solar", [0, 100])
def test_inverse_handles_air_dependent_boundaries_and_partial_neighbours(fraction, solar):
    surfaces = [
        Surface("garage", 12, 1.5, Boundary.UNHEATED_SPACE),
        Surface(
            "partition",
            10,
            1.5,
            Boundary.HEATED_ROOM,
            adjacent_fractions={"known": 0.25, "unknown": 0.75},
        ),
    ]
    env = Environment(t_out=-5, adjacent_temps={"known": 16}, ghi_wm2=solar, sun_elevation_deg=30)
    surfaces.append(Surface("glass", 3, 1.4, Boundary.OUTSIDE, glazed=True, bearing_deg=180))
    p = replace(FULL_TRUST, unheated_fraction=fraction)
    c = required_air_temperature(surfaces, env, 20, p)
    mrt = steady_state_mrt(surfaces, env, c.t_air_required, p).mrt
    assert operative_temperature(c.t_air_required, mrt) == pytest.approx(20, abs=1e-10)


def test_adiabatic_solar_room_loses_heat_only_by_convection():
    surfaces = [Surface("glass", 10, 0, Boundary.OUTSIDE, glazed=True, tilt_deg=0)]
    env = Environment(t_out=20, ghi_wm2=10, sun_elevation_deg=45)
    p = replace(FULL_TRUST, solar_cap_k=20)
    result = steady_state_mrt(surfaces, env, 20, p)
    assert result.solar_k == pytest.approx(10 * 0.6 / (p.h_i - p.h_r))
    assert result.mrt == pytest.approx(20 + result.solar_k)


@pytest.mark.parametrize("elevation", [0.1, 2.9, 3, 10, 45, 90])
def test_solar_decomposition_and_horizontal_projection_preserve_ghi(elevation):
    env = Environment(t_out=10, ghi_wm2=100, sun_elevation_deg=elevation, day_of_year=15)
    dni, dhi = solar_components(env)
    assert min(dni, dhi) >= 0
    assert dhi + dni * math.sin(math.radians(elevation)) == pytest.approx(100)
    rooflight = Surface("rooflight", 1, 1.4, Boundary.OUTSIDE, tilt_deg=0, glazed=True)
    assert irradiance_on_surface(rooflight, env) == pytest.approx(100)


def test_overcast_is_mostly_diffuse_and_cloud_guess_does_not_reduce_heating():
    env = Environment(t_out=0, ghi_wm2=50, sun_elevation_deg=30, day_of_year=15)
    _, dhi = solar_components(env)
    assert dhi / 50 > 0.98
    surfaces = living_room_surfaces()
    guessed = replace(env, ghi_wm2=None, cloud_fraction=0)
    night = replace(env, ghi_wm2=0)
    assert (
        required_air_temperature(surfaces, guessed, 20).air_setpoint
        == required_air_temperature(surfaces, night, 20).air_setpoint
    )


def test_wind_replaces_only_external_resistance_and_has_a_finite_limit():
    p = ModelParams()
    wall = Surface("wall", 10, 1.7, Boundary.OUTSIDE)
    nominal = Environment(t_out=0, wind_ms=(25 - 10.4) / 3.8)
    assert effective_u(wall, nominal, p) == pytest.approx(1.7)
    windy = Environment(t_out=0, wind_ms=1e6)
    assert 1.7 < effective_u(wall, windy, p) < 1 / (1 / 1.7 - 0.04)
    floor = replace(wall, boundary=Boundary.GROUND)
    assert effective_u(floor, windy, p) == 1.7


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1])
def test_invalid_surface_values_cannot_produce_a_command(bad):
    with pytest.raises(ValueError):
        required_air_temperature(
            [Surface("bad", 10, bad, Boundary.OUTSIDE)], Environment(t_out=0), 20
        )


def test_splitting_a_surface_does_not_change_energy_or_comfort():
    surfaces = living_room_surfaces()
    wall = surfaces.pop(0)
    split = surfaces + [
        replace(wall, name="one", area_m2=wall.area_m2 * 0.3),
        replace(wall, name="two", area_m2=wall.area_m2 * 0.7),
    ]
    env = Environment(t_out=0, ghi_wm2=100, sun_elevation_deg=30)
    assert required_air_temperature(split, env, 20).t_air_required == pytest.approx(
        required_air_temperature(surfaces + [wall], env, 20).t_air_required
    )
