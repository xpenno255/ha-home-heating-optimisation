"""Heat spreading: keep enough radiators open for the boiler to deliver into.

With only a couple of valves open the boiler reaches its flow target in under a minute
and stops, so the radiators go cold between calls (a burn starting with under 4 kW of
radiator capacity open lasted a median 0.8 minutes in Nov 2026; with 8 kW or more open,
4.5 minutes). Meanwhile half the house's radiators stay shut because their rooms sit a
few tenths over target, kept warm by their neighbours.

So while the house is calling for heat and too little capacity is open, the rooms that are
at, or only a little over, their target are *lifted*: their written zone target is raised
to just above the room's own temperature, which half-opens the valve. A lift is never more
than ``max_k`` above the room's target, a room well over target is left alone, and a room
that is short is left to its overdrive.

``shadow`` works everything out and reports it without changing a target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MODES = ("off", "shadow", "active")
# Told this much above the room's own temperature, a valve sits roughly half open.
MARGIN_K = 0.2
# A room this far past the most a lift may add is too warm to take part.
OVER_K = 0.3
# A room this far below target is short: overdrive has it.
SHORT_K = 0.1
# Open capacity (as a multiple of the engage level) at which spreading lets go again.
RELEASE_RATIO = 1.5
# Once engaged or released, stay so for at least this long: valves take minutes to move.
HOLD_SECONDS = 20 * 60
# The boiler relay demand (0-1) that counts as the house calling for heat.
CALLING_DEMAND = 0.1


@dataclass(frozen=True)
class SpreadRoom:
    """One room as spreading sees it; ``error`` is air minus air target, ``valve`` 0-1."""

    room_id: str
    error: float | None
    valve: float | None
    rated_kw: float
    eligible: bool = True


@dataclass(frozen=True)
class SpreadState:
    engaged: bool = False
    changed_at: datetime | None = None


def open_capacity(rooms) -> float:
    """Radiator capacity open right now, kW: each valve's opening times its rated output."""
    return round(sum((r.valve or 0.0) * r.rated_kw for r in rooms), 2)


def room_lift(room: SpreadRoom, max_k: float) -> float:
    """K to add to this room's zone target while spreading, or 0."""
    if not room.eligible or room.error is None or room.rated_kw <= 0:
        return 0.0
    if room.error < -SHORT_K or room.error > max_k + OVER_K:
        return 0.0
    return round(min(max_k, max(0.0, room.error + MARGIN_K)), 1)


def spread_step(state: SpreadState, rooms, calling: bool, now: datetime, capacity_kw, max_k):
    """``(next state, {room_id: lift K}, open capacity kW)``.

    Engages when the house is calling and less than ``capacity_kw`` is open; lets go when
    the call ends or ``RELEASE_RATIO`` times that much is open. Either way it then stays
    put for ``HOLD_SECONDS``, except that the end of the call releases at once.
    """
    capacity = open_capacity(rooms)
    held = state.changed_at is not None and (now - state.changed_at).total_seconds() < HOLD_SECONDS
    engaged = state.engaged
    if not calling:
        engaged = False
    elif not held:
        if not engaged and capacity < capacity_kw:
            engaged = True
        elif engaged and capacity >= capacity_kw * RELEASE_RATIO:
            engaged = False
    if engaged != state.engaged:
        state = SpreadState(engaged, now)
    lifts = {}
    if engaged:
        for room in rooms:
            lift = room_lift(room, max_k)
            if lift > 0:
                lifts[room.room_id] = lift
    return state, lifts, capacity


class Spread:
    """The house's spreading state, stepped by the control runtime."""

    def __init__(self):
        self.state = SpreadState()
        self.mode = "off"
        self.lifts: dict = {}
        self.capacity_kw: float | None = None
        self.calling = False
        self._stepped_at: datetime | None = None

    def due(self, now: datetime, seconds=30) -> bool:
        return self._stepped_at is None or abs((now - self._stepped_at).total_seconds()) >= seconds

    def step(self, now: datetime, mode, rooms, calling, capacity_kw, max_k) -> bool:
        """Advance to ``now``; True when spreading has just engaged or let go."""
        self._stepped_at = now
        self.mode = mode
        if mode not in ("shadow", "active"):
            changed = self.state.engaged
            self.state, self.lifts, self.capacity_kw, self.calling = SpreadState(), {}, None, False
            return changed
        before = self.state.engaged
        self.calling = bool(calling)
        self.state, self.lifts, self.capacity_kw = spread_step(
            self.state, rooms, self.calling, now, capacity_kw, max_k
        )
        return self.state.engaged != before

    def lift(self, room_id) -> float:
        return self.lifts.get(room_id, 0.0)

    def report(self, names=None) -> dict:
        names = names or {}
        return {
            "mode": self.mode,
            "engaged": self.state.engaged,
            "since": self.state.changed_at.isoformat() if self.state.changed_at else None,
            "calling": self.calling,
            "open_capacity_kw": self.capacity_kw,
            "lifts": {names.get(k, k): v for k, v in self.lifts.items()},
        }
