"""Heat spreading: steer the valves so the boiler runs long enough, into enough radiators.

Two things starve the house while its rooms are on target.

*Too few radiators open.* With only a couple of valves open the boiler reaches its flow
target in under a minute and stops (a burn starting with under 4 kW of radiator capacity
open lasted a median 0.8 minutes in Nov 2026; with 8 kW or more open, 4.5 minutes). About
half the burner's starts are of this kind. Half the house's radiators stay shut because
their rooms sit a few tenths over target.

*Too little boiler time.* Evohome calls the boiler according to the single most-open valve
(40-59 % open: called 18 % of the time; 60-79 %: 37 %; 80-99 %: 67 %). Rooms on target sit
near 50 %, so the call collapses, the radiators go cold and the rooms sag.

A valve cannot be set directly: the valve head works its own position out from the target
it is told and its own sensor, and the same push opens one room's valve by thirty points
and another's by six. So while spreading is engaged each room taking part is *steered*: its
told target is raised or lowered a step at a time until the valve reports the opening
wanted. Every room taking part is steered to a common share (40 % by default), which keeps
capacity open. When Evohome's call is under the *floor* (the boiler time the weather needs)
one *lead* room, the one with most room to take heat, is steered further, to the opening
that earns that much boiler time.

Taking part means being at, or no more than ``max_k`` over, target. A room further over,
a short room (overdrive has it), a pre-heating room and an excluded room are left alone;
a room that leaves loses its steer at once. The steer only ever adds to the told target,
never more than ``steer_max_k``, and stands in for the room's overdrive.

``shadow`` works everything out and reports it without changing a target.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

MODES = ("off", "shadow", "active")
# A room this far below target is short: overdrive has it.
SHORT_K = 0.1
# Once engaged or released, stay so for at least this long: valves take minutes to move.
HOLD_SECONDS = 20 * 60
# The boiler relay demand (0-1) that counts as the house calling for heat.
CALLING_DEMAND = 0.1
# A room's steer moves one step at most this often. The step is the smallest change the
# rooms send to Evohome for a room that is over target.
STEER_STEP_K = 0.3
STEER_PERIOD_S = 10 * 60
# The valve must be this many points under its target to be opened further, and this many
# over it for the steer to be eased back.
STEER_UNDER = 10.0
STEER_OVER = 15.0
# The call must be this far (percentage points) under the floor for a lead room to be
# chosen, and this far over it for the lead to be dropped.
FLOOR_UNDER = 5.0
FLOOR_OVER = 15.0
# How warm the rooms taking part are running, weighted by radiator size: above the first
# spreading does not start and no lead is chosen; above the second it lets go.
WARM_HOLD_K = 0.2
WARM_RELEASE_K = 0.4
# The floor is given for 5 C outside and scales with how far below this it is, up to a cap.
FLOOR_BASE_C = 15.5
FLOOR_REFERENCE_C = 5.0
FLOOR_MAX = 60.0
# Evohome's boiler call (%) against the most-open valve (%), measured 13-20 Nov 2026.
CALL_TO_VALVE = ((18.0, 50.0), (37.0, 70.0), (67.0, 90.0))
LEAD_VALVE_MAX = 85.0
# A new lead is chosen only when another room is this much cooler against its target.
LEAD_SWITCH_K = 0.1


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
    lead: str | None = None
    # room_id -> (steer K, when it last moved)
    steer: dict = field(default_factory=dict)


@dataclass(frozen=True)
class SpreadInputs:
    """What the house looks like this cycle."""

    calling: bool
    relay_demand: float | None  # Evohome's boiler call, 0-100
    outdoor: float | None
    capacity_kw: float  # engage below this much open capacity
    max_k: float  # how far over target a room may be and still take part
    floor_at_5c: float  # boiler-time floor at 5 C outside, 0-100; 0 turns the floor off
    valve_pct: float = 40.0  # the opening every room taking part is steered to
    steer_max_k: float = 1.0  # the most a steer may add to a room's told target


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


def lead_valve(call) -> float:
    """The valve opening (%) at which Evohome calls the boiler ``call`` % of the time."""
    (c0, v0), *rest = CALL_TO_VALVE
    if call <= c0:
        return v0
    for c1, v1 in rest:
        if call <= c1:
            return round(min(LEAD_VALVE_MAX, v0 + (v1 - v0) * (call - c0) / (c1 - c0)), 0)
        c0, v0 = c1, v1
    return LEAD_VALVE_MAX


def taking_part(room: SpreadRoom, max_k: float) -> bool:
    """Whether this room is steered while spreading is engaged."""
    if not room.eligible or room.error is None or room.rated_kw <= 0:
        return False
    return -SHORT_K - 1e-9 <= room.error <= max_k + 1e-9


def choose_lead(current, members):
    """The room to open furthest: the coolest against its target, the bigger radiator on a
    tie; the present lead is kept unless another is clearly cooler."""
    if not members:
        return None
    best = min(members, key=lambda r: (round(r.error, 2), -r.rated_kw))
    held = next((r for r in members if r.room_id == current), None)
    if held is not None and held.error <= best.error + LEAD_SWITCH_K:
        return held.room_id
    return best.room_id


def steer_step(steer, at, valve, target, now: datetime, steer_max_k):
    """``(steer K, when it last moved)`` one step nearer the valve opening wanted.

    ``valve`` is 0-1 and ``target`` 0-100. Nothing without a valve reading, inside the
    dead band, or within ``STEER_PERIOD_S`` of the last move.
    """
    if valve is None:
        return steer, at
    if at is not None and (now - at).total_seconds() < STEER_PERIOD_S:
        return steer, at
    opening = valve * 100
    if opening < target - STEER_UNDER and steer < steer_max_k:
        return round(min(steer_max_k, steer + STEER_STEP_K), 1), now
    if opening > target + STEER_OVER and steer > 0:
        return round(max(0.0, steer - STEER_STEP_K), 1), now
    return steer, at


def spread_step(state: SpreadState, rooms, inputs: SpreadInputs, now: datetime):
    """``(next state, {room_id: steer K}, info)``; a steer of 0.0 still replaces a take-off."""
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
    # Letting go is not judged on open capacity: once steering it is our own doing.
    keep = inputs.calling or under_floor
    held = state.changed_at is not None and (now - state.changed_at).total_seconds() < HOLD_SECONDS
    engaged = state.engaged
    if not members:
        engaged = False  # nobody to steer: outside comfort hours, or everyone short or hot
    elif not held:
        if not engaged:
            engaged = need and warmth <= WARM_HOLD_K
        else:
            engaged = keep and warmth <= WARM_RELEASE_K
    lead, steer, targets = None, {}, {}
    if engaged:
        if warmth > WARM_HOLD_K:
            lead = None  # the house is running warm: nobody is opened further
        elif below_floor:
            lead = choose_lead(state.lead, members)
        elif under_floor and any(r.room_id == state.lead for r in members):
            lead = state.lead  # inside the dead band: keep the lead there is
        for room in members:
            target = lead_valve(floor) if room.room_id == lead else inputs.valve_pct
            targets[room.room_id] = target
            k, at = state.steer.get(room.room_id, (0.0, None))
            steer[room.room_id] = steer_step(
                min(k, inputs.steer_max_k), at, room.valve, target, now, inputs.steer_max_k
            )
    changed_at = now if engaged != state.engaged else state.changed_at
    state = SpreadState(engaged, changed_at, lead, steer)
    lifts = {room_id: k for room_id, (k, _at) in steer.items()}
    info = {
        "open_capacity_kw": capacity,
        "boiler_floor": floor,
        "boiler_call": relay,
        "house_warmth_k": None if warmth is None else round(warmth, 2),
        "valves": {
            r.room_id: {
                "valve": None if r.valve is None else round(r.valve * 100),
                "target": targets[r.room_id],
                "steer_k": steer[r.room_id][0],
            }
            for r in members
            if r.room_id in targets
        },
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
        """Advance to ``now``; True when spreading engaged, let go or changed its lead."""
        self._stepped_at = now
        self.mode = mode
        before = (self.state.engaged, self.state.lead)
        if mode not in ("shadow", "active"):
            self.state, self.lifts, self.info, self.calling = SpreadState(), {}, {}, False
        else:
            self.calling = bool(inputs.calling)
            self.state, self.lifts, self.info = spread_step(self.state, rooms, inputs, now)
        return (self.state.engaged, self.state.lead) != before

    @property
    def engaged_active(self) -> bool:
        return self.mode == "active" and self.state.engaged

    def lift(self, room_id):
        """This room's steer in K, or None when it is not being steered."""
        return self.lifts.get(room_id)

    def report(self, names=None) -> dict:
        names = names or {}
        info = dict(self.info)
        valves = info.pop("valves", {})
        return {
            "mode": self.mode,
            "engaged": self.state.engaged,
            "since": self.state.changed_at.isoformat() if self.state.changed_at else None,
            "calling": self.calling,
            **info,
            "lead": names.get(self.state.lead, self.state.lead),
            "rooms": {names.get(k, k): v for k, v in valves.items()},
        }
