"""Overdrive: lean on Evohome's valve control by moving the target written to a zone.

Evohome opens a valve roughly in proportion to how far its zone is below target, so a
room a few tenths short gets a valve only part open and can sit there all day. Overdrive
adds to the written target in proportion to the room's own shortfall (and takes a little
off when it is over), so the valve opens or closes harder.

A push that only follows the shortfall is gone the moment the room is on target; the valve
then closes part-way, the room sags over an hour or two and the push comes back hard. So a
small *hold* is remembered and used as a floor: on target, and while a little short, the
room keeps at least the hold. It never adds to a push, and it fades out over the first
``HOLD_FADE_K`` above target. It is learnt slowly and only while the room is close to its
target: it grows while the room keeps needing a push there and shrinks while it sits over.

(0.28.0 added the hold to the push instead. On its first evening the Living room's pushes
came out about twice as large for the same shortfall, the hold reached its 0.5 K ceiling
in six rooms within a day, and the room peaked 0.8 K over target against 0.1-0.5 before.)
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime

# (shortfall K, added K) at strength 1, straight lines between; the last slope continues.
SCALE = ((0.0, 0.0), (0.1, 0.1), (0.2, 0.3), (0.3, 0.5), (0.5, 0.8), (0.6, 1.0))
# Over target: taken off one for one (times the strength), at most this much.
COOL_MAX_K = 0.5
# Closer than this to the target counts as on target.
DEADBAND_K = 0.05
# The hold: at most this much, built only while the room is no further than this below its
# target (a room further down is recovering, not holding), with this time constant: a
# push of 0.3 K kept up for an hour adds 0.1 K of hold.
HOLD_MAX_K = 0.3
HOLD_BAND_K = 0.25
HOLD_TAU_S = 180 * 60
# Over target the hold fades out, to nothing at this much over.
HOLD_FADE_K = 0.2
# Longest gap between two cycles that still counts towards the hold.
HOLD_MAX_GAP_S = 10 * 60


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


def hold_step(hold, short, push, seconds, limit) -> float:
    """The hold after ``seconds`` more of ``push`` (K, the part that follows the shortfall).

    Grows with a positive push and shrinks with a negative one, between nothing and
    ``HOLD_MAX_K`` (or ``limit`` when that is lower). It does not grow while the room is
    more than ``HOLD_BAND_K`` below its target (recovering, not holding); any time over
    target shrinks it. Unchanged after a gap longer than ``HOLD_MAX_GAP_S`` or without
    readings.
    """
    if not limit or short is None or seconds is None:
        return hold
    if short > HOLD_BAND_K or not 0 < seconds <= HOLD_MAX_GAP_S:
        return hold
    return min(max(hold + push * seconds / HOLD_TAU_S, 0.0), HOLD_MAX_K, float(limit))


def with_hold(push, hold, limit, short) -> float:
    """The overdrive to apply: the push, with the hold as a floor near the target.

    At or below target the room gets the larger of the two, never their sum. Above target
    the hold fades to nothing at ``HOLD_FADE_K`` over, so the easing-off is gradual.
    """
    if not limit or short is None:
        return 0.0
    if short >= 0:
        value = max(push, hold)
    else:
        value = push + hold * max(0.0, 1 + short / HOLD_FADE_K)
    return round(min(float(limit), max(-COOL_MAX_K, value)), 1)


@dataclass(frozen=True)
class OverdriveState:
    """The overdrive in force and when it last changed; the hold and when it was last fed."""

    value: float = 0.0
    changed_at: datetime | None = None
    hold: float = 0.0
    hold_at: datetime | None = None


def overdrive_step(state: OverdriveState, wanted, now: datetime, period_s) -> OverdriveState:
    """Raise to ``wanted`` at most once per ``period_s``; any lowering is immediate.

    Lowering covers a shrinking push, a room crossing above target and one going further
    over, so the valve starts closing as soon as the room catches up.
    """
    if wanted == state.value:
        return state
    due = state.changed_at is None or (now - state.changed_at).total_seconds() >= period_s
    if due or wanted < state.value:
        return replace(state, value=wanted, changed_at=now)
    return state
