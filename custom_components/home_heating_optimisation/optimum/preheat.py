"""Pure planning pieces for the morning pre-heat: the plan, its trajectory, hot water.

A plan says when a room should start warming so that it reaches its goal at the
switchpoint, at a gentle flow. The straight-line trajectory (after the room's dead time)
is the yardstick the room is judged against while it warms. Hot water outranks space
heating, so a start that would run into the next scheduled charge moves to its end.

Time is injected: nothing here reads the clock.
"""

import math
import statistics
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

# Charge length assumed until enough real charges have been seen.
DEFAULT_CHARGE_MINUTES = 40
MIN_CHARGES = 3
MAX_CHARGES = 10
# Observed charges outside this range are a glitch (a flickering relay, a stuck flag).
CHARGE_RANGE = (5, 180)


def _at(ts):
    return datetime.fromtimestamp(ts, timezone.utc) if ts is not None else None


@dataclass(frozen=True)
class PreheatPlan:
    """One room's planned warm-up to one upward switchpoint.

    ``started`` is the latch: once the pre-heat has begun the plan stops moving, so a
    room that warms faster or slower than planned is neither released nor re-timed.
    """

    room_id: str
    key: int  # the switchpoint's timestamp: one plan per warm-up
    start_at: datetime
    switch_at: datetime
    schedule_target: float
    goal: float
    start_temp: float
    # Dead time before the room responds, then minutes to climb from start_temp to goal.
    delay_minutes: float
    climb_minutes: float
    planned_minutes: int  # start to switchpoint, margin included
    late_risk_minutes: int = 0  # predicted minutes past the switchpoint (hot water or lead cap)
    plan_factor: float = 1.0
    started: bool = False
    started_at: datetime | None = None
    planned_journaled: bool = False
    arrived_at: datetime | None = None
    peak_temp: float | None = None
    # The latest start that still arrives by the switchpoint at the boost flow (the most a
    # hot water charge may delay it), and what the start actually waited and why.
    latest_start: datetime | None = None
    dhw_wait_minutes: float = 0.0
    start_reason: str = ""

    @property
    def begin(self):
        """When the room actually began warming: the start, or later if it was held back."""
        return self.started_at or self.start_at

    def expected_at(self, now):
        """Where a room on plan would be: flat through the dead time, then a straight climb."""
        elapsed = (now - self.begin).total_seconds() / 60 - self.delay_minutes
        if elapsed <= 0:
            return self.start_temp
        if self.climb_minutes <= 0 or elapsed >= self.climb_minutes:
            return max(self.goal, self.start_temp)
        return self.start_temp + (self.goal - self.start_temp) * elapsed / self.climb_minutes

    def on_plan_k(self, now, temp):
        """Actual minus expected, kelvin (negative: behind plan); None before the start."""
        if not self.started or temp is None:
            return None
        return round(temp - self.expected_at(now), 2)

    def to_dict(self):
        return {
            "room_id": self.room_id,
            "key": self.key,
            "start_ts": self.start_at.timestamp(),
            "switch_ts": self.switch_at.timestamp(),
            "schedule_target": self.schedule_target,
            "goal": self.goal,
            "start_temp": self.start_temp,
            "delay_minutes": self.delay_minutes,
            "climb_minutes": self.climb_minutes,
            "planned_minutes": self.planned_minutes,
            "late_risk_minutes": self.late_risk_minutes,
            "plan_factor": self.plan_factor,
            "started": self.started,
            "started_ts": self.started_at.timestamp() if self.started_at else None,
            "planned_journaled": self.planned_journaled,
            "arrived_ts": self.arrived_at.timestamp() if self.arrived_at else None,
            "peak_temp": self.peak_temp,
            "latest_ts": self.latest_start.timestamp() if self.latest_start else None,
            "dhw_wait_minutes": self.dhw_wait_minutes,
            "start_reason": self.start_reason,
        }

    @classmethod
    def from_dict(cls, raw):
        return cls(
            room_id=str(raw["room_id"]),
            key=int(raw["key"]),
            start_at=_at(raw["start_ts"]),
            switch_at=_at(raw["switch_ts"]),
            schedule_target=float(raw["schedule_target"]),
            goal=float(raw["goal"]),
            start_temp=float(raw["start_temp"]),
            delay_minutes=float(raw["delay_minutes"]),
            climb_minutes=float(raw["climb_minutes"]),
            planned_minutes=int(raw["planned_minutes"]),
            late_risk_minutes=int(raw.get("late_risk_minutes") or 0),
            plan_factor=float(raw.get("plan_factor") or 1.0),
            started=bool(raw.get("started")),
            started_at=_at(raw.get("started_ts")),
            planned_journaled=bool(raw.get("planned_journaled")),
            arrived_at=_at(raw.get("arrived_ts")),
            peak_temp=raw.get("peak_temp"),
            latest_start=_at(raw.get("latest_ts")),
            dhw_wait_minutes=float(raw.get("dhw_wait_minutes") or 0.0),
            start_reason=str(raw.get("start_reason") or ""),
        )

    def start(self, now, temp, reason="planned", margin_minutes=0):
        """The latched copy: the plan from the moment the pre-heat began.

        Starting after the planned start (held back by hot water) records the wait, and the
        late risk is raised to what arrival from now would be.
        """
        began = max(now, self.start_at)
        wait = round((began - self.start_at).total_seconds() / 60, 1)
        heating = self.delay_minutes + self.climb_minutes + margin_minutes
        late = math.ceil((began + timedelta(minutes=heating) - self.switch_at).total_seconds() / 60)
        return replace(
            self,
            started=True,
            started_at=began,
            start_temp=self.start_temp if temp is None else temp,
            dhw_wait_minutes=wait,
            start_reason=reason,
            late_risk_minutes=max(self.late_risk_minutes, late),
        )


