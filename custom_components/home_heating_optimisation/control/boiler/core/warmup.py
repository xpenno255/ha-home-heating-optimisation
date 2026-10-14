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


@dataclass(frozen=True)
class WarmupBoostParams:
    start: float = 0.15
    full: float = 0.40
    boost_max: float = 65.0
    decay_k_per_min: float = 1.0
    return_margin_k: float = 5.0


@dataclass(frozen=True)
class WarmupBoostState:
    uplift: float = 0.0
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
) -> WarmupBoostState:
    """Next uplift: up at once, down no faster than the decay rate, zero without a reading."""
    if fraction is None:
        return WarmupBoostState(0.0, now)
    target = uplift_target(fraction, base, params) * scale
    if target >= state.uplift or state.updated_at is None:
        uplift = target
    else:
        minutes = max(0.0, (now - state.updated_at).total_seconds() / 60)
        uplift = max(target, state.uplift - params.decay_k_per_min * minutes)
    return WarmupBoostState(min(uplift, max(0.0, params.boost_max - base)), now)
