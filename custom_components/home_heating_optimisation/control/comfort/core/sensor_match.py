"""Match the zone's own sensor to the room's air sensor (pure; no Home Assistant).

HHO's air target is in room-sensor terms, but Evohome drives the valve from its zone
sensor (often the TRV head). Where the two read differently the room settles off target
by the gap. With matching on, the written zone target is the air target less the slowly
filtered gap, so the zone sensor aims at what the room sensor should then read.
"""

from __future__ import annotations

import math

from .model import round_to_step

GAP_TAU_H = 2.0  # door openings and radiator bursts do not move the offset
MAX_OFFSET_K = 1.5
STEP_K = 0.1


def gap_step(previous: float | None, gap: float, elapsed_h: float, tau_h: float = GAP_TAU_H):
    """First-order filter of ``room sensor - zone sensor``; seeds with the first gap."""
    if previous is None or not math.isfinite(previous) or tau_h <= 0:
        return gap
    weight = 1.0 - math.exp(-max(0.0, elapsed_h) / tau_h)
    return previous + weight * (gap - previous)


def zone_offset(gap: float | None) -> float:
    """Kelvin taken off a room-sensor target before it is written to the zone."""
    if gap is None or not math.isfinite(gap):
        return 0.0
    return round_to_step(min(MAX_OFFSET_K, max(-MAX_OFFSET_K, gap)), STEP_K)


def to_zone(target: float | None, offset: float) -> float | None:
    """A room-sensor target in zone-sensor terms."""
    return None if target is None else round(target - offset, 2)
