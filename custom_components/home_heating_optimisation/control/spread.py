"""Heat spreading and the boiler-time floor: run the boiler long enough, into enough radiators.

Two things starve the house while its rooms are on target.

*Too few radiators open.* With only a couple of valves open the boiler reaches its flow
target in under a minute and stops (a burn starting with under 4 kW of radiator capacity
open lasted a median 0.8 minutes in Nov 2026; with 8 kW or more open, 4.5 minutes). Half
the house's radiators stay shut because their rooms sit a few tenths over target, and
overdrive shuts them further by taking heat off any room that is over.

*Too little boiler time.* Evohome calls the boiler according to the single most-open valve
(40-59 % open: called 18 % of the time; 60-79 %: 37 %; 80-99 %: 67 %). Rooms on target sit
near 50 %, so the call collapses to a few one-minute burns an hour, the radiators go cold
and the rooms sag until one is cold enough to be pushed hard.

So while spreading is engaged every room that is at, or a little over, its target is told
its target plus a common *level* instead of its overdrive. At level 0 that only stops heat
being taken off rooms that are slightly warm, so their valves stay part open. The level
then rises a step at a time while Evohome's call is below the *floor* (the boiler time the
weather needs) and falls again when the call is comfortably above it or the house is
running warm. Every valve opens a little together, so the heat is shared and no one room
is driven hard. A room more than ``max_k`` over target, a short room (overdrive has it)
and a pre-heating room are left alone.

``shadow`` works everything out and reports it without changing a target.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

MODES = ("off", "shadow", "active")
# A room this far below target is short: overdrive has it.
SHORT_K = 0.1
# Open capacity (as a multiple of the engage level) at which spreading lets go again.
RELEASE_RATIO = 1.5
# Once engaged or released, stay so for at least this long: valves take minutes to move.
HOLD_SECONDS = 20 * 60
# The boiler relay demand (0-1) that counts as the house calling for heat.
CALLING_DEMAND = 0.1
# The level moves one step at most this often: Evohome re-works its call every 10 minutes.
LEVEL_STEP_K = 0.1
LEVEL_PERIOD_S = 10 * 60
# The call must be this far (percentage points) under the floor to raise the level, and
# this far over it to lower it.
FLOOR_UNDER = 5.0
FLOOR_OVER = 15.0
# Error of the rooms taking part, weighted by radiator size: above the first the level
# stops rising and spreading does not start; above the second the level falls; above the
# third it lets go.
WARM_HOLD_K = 0.2
WARM_EASE_K = 0.3
WARM_RELEASE_K = 0.4
# The floor is given for 5 C outside and scales with how far below this it is, up to a cap.
FLOOR_BASE_C = 15.5
FLOOR_REFERENCE_C = 5.0
FLOOR_MAX = 60.0


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
    level: float = 0.0
    level_at: datetime | None = None


@dataclass(frozen=True)
class SpreadInputs:
    """What the house looks like this cycle."""

    calling: bool
    relay_demand: float | None  # Evohome's boiler call, 0-100
    outdoor: float | None
    capacity_kw: float  # engage below this much open capacity
    max_k: float  # the level's ceiling, and how far over target a room may be to take part
    floor_at_5c: float  # boiler-time floor at 5 C outside, 0-100; 0 turns the floor off


def open_capacity(rooms) -> float:
    """Radiator capacity open right now, kW: each valve's opening times its rated output."""
    return round(sum((r.valve or 0.0) * r.rated_kw for r in rooms), 2)


def boiler_floor(floor_at_5c, outdoor) -> float:
    """The boiler call (0-100) the weather needs: ``floor_at_5c`` at 5 C, nothing at 15.5 C."""
    if not floor_at_5c or floor_at_5c <= 0:
        return 0.0
    if outdoor is None:
        return float(floor_at_5c)
    scale = (FLOOR_BASE_C - outdoor) / (FLOOR_BASE_C - FLOOR_REFERENCE_C)
    return round(min(FLOOR_MAX, max(0.0, floor_at_5c * scale)), 1)


def taking_part(room: SpreadRoom, max_k: float) -> bool:
    """Whether this room is lifted while spreading is engaged."""
    if not room.eligible or room.error is None or room.rated_kw <= 0:
        return False
    return -SHORT_K - 1e-9 <= room.error <= max_k + 1e-9


