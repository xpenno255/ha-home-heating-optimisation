"""Overdrive: lean on Evohome's valve control by moving the target written to a zone.

Evohome opens a valve roughly in proportion to how far its zone is below target, so a
room a few tenths short gets a valve only part open and can sit there all day. Overdrive
adds to the written target in proportion to the room's own shortfall (and takes a little
off when it is over), so the valve opens or closes harder. Nothing accumulates: the
addition follows the shortfall and is gone once the room is on target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# (shortfall K, added K) at strength 1, straight lines between; the last slope continues.
SCALE = ((0.0, 0.0), (0.1, 0.1), (0.2, 0.3), (0.3, 0.5), (0.5, 0.8), (0.6, 1.0))
# Over target: taken off one for one (times the strength), at most this much.
COOL_MAX_K = 0.5
# Closer than this to the target counts as on target.
DEADBAND_K = 0.05


def overdrive_wanted(short, strength, limit) -> float:
    """K to add to the zone target of a room ``short`` K below its air target.

    Negative ``short`` (over target) gives a negative addition. Capped at ``limit``
    above and ``COOL_MAX_K`` below, in 0.1 K steps; 0 without a limit.
    """
    if short is None or not limit or abs(short) < DEADBAND_K:
        return 0.0
    if short < 0:
        return -round(min(COOL_MAX_K, float(limit), -short * strength), 1)
    for (x0, y0), (x1, y1) in zip(SCALE, SCALE[1:]):
        if short <= x1:
            base = y0 + (y1 - y0) * (short - x0) / (x1 - x0)
            break
    else:
        (x0, y0), (x1, y1) = SCALE[-2], SCALE[-1]
        base = y1 + (y1 - y0) / (x1 - x0) * (short - x1)
    return round(min(float(limit), base * strength), 1)


@dataclass(frozen=True)
class OverdriveState:
    """The overdrive in force and when it last changed."""

    value: float = 0.0
    changed_at: datetime | None = None


def overdrive_step(state: OverdriveState, wanted, now: datetime, period_s) -> OverdriveState:
    """Move to ``wanted`` at most once per ``period_s``; easing towards 0 is immediate."""
    if wanted == state.value:
        return state
    due = state.changed_at is None or (now - state.changed_at).total_seconds() >= period_s
    if due or abs(wanted) < abs(state.value):
        return OverdriveState(wanted, now)
    return state