@dataclass(frozen=True)
class PreheatRoom:
    """A room that is pre-heating now, as the boiler side needs to see it."""

    room_id: str
    name: str
    goal: float
    commanded: float | None  # what the room controller is asking its zone for
    air_temp: float | None
    expected_temp: float
    # Kelvin behind the planned trajectory (positive: behind, zero or negative: on or ahead).
    behind_k: float | None
    started_at: datetime
    switch_at: datetime
    planned_minutes: int
    # At its goal less tolerance and the room's coast-up: heat already stored in the
    # radiators will carry it the rest of the way, so it asks for no more.
    done: bool = False
    coast_up_k: float = 0.3


def dhw_wait_deadline(plan, charge_end, grace_minutes=10):
    """When a pre-heat held back by a hot water charge must start anyway.

    The earlier of the expected end of the charge plus a grace, and the latest start that
    still arrives by the switchpoint at the boost flow. Also why: ``dhw_grace`` or
    ``latest_start``. Past it the system heats the cylinder and the radiators together.
    """
    grace = charge_end + timedelta(minutes=grace_minutes)
    latest = plan.latest_start or plan.start_at
    if latest <= grace:
        return max(latest, plan.start_at), "latest_start"
    return grace, "dhw_grace"


# -- hot water --------------------------------------------------------------


def next_dhw_on(state):
    """Start of the next scheduled DHW On period from the Evohome cloud water heater.

    The entity reports ``status.setpoints.next_sp_from`` and ``next_sp_state`` ("On" or
    "Off"); None when the next change is to Off, or when it gives no usable answer.
    """
    if state is None or state.state in ("unavailable", "unknown"):
        return None
    status = state.attributes.get("status")
    setpoints = status.get("setpoints") if isinstance(status, dict) else None
    if not isinstance(setpoints, dict):
        return None
    if str(setpoints.get("next_sp_state") or "").lower() != "on":
        return None
    raw = setpoints.get("next_sp_from")
    parsed = dt_util.parse_datetime(str(raw)) if raw else None
    return dt_util.as_utc(parsed) if parsed is not None else None


def charge_length(durations):
    """Expected DHW charge length in minutes: the median of recent charges, else a default."""
    recent = [d for d in durations if CHARGE_RANGE[0] <= d <= CHARGE_RANGE[1]][-MAX_CHARGES:]
    if len(recent) < MIN_CHARGES:
        return DEFAULT_CHARGE_MINUTES
    return statistics.median(recent)


def avoid_charge(start_at, switch_at, lead_minutes, dhw_on, charge_minutes):
    """``(start, late_risk_minutes)`` with the start moved past a charge it would overlap.

    The window from the start to the switchpoint overlaps the charge when the start is
    before the charge ends and the charge starts before the switchpoint. Late risk is how
    far past the switchpoint the room would then arrive (0 when still on time).
    """
    if dhw_on is None:
        return start_at, 0
    charge_end = dhw_on + timedelta(minutes=charge_minutes)
    if not (start_at < charge_end and dhw_on < switch_at):
        return start_at, 0
    late = math.ceil(
        (charge_end + timedelta(minutes=lead_minutes) - switch_at).total_seconds() / 60
    )
    return min(charge_end, switch_at), max(0, late)
