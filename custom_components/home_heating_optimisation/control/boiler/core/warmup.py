"""Warm-up flow boost from whole-house demand (pure; no Home Assistant).

Evohome decides relay timing, but while several rooms call at once a flow target stuck at
the floor makes every burn short. The existing demand correction only reacts to the single
highest zone above 70 % for 20 minutes, so it is slow and ignores how many rooms call.

``house_demand`` is the share of the house's radiator capacity being asked for. The uplift
grows linearly from ``start`` to ``full`` of that share towards ``boost_max``, rises at
once and falls no faster than ``decay_k_per_min`` so the target does not flap.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MODES = ("off", "shadow", "active")
# The return guard fades the uplift out over this many kelvin below its limit.
RETURN_FADE_K = 5.0
# Radiator fullness needs a real flow-to-room span to mean anything.
FULLNESS_MIN_SPAN_K = 3.0
# Time constant of the fullness filter, and the longest step it integrates over (so a
# burner that has been off does not make the first firing reading jump the filter).
FULLNESS_TAU_S = 180.0
FULLNESS_MAX_STEP_S = 60.0
# Kelvin behind the planned trajectory that asks for the full uplift.
NEED_FULL_K = 0.5


@dataclass(frozen=True)
class WarmupBoostParams:
    start: float = 0.15
    full: float = 0.40
    boost_max: float = 65.0
    decay_k_per_min: float = 1.0
    return_margin_k: float = 5.0
    # The uplift fades out as the most overheated room goes from the first to the second.
    trim_start_k: float = 0.3
    trim_full_k: float = 0.8
    # During a planned pre-heat the uplift may also rise only this fast, and the fullness
    # cap runs from fullness_low (no cap) to fullness_high (no uplift).
    rise_k_per_min: float = 1.0
    need_full_k: float = NEED_FULL_K
    fullness_low: float = 0.45
    fullness_high: float = 0.65


@dataclass(frozen=True)
class WarmupBoostState:
    uplift: float = 0.0
    updated_at: datetime | None = None


@dataclass(frozen=True)
class FullnessState:
    """Filtered radiator fullness, held while the burner is off."""

    value: float | None = None
    updated_at: datetime | None = None


@dataclass(frozen=True)
class HouseDemand:
    fraction: float  # share of radiator capacity being asked for, 0-1
    room_equivalents: float  # sum of room demands, e.g. 2.4 rooms' worth
    valid: int
    total: int


def house_demand(rooms) -> HouseDemand | None:
    """``rooms`` is ``[(demand 0-1 | None, installed_output_dt50_w | None)]`` (enabled rooms).

    Demand is weighted by installed radiator output; a room with no known output counts at
    the mean known output (or equally when none is known). None when fewer than half of the
    rooms have a demand reading.
    """
    rooms = list(rooms)
    valid = [(min(1.0, max(0.0, d)), p) for d, p in rooms if d is not None]
    if not rooms or len(valid) * 2 < len(rooms):
        return None
    known = [p for _, p in valid if p is not None and p > 0]
    fallback = sum(known) / len(known) if known else 1.0
    weights = [p if p is not None and p > 0 else fallback for _, p in valid]
    fraction = sum(d * w for (d, _), w in zip(valid, weights)) / sum(weights)
    return HouseDemand(fraction, sum(d for d, _ in valid), len(valid), len(rooms))


def update_baselines(rooms, baselines, episode_on):
    """Each room's overshoot when the boost began: ``max(0, air - target)``.

    Outside an episode every room is re-baselined at its current overshoot, so an
    existing overshoot never blocks a boost from starting. During an episode the baselines
    stay, and a room seen for the first time joins with its overshoot at that moment.
    """
    out = dict(baselines) if episode_on else {}
    for name, air, target, _demand in rooms:
        if None not in (air, target) and name not in out:
            out[name] = max(0.0, air - target)
    return out


def overshoot(rooms, baselines=None):
    """``(kelvin, room, baseline)`` of the room that has risen furthest above target since
    the boost began, else None.

    ``rooms`` is ``[(name, air temperature | None, commanded air target | None, heat demand
    0-1 | None)]`` for the enabled rooms. Only the overshoot added since the boost began
    counts (``baselines`` maps room to its overshoot then; a room without one counts from 0),
    so a room already warm at the start does not hold the boost back. Only rooms still
    calling for heat count: a room with its valve shut is not taking the boosted water.
    Rooms missing a value, or with no demand reading, are ignored.
    """
    baselines = baselines or {}
    over = [
        (air - target - baselines.get(name, 0.0), name, baselines.get(name, 0.0))
        for name, air, target, demand in rooms
        if None not in (air, target, demand) and demand > 0.0
    ]
    return max(over, key=lambda item: item[0]) if over else None


def trim_factor(overshoot_k, params: WarmupBoostParams) -> float:
    """1 up to ``trim_start_k`` of overshoot, falling linearly to 0 at ``trim_full_k``."""
    if overshoot_k is None or overshoot_k <= params.trim_start_k:
        return 1.0
    if overshoot_k >= params.trim_full_k:
        return 0.0
    return (params.trim_full_k - overshoot_k) / (params.trim_full_k - params.trim_start_k)


def uplift_target(fraction, base, params: WarmupBoostParams) -> float:
    """Wanted uplift (K) above ``base`` for a house demand share."""
    if fraction is None or params.full <= params.start:
        return 0.0
    room = max(0.0, params.boost_max - base)
    scale = min(1.0, max(0.0, (fraction - params.start) / (params.full - params.start)))
    return scale * room


def return_scale(return_filtered, return_ceiling, params: WarmupBoostParams) -> float:
    """1 well below ``ceiling - margin``, fading to 0 at it; 1 when the return is unknown."""
    if return_filtered is None:
        return 1.0
    limit = return_ceiling - params.return_margin_k
    return min(1.0, max(0.0, (limit - return_filtered) / RETURN_FADE_K))


def boost_step(
    state: WarmupBoostState,
    fraction,
    base,
    now: datetime,
    params: WarmupBoostParams,
    scale: float = 1.0,
    trim: float = 1.0,
) -> WarmupBoostState:
    """Next uplift: up at once, down no faster than the decay rate, zero without a reading.

    ``scale`` (return guard) and ``trim`` (room overshoot) shrink the target, so a trimmed
    uplift still falls at the decay rate rather than at once."""
    if fraction is None:
        return WarmupBoostState(0.0, now)
    target = uplift_target(fraction, base, params) * scale * trim
    if target >= state.uplift or state.updated_at is None:
        uplift = target
    else:
        minutes = max(0.0, (now - state.updated_at).total_seconds() / 60)
        uplift = max(target, state.uplift - params.decay_k_per_min * minutes)
    return WarmupBoostState(min(uplift, max(0.0, params.boost_max - base)), now)


# -- planned pre-heat: radiator fullness and need -----------------------------------


def fullness(flow, ret, room):
    """Radiator fullness ``(return - room) / (flow - room)``; None when unknown or no span.

    Near 0.25 as a burn starts on cool radiators, 0.55-0.62 once they have loaded. It is
    only meaningful while the burner fires: it jumps towards 1 when the flow stops.
    """
    if None in (flow, ret, room) or flow - room < FULLNESS_MIN_SPAN_K:
        return None
    return (ret - room) / (flow - room)


def fullness_step(state: FullnessState, raw, firing: bool, now: datetime) -> FullnessState:
    """Exponential filter (about 3 minutes), updated only while firing; held otherwise."""
    if not firing or raw is None:
        return state
    if state.value is None or state.updated_at is None:
        return FullnessState(raw, now)
    elapsed = min(FULLNESS_MAX_STEP_S, max(0.0, (now - state.updated_at).total_seconds()))
    value = state.value + elapsed / (FULLNESS_TAU_S + elapsed) * (raw - state.value)
    return FullnessState(value, now)


def fullness_cap(filtered, base, boost_max, low, high) -> float:
    """Most uplift (K) the radiators can take: full below ``low``, none at ``high``.

    Unknown fullness gives the full cap: no information is no reason to restrict the boost
    beyond the other limits.
    """
    room = max(0.0, boost_max - base)
    if filtered is None or high <= low:
        return room
    return room * min(1.0, max(0.0, (high - filtered) / (high - low)))


def need_uplift(rooms, base, boost_max, params: WarmupBoostParams) -> float:
    """Uplift (K) the rooms in pre-heat ask for: how far the worst is behind its plan.

    ``rooms`` carry ``behind_k`` (positive: behind plan) and ``done``. A room already
    done (at its goal less tolerance and coast-up) or on or ahead of plan asks for none;
    ``need_full_k`` behind asks for everything up to ``boost_max``.
    """
    behind = [
        r.behind_k
        for r in rooms
        if not getattr(r, "done", False) and r.behind_k is not None and r.behind_k > 0
    ]
    if not behind:
        return 0.0
    room = max(0.0, boost_max - base)
    return room * min(1.0, max(0.0, max(behind) / params.need_full_k))


def preheat_step(
    state: WarmupBoostState, target, base, now: datetime, params: WarmupBoostParams
) -> WarmupBoostState:
    """Next uplift towards ``target``: up no faster than ``rise_k_per_min``, down no faster
    than ``decay_k_per_min``. The first step has no elapsed time to go on and counts a minute.
    """
    if state.updated_at is None:
        minutes = 1.0
    else:
        minutes = min(10.0, max(0.0, (now - state.updated_at).total_seconds() / 60))
    if target >= state.uplift:
        uplift = min(target, state.uplift + params.rise_k_per_min * minutes)
    else:
        uplift = max(target, state.uplift - params.decay_k_per_min * minutes)
    return WarmupBoostState(min(uplift, max(0.0, params.boost_max - base)), now)
