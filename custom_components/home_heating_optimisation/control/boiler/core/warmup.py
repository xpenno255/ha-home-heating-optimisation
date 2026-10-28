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
# A held reading older than this (no firing) is dropped.
FULLNESS_HOLD_S = 15 * 60
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
    """Exponential filter (about 3 minutes), updated only while firing; held otherwise.

    A held value goes stale after FULLNESS_HOLD_S without a firing update and is dropped:
    the radiators cool while the burner is off. Live 23 Oct: a 20-second burn just after the
    hot-water charge read 0.61 from the hot system and was held for an hour, capping the
    planned uplift at +2.7 K through the warm-up.
    """
    if state.updated_at is not None and (now - state.updated_at).total_seconds() > FULLNESS_HOLD_S:
        state = FullnessState()
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


def plan_floor(rooms, base, plan_flow, boost_max) -> float:
    """Uplift (K) that holds the flow at the plan flow while a room is still warming.

    Starts are timed for ``plan_flow``, so a room on plan still gets it rather than the
    weather curve's lower flow. None once every room is done, or when the curve is already
    at or above the plan flow. Not subject to the fullness cap or the overshoot trim.
    """
    if plan_flow is None or not any(not getattr(r, "done", False) for r in rooms):
        return 0.0
    return min(max(0.0, plan_flow - base), max(0.0, boost_max - base))


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


# ---------------------------------------------------------------------------
# Catch-up: a room stuck below its target outside a planned pre-heat
# ---------------------------------------------------------------------------

# A room this far below its air target starts the clock ...
CATCHUP_START_K = 0.3
# ... and, once pushed, is pushed until it is this close.
CATCHUP_END_K = 0.1
# Shortfall that asks for the whole push.
CATCHUP_FULL_K = 0.8
# The shortfall must last this long without the room recovering at this rate.
CATCHUP_SUSTAIN_S = 20 * 60
CATCHUP_RECOVERING_K_PER_H = 0.2
# Highest flow a catch-up asks for (below the warm-up maximum: the house is mostly warm).
CATCHUP_MAX_FLOW = 60.0


@dataclass(frozen=True)
class CatchupTrack:
    """A room below target: since when, its air then, and whether it is being pushed."""

    since: datetime
    air_since: float
    pushing: bool = False


def catchup_step(tracks, rooms, now: datetime) -> dict:
    """Next per-room tracks.

    ``rooms`` is ``[(name, air, air target, heat demand 0-1, in operating window)]``. A room
    counts only while calling for heat inside its window. One at least ``CATCHUP_START_K``
    short for ``CATCHUP_SUSTAIN_S``, and warming slower than ``CATCHUP_RECOVERING_K_PER_H``
    since then, is pushed; a pushed room stays pushed until within ``CATCHUP_END_K``, so the
    recovery the push causes does not end it.
    """
    out = {}
    for name, air, target, demand, in_window in rooms:
        if None in (air, target, demand) or demand <= 0 or not in_window:
            continue
        short = target - air
        track = tracks.get(name)
        if track is not None and track.pushing:
            if short >= CATCHUP_END_K:
                out[name] = track
            continue
        if short < CATCHUP_START_K:
            continue
        if track is None:
            out[name] = CatchupTrack(now, air)
            continue
        elapsed_s = (now - track.since).total_seconds()
        rate = (air - track.air_since) * 3600 / elapsed_s if elapsed_s > 0 else 0.0
        pushing = elapsed_s >= CATCHUP_SUSTAIN_S and rate < CATCHUP_RECOVERING_K_PER_H
        out[name] = CatchupTrack(track.since, track.air_since, pushing)
    return out


def catchup_need(tracks, rooms, base, ceiling):
    """``(uplift K, room, shortfall K)`` for the pushed room furthest below target.

    The uplift runs from nothing at ``CATCHUP_END_K`` short to ``ceiling - base`` at
    ``CATCHUP_FULL_K``. ``(0.0, None, None)`` when no room is pushed.
    """
    worst = None
    for name, air, target, *_ in rooms:
        track = tracks.get(name)
        if track is None or not track.pushing or None in (air, target):
            continue
        if worst is None or target - air > worst[1]:
            worst = (name, target - air)
    if worst is None:
        return 0.0, None, None
    span = CATCHUP_FULL_K - CATCHUP_END_K
    share = min(1.0, max(0.0, (worst[1] - CATCHUP_END_K) / span))
    return max(0.0, ceiling - base) * share, worst[0], worst[1]
