"""Steady-state operative-temperature model (docs/physics-model.md).

All functions are pure. Temperatures in °C, areas m², U-values W/m²K,
irradiance W/m², wind m/s, angles degrees.

Sign convention: an *offset* is (air setpoint − OT target). Positive means
the air must run warmer than the schedule to feel like the schedule.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from enum import Enum
from typing import Mapping

H_I_DEFAULT = 7.7  # reference inside film included in survey U-values, W/m²K
GROUND_TEMP_DEFAULT = 10.0


class Boundary(str, Enum):
    """What is on the far side of a surface."""

    OUTSIDE = "outside"
    GROUND = "ground"
    HEATED_ROOM = "heated_room"
    UNHEATED_SPACE = "unheated_space"
    LOFT = "loft"
    ROOF = "roof"


@dataclass(frozen=True)
class Surface:
    """One inside surface of the room."""

    name: str
    area_m2: float
    u_value: float
    boundary: Boundary
    bearing_deg: float | None = None  # outward normal; None for floor/ceiling/internal
    tilt_deg: float = 90.0  # 90 = vertical wall, 0 = horizontal
    glazed: bool = False
    g_value: float = 0.6  # solar transmittance of glazing
    shade_factor: float = 1.0  # 1.0 open, 0.0 fully covered
    adjacent: str | None = None  # room id for HEATED_ROOM / UNHEATED_SPACE
    adjacent_fractions: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Emitter:
    """A radiator or similar, rated at ΔT50."""

    name: str
    output_dt50_w: float
    exponent: float = 1.3


@dataclass(frozen=True)
class Environment:
    """Weather, sun and neighbouring-room state for one evaluation."""

    t_out: float
    wind_ms: float = 0.0
    ghi_wm2: float | None = None  # global horizontal irradiance if measured
    cloud_fraction: float | None = None  # 0..1, used only when ghi is None
    sun_elevation_deg: float = 0.0
    sun_azimuth_deg: float = 180.0
    t_ground: float = GROUND_TEMP_DEFAULT
    adjacent_temps: Mapping[str, float] = field(default_factory=dict)
    day_of_year: int = 172  # coordinator supplies the actual date for solar decomposition


@dataclass(frozen=True)
class ModelParams:
    """Tunables. Defaults follow the design note."""

    h_i: float = H_I_DEFAULT  # operating convection + linearised radiation
    h_r: float = 4.7  # surface radiation to the mean enclosure node; h_c = h_i - h_r
    outside_resistance: float = 0.04  # reference outside film included in survey U-values
    trust_k: float = 0.8  # fraction of the physical offset applied
    cap_up: float = 1.5
    cap_down: float = 1.5
    step: float = 0.1  # thermostat resolution
    asymmetry_a: float = 0.0  # optional empirical comfort bias, NOT a view-factor model
    asymmetry_cap: float = 1.0
    loft_delta: float = 1.0  # loft air = t_out + loft_delta
    unheated_fraction: float = 0.5  # unheated space sits this far from t_out towards t_air
    solar_cap_k: float = 2.0  # cap on the MRT rise from solar gain


@dataclass(frozen=True)
class MRTBreakdown:
    """Steady-state MRT and how it was made."""

    mrt: float
    t_air: float
    solar_k: float  # MRT rise from transmitted solar, already included in mrt
    total_area_m2: float
    surface_temps: Mapping[str, float]


@dataclass(frozen=True)
class Correction:
    """Result of `required_air_temperature`."""

    ot_target: float
    t_air_required: float  # physical answer before trust/caps
    offset_physical: float
    offset_trusted: float
    offset_asymmetry: float
    offset_final: float  # after trust, asymmetry and caps, before rounding
    air_setpoint: float  # ot_target + offset_final, rounded to step
    capped: bool
    mrt_at_setpoint: float
    solar_k: float
    sum_l: float  # 1 - d(MRT)/d(T_air), effective dimensionless leakiness


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def round_to_step(value: float, step: float) -> float:
    """Round to the nearest multiple of `step` (0.5 → thermostat resolution)."""
    if step <= 0:
        return value
    return round(math.floor(value / step + 0.5) * step, 6)


def clear_sky_ghi(sun_elevation_deg: float) -> float:
    """Haurwitz clear-sky global horizontal irradiance, W/m²."""
    if sun_elevation_deg <= 0:
        return 0.0
    s = math.sin(math.radians(sun_elevation_deg))
    return 1098.0 * s * math.exp(-0.057 / s)


def estimate_ghi(env: Environment) -> float:
    """Measured GHI if available, else clear-sky scaled by cloud cover."""
    if env.ghi_wm2 is not None:
        return max(0.0, env.ghi_wm2)
    cloud = 0.5 if env.cloud_fraction is None else min(1.0, max(0.0, env.cloud_fraction))
    return clear_sky_ghi(env.sun_elevation_deg) * (1.0 - 0.75 * cloud)


def solar_components(env: Environment) -> tuple[float, float]:
    """Return (DNI, DHI), W/m², using the Erbs diffuse-fraction correlation.

    GHI = DHI + DNI*sin(elevation). At <3° elevation use all diffuse to
    avoid a singular projection. Cloud estimates are for diagnostics only.
    """
    ghi = estimate_ghi(env)
    if ghi <= 0 or env.sun_elevation_deg <= 0:
        return 0.0, 0.0
    sin_e = math.sin(math.radians(env.sun_elevation_deg))
    if env.sun_elevation_deg < 3:
        return 0.0, ghi
    extraterrestrial = 1367.0 * (1 + 0.033 * math.cos(2 * math.pi * env.day_of_year / 365))
    kt = min(1.0, ghi / (extraterrestrial * sin_e))
    if kt <= 0.22:
        diffuse_fraction = 1 - 0.09 * kt
    elif kt <= 0.8:
        diffuse_fraction = 0.9511 - 0.1604 * kt + 4.388 * kt**2 - 16.638 * kt**3 + 12.336 * kt**4
    else:
        diffuse_fraction = 0.165
    # Keep closure even when bounding physically implausible beam estimates.
    dni = min(extraterrestrial, max(0.0, ghi * (1 - diffuse_fraction) / sin_e))
    return dni, ghi - dni * sin_e


def irradiance_on_surface(surface: Surface, env: Environment) -> float:
    """Beam projection plus isotropic sky diffuse and ground reflection (albedo 0.2)."""
    dni, dhi = solar_components(env)
    if env.sun_elevation_deg <= 0:
        return 0.0
    elev = math.radians(env.sun_elevation_deg)
    tilt = math.radians(surface.tilt_deg)
    cos_incidence = math.sin(elev) * math.cos(tilt)
    if surface.bearing_deg is not None:
        az_diff = math.radians(env.sun_azimuth_deg - surface.bearing_deg)
        cos_incidence += math.cos(elev) * math.sin(tilt) * math.cos(az_diff)
    elif surface.tilt_deg != 0:
        cos_incidence = 0.0  # unknown tilted face: do not invent a beam bearing
    return (
        dni * max(0.0, cos_incidence)
        + dhi * (1 + math.cos(tilt)) / 2
        + 0.2 * estimate_ghi(env) * (1 - math.cos(tilt)) / 2
    )


def _other_side_affine(
    surface: Surface, env: Environment, params: ModelParams
) -> tuple[float, float]:
    """Other-side air = a*T_air + b; retain unknown neighbours in the inverse."""
    b = surface.boundary
    if b in (Boundary.OUTSIDE, Boundary.ROOF):
        return 0.0, env.t_out
    if b is Boundary.LOFT:
        return 0.0, env.t_out + params.loft_delta
    if b is Boundary.GROUND:
        return 0.0, env.t_ground
    if b is Boundary.UNHEATED_SPACE:
        fallback = params.unheated_fraction, (1 - params.unheated_fraction) * env.t_out
    else:
        fallback = 1.0, 0.0  # unknown heated neighbour follows this room's air
    neighbours = surface.adjacent_fractions or ({surface.adjacent: 1.0} if surface.adjacent else {})
    if not neighbours:
        return fallback
    a = constant = 0.0
    for room, fraction in neighbours.items():
        if room in env.adjacent_temps:
            constant += fraction * env.adjacent_temps[room]
        else:
            a += fraction * fallback[0]
            constant += fraction * fallback[1]
    return a, constant


def other_side_temperature(
    surface: Surface, env: Environment, t_air: float, params: ModelParams
) -> float:
    """Temperature on the far side of a surface, including partial known adjacencies."""
    a, b = _other_side_affine(surface, env, params)
    return a * t_air + b


def effective_u(surface: Surface, env: Environment, params: ModelParams) -> float:
    """Replace the outside film resistance, rather than scaling the whole wall.

    McAdams exterior convection 5.7+3.8*v, plus 4.7 W/m²K linearised exterior
    radiation to surroundings assumed at outdoor air temperature. Weather wind
    is a proxy for local facade wind; infiltration is not represented.
    """
    if surface.u_value == 0 or surface.boundary not in (Boundary.OUTSIDE, Boundary.ROOF):
        return surface.u_value
    r_other = 1 / surface.u_value - params.outside_resistance
    if r_other <= 0:
        raise ValueError("U-value does not leave a positive construction resistance")
    r_out = 1 / (5.7 + 3.8 * max(0.0, env.wind_ms) + 4.7)
    return 1 / (r_other + r_out)


def _validate(surfaces: list[Surface], env: Environment, p: ModelParams) -> None:
    """Reject invalid physical inputs before they can become thermostat targets."""

    def finite(*values: float) -> bool:
        return all(math.isfinite(x) for x in values)

    if not finite(
        p.h_i,
        p.h_r,
        p.outside_resistance,
        p.trust_k,
        p.cap_up,
        p.cap_down,
        p.step,
        p.asymmetry_a,
        p.asymmetry_cap,
        p.loft_delta,
        p.unheated_fraction,
        p.solar_cap_k,
    ):
        raise ValueError("non-finite model parameter")
    if not (
        p.h_i > p.h_r >= 0
        and p.outside_resistance >= 0
        and 0 <= p.trust_k <= 1
        and 0 <= p.unheated_fraction <= 1
        and min(p.cap_up, p.cap_down, p.step, p.asymmetry_a, p.asymmetry_cap, p.solar_cap_k) >= 0
    ):
        raise ValueError("invalid model parameter range")
    values = [
        env.t_out,
        env.t_ground,
        env.wind_ms,
        env.sun_elevation_deg,
        env.sun_azimuth_deg,
        env.day_of_year,
        *env.adjacent_temps.values(),
    ]
    values.extend(v for v in (env.ghi_wm2, env.cloud_fraction) if v is not None)
    if not finite(*values) or not (
        1 <= env.day_of_year <= 366 and -90 <= env.sun_elevation_deg <= 90
    ):
        raise ValueError("invalid environment input")
    if not surfaces:
        raise ValueError("room has no surfaces")
    if len({s.name for s in surfaces}) != len(surfaces):
        raise ValueError("surface names must be unique")
    for s in surfaces:
        vals = [
            s.area_m2,
            s.u_value,
            s.g_value,
            s.shade_factor,
            s.tilt_deg,
            *s.adjacent_fractions.values(),
        ]
        if s.bearing_deg is not None:
            vals.append(s.bearing_deg)
        if not finite(*vals) or not (
            s.area_m2 > 0
            and 0 <= s.u_value < H_I_DEFAULT
            and 0 <= s.g_value <= 1
            and 0 <= s.shade_factor <= 1
            and 0 <= s.tilt_deg <= 180
        ):
            raise ValueError(f"invalid physical surface: {s.name}")
        if s.adjacent_fractions and (
            any(v <= 0 for v in s.adjacent_fractions.values())
            or not math.isclose(sum(s.adjacent_fractions.values()), 1.0, abs_tol=1e-6)
        ):
            raise ValueError(f"adjacency fractions must be positive and sum to one: {s.name}")


@dataclass(frozen=True)
class _Balance:
    """Affine solution of a linearised diffuse enclosure heat balance."""

    air_coefficient: float
    constant: float
    solar_k: float
    solar_flux: float  # credited absorbed power per m², including any solar cap
    # Ts_i = a_i*T_air + b_i + r_i*MRT + inverse_d_i*solar_flux
    terms: list[tuple[float, float, float, float]]


def _surface_balance(surfaces: list[Surface], env: Environment, p: ModelParams) -> _Balance:
    """Solve conduction + convection + net internal radiation + absorbed solar = 0.

    Survey U includes reference inside film 1/7.7. Remove it to obtain
    conductance from the inside surface to the far-side air. Radiation goes
    to an area-weighted enclosure node; summed internal radiation is zero.
    This is a linearised room-average proxy, not occupant-specific MRT.
    """
    _validate(surfaces, env, p)
    total_area = sum(s.area_m2 for s in surfaces)
    terms = []
    air = constant = radiant = response = 0.0
    for s in surfaces:
        u = effective_u(s, env, p)
        resistance = 1 / u - 1 / H_I_DEFAULT if u else math.inf
        if resistance <= 0:
            raise ValueError(f"non-positive surface-to-boundary resistance: {s.name}")
        conductance = 1 / resistance
        inv_d = 1 / (conductance + p.h_i)
        other_a, other_b = _other_side_affine(s, env, p)
        a = (p.h_i - p.h_r + conductance * other_a) * inv_d
        b = conductance * other_b * inv_d
        r = p.h_r * inv_d
        terms.append((a, b, r, inv_d))
        w = s.area_m2 / total_area
        air += w * a
        constant += w * b
        radiant += w * r
        response += w * inv_d
    denom = 1 - radiant  # strictly positive because convection is positive
    solar_power = 0.0
    # Cloud-derived irradiance is too uncertain to lower heating targets.
    if env.ghi_wm2 is not None:
        solar_power = sum(
            s.g_value * s.shade_factor * irradiance_on_surface(s, env) * s.area_m2
            for s in surfaces
            if s.glazed and s.boundary in (Boundary.OUTSIDE, Boundary.ROOF)
        )
    # Uniform absorption is explicit. Cap the credited power, so the returned
    # temperatures and MRT still describe the same energy-conserving balance.
    flux = min(solar_power / total_area, p.solar_cap_k * denom / response)
    return _Balance(air / denom, constant / denom, flux * response / denom, flux, terms)


def solar_mrt_rise(surfaces: list[Surface], env: Environment, params: ModelParams) -> float:
    """Steady-state MRT rise from credited transmitted solar, K."""
    return _surface_balance(surfaces, env, params).solar_k


def steady_state_mrt(
    surfaces: list[Surface], env: Environment, t_air: float, params: ModelParams | None = None
) -> MRTBreakdown:
    """Room-average linearised MRT, with consistent individual surface temperatures."""
    if not math.isfinite(t_air):
        raise ValueError("non-finite air temperature")
    balance = _surface_balance(surfaces, env, params or ModelParams())
    mrt = balance.air_coefficient * t_air + balance.constant + balance.solar_k
    temps = {
        s.name: a * t_air + b + r * mrt + inv_d * balance.solar_flux
        for s, (a, b, r, inv_d) in zip(surfaces, balance.terms)
    }
    return MRTBreakdown(mrt, t_air, balance.solar_k, sum(s.area_m2 for s in surfaces), temps)


def operative_temperature(t_air: float, mrt: float) -> float:
    """ASHRAE 55 operative temperature for still air."""
    return 0.5 * (t_air + mrt)


def required_air_temperature(
    surfaces: list[Surface],
    env: Environment,
    ot_target: float,
    params: ModelParams | None = None,
) -> Correction:
    """Invert the same surface balance used for current-condition MRT.

    MRT = a*T_air + b + solar, hence T_air = (2*OT - b - solar)/(1+a).
    Boundary temperatures dependent on room air are included in coefficient a.
    """
    if not math.isfinite(ot_target):
        raise ValueError("non-finite OT target")
    p = params or ModelParams()
    balance = _surface_balance(surfaces, env, p)
    total_area = sum(s.area_m2 for s in surfaces)
    solar = balance.solar_k
    sum_l = 1 - balance.air_coefficient
    t_air = (2 * ot_target - balance.constant - solar) / (1 + balance.air_coefficient)

    offset_physical = t_air - ot_target
    offset_trusted = p.trust_k * offset_physical

    # Radiant asymmetry: how much colder the glazing is than the air, weighted
    # by its share of the surfaces. Represents sitting by a cold window.
    asym = 0.0
    if p.asymmetry_a > 0:
        glass_area = sum(s.area_m2 for s in surfaces if s.glazed)
        if glass_area > 0:
            temps = steady_state_mrt(surfaces, env, t_air, p).surface_temps
            glass_t = sum(s.area_m2 * temps[s.name] for s in surfaces if s.glazed) / glass_area
            asym = min(
                p.asymmetry_cap,
                max(0.0, p.asymmetry_a * (glass_area / total_area) * (t_air - glass_t)),
            )

    offset = offset_trusted + asym
    capped = False
    if offset > p.cap_up:
        offset, capped = p.cap_up, True
    elif offset < -p.cap_down:
        offset, capped = -p.cap_down, True

    setpoint = round_to_step(ot_target + offset, p.step)
    mrt_sp = steady_state_mrt(surfaces, env, setpoint, p).mrt
    return Correction(
        ot_target=ot_target,
        t_air_required=t_air,
        offset_physical=offset_physical,
        offset_trusted=offset_trusted,
        offset_asymmetry=asym,
        offset_final=offset,
        air_setpoint=setpoint,
        capped=capped,
        mrt_at_setpoint=mrt_sp,
        solar_k=solar,
        sum_l=sum_l,
    )


# ---------------------------------------------------------------------------
# Emitters and diagnostic running mean
# ---------------------------------------------------------------------------


def radiator_output_w(
    emitters: list[Emitter], t_flow: float, t_air: float, dt_drop: float = 10.0
) -> float:
    """Total emitter output at the running flow temperature.

    P = P50 · ((T_flow − ΔT_drop/2 − T_air) / 50)^n, zero if the mean water
    temperature is not above the room.
    """
    mean_water = t_flow - 0.5 * dt_drop
    dt = mean_water - t_air
    if dt <= 0:
        return 0.0
    return sum(e.output_dt50_w * (dt / 50.0) ** e.exponent for e in emitters)


def running_mean_outdoor(previous: float | None, today_mean: float, alpha: float = 0.8) -> float:
    """EN 16798 running mean: T_rm = α·T_rm,prev + (1−α)·T_yesterday."""
    if previous is None:
        return today_mean
    return alpha * previous + (1.0 - alpha) * today_mean