def spread_step(state: SpreadState, rooms, inputs: SpreadInputs, now: datetime):
    """``(next state, {room_id: lift K}, info)``; a lift of 0.0 still replaces a take-off."""
    capacity = open_capacity(rooms)
    floor = boiler_floor(inputs.floor_at_5c, inputs.outdoor)
    relay = inputs.relay_demand
    members = [r for r in rooms if taking_part(r, inputs.max_k)]
    # How warm the house is running: the members' errors weighted by radiator size, so
    # the big rooms count for most and a small warm room does not veto the rest.
    weight = sum(r.rated_kw for r in members)
    warmth = sum(r.error * r.rated_kw for r in members) / weight if members else None
    below_floor = floor > 0 and relay is not None and relay < floor - FLOOR_UNDER
    under_floor = floor > 0 and relay is not None and relay < floor + FLOOR_OVER
    need = (inputs.calling and capacity < inputs.capacity_kw) or below_floor
    keep = (inputs.calling and capacity < inputs.capacity_kw * RELEASE_RATIO) or under_floor
    held = state.changed_at is not None and (now - state.changed_at).total_seconds() < HOLD_SECONDS
    engaged = state.engaged
    if not members:
        engaged = False  # nobody to lift: outside comfort hours, or everyone short or hot
    elif not held:
        if not engaged:
            engaged = need and warmth <= WARM_HOLD_K
        else:
            engaged = keep and warmth <= WARM_RELEASE_K
    level, level_at = state.level, state.level_at
    if not engaged:
        level, level_at = 0.0, None
    elif level_at is None:
        level_at = now  # just engaged: start at level 0, the first step after one period
    elif (now - level_at).total_seconds() >= LEVEL_PERIOD_S:
        if warmth > WARM_EASE_K or (relay is not None and relay > floor + FLOOR_OVER):
            level, level_at = max(0.0, round(level - LEVEL_STEP_K, 1)), now
        elif below_floor and warmth <= WARM_HOLD_K:
            level, level_at = min(inputs.max_k, round(level + LEVEL_STEP_K, 1)), now
    changed_at = now if engaged != state.engaged else state.changed_at
    state = SpreadState(engaged, changed_at, level, level_at)
    lifts = {r.room_id: level for r in members} if engaged else {}
    info = {
        "open_capacity_kw": capacity,
        "boiler_floor": floor,
        "boiler_call": relay,
        "house_warmth_k": None if warmth is None else round(warmth, 2),
    }
    return state, lifts, info


class Spread:
    """The house's spreading state, stepped by the control runtime."""

    def __init__(self):
        self.state = SpreadState()
        self.mode = "off"
        self.lifts: dict = {}
        self.info: dict = {}
        self.calling = False
        self._stepped_at: datetime | None = None

    def due(self, now: datetime, seconds=30) -> bool:
        return self._stepped_at is None or abs((now - self._stepped_at).total_seconds()) >= seconds

    def step(self, now: datetime, mode, rooms, inputs: SpreadInputs) -> bool:
        """Advance to ``now``; True when spreading engaged, let go or changed its level."""
        self._stepped_at = now
        self.mode = mode
        before = (self.state.engaged, self.state.level)
        if mode not in ("shadow", "active"):
            self.state, self.lifts, self.info, self.calling = SpreadState(), {}, {}, False
        else:
            self.calling = bool(inputs.calling)
            self.state, self.lifts, self.info = spread_step(self.state, rooms, inputs, now)
        return (self.state.engaged, self.state.level) != before

    @property
    def engaged_active(self) -> bool:
        return self.mode == "active" and self.state.engaged

    def lift(self, room_id):
        """This room's lift in K, or None when it is not being lifted."""
        return self.lifts.get(room_id)

    def report(self, names=None) -> dict:
        names = names or {}
        return {
            "mode": self.mode,
            "engaged": self.state.engaged,
            "since": self.state.changed_at.isoformat() if self.state.changed_at else None,
            "level_k": self.state.level,
            "calling": self.calling,
            **self.info,
            "rooms": [names.get(k, k) for k in self.lifts],
        }
