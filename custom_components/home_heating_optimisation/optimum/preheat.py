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


def _slot_out(slot):
    return [slot[0].timestamp(), slot[1]] if slot else None


def _slot_in(raw):
    try:
        return (_at(float(raw[0])), float(raw[1]))
    except TypeError, ValueError, IndexError:
        return None


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
    # The room's temperature at the switchpoint, as far as it could be established: what
    # the next start learns from. None until the capture is finalised, and when it failed.
    switch_temp: float | None = None
    # The last qualified report at or before the switchpoint and the first after it, each
    # ``(report time, value)``; the finalised capture (method, times, values, gap, reason).
    obs_before: tuple | None = None
    obs_after: tuple | None = None
    capture: dict | None = None
    # A restart or reload between the start and the capture: the outcome is still
    # reported, but it teaches nothing.
    interrupted: bool = False
    # A hot water charge was running when this pre-heat was due to begin, however briefly.
    dhw_held: bool = False
    # Minutes of lead added (or, negative, taken off) from earlier arrivals at this time.
    lead_bias_minutes: float = 0.0

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
            "switch_temp": self.switch_temp,
            "lead_bias_minutes": self.lead_bias_minutes,
            "obs_before": _slot_out(self.obs_before),
            "obs_after": _slot_out(self.obs_after),
            "capture": self.capture,
            "interrupted": self.interrupted,
            "dhw_held": self.dhw_held,
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
            switch_temp=raw.get("switch_temp"),
            lead_bias_minutes=float(raw.get("lead_bias_minutes") or 0.0),
            obs_before=_slot_in(raw.get("obs_before")),
            obs_after=_slot_in(raw.get("obs_after")),
            capture=raw.get("capture") if isinstance(raw.get("capture"), dict) else None,
            interrupted=bool(raw.get("interrupted")),
            dhw_held=bool(raw.get("dhw_held")),
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


# Learning the lead from how a pre-heat turned out. A room within this much of its goal at
# the switchpoint was on time (0.1 K sensor steps and a reading up to ten minutes from the
# switchpoint leave about that much doubt); otherwise the shortfall is turned into minutes
# at the pace the plan climbed at. The extra lead stays within these limits, and one
# arrival never moves it by more than LEAD_STEP_MAX.
ARRIVAL_DEADBAND_K = 0.2
LEAD_BIAS_MIN = -30.0
LEAD_BIAS_MAX = 60.0
LEAD_STEP_MAX = 20.0


# Capturing the temperature at the switchpoint. Reports are kept only inside this window
# either side of it, and the capture is finalised once the window has closed. Two reports
# that bracket the switchpoint no further apart than BRACKET_SPAN are interpolated;
# failing that the nearest report counts if it is within NEAREST of the switchpoint.
CAPTURE_WINDOW = timedelta(minutes=15)
BRACKET_SPAN = timedelta(minutes=25)
NEAREST = timedelta(minutes=10)


def note_report(plan, reported_at, value):
    """``plan`` with a qualified report filed in its slot, if it belongs in one.

    The slots are the last report at or before the switchpoint and the first after it,
    both inside the capture window. A report already filed, one outside the window, and
    anything once the capture is final or the plan interrupted change nothing.
    """
    if plan.capture is not None or plan.interrupted or reported_at is None or value is None:
        return plan
    if abs(reported_at - plan.switch_at) > CAPTURE_WINDOW:
        return plan
    if reported_at <= plan.switch_at:
        if plan.obs_before is None or reported_at > plan.obs_before[0]:
            return replace(plan, obs_before=(reported_at, float(value)))
    elif plan.obs_after is None or reported_at < plan.obs_after[0]:
        return replace(plan, obs_after=(reported_at, float(value)))
    return plan


def finalise_capture(plan):
    """``plan`` with its capture settled from the reports in its two slots.

    Interpolated between two reports that bracket the switchpoint within ``BRACKET_SPAN``;
    else the nearest report within ``NEAREST``; else nothing, with the reason. Never
    extrapolated. The record keeps the method, both reports and the gap between them (or
    from the one report to the switchpoint), in minutes.
    """
    if plan.capture is not None:
        return plan
    before, after, switch = plan.obs_before, plan.obs_after, plan.switch_at
    record = {
        "method": None,
        "value": None,
        "before": [before[0].isoformat(), before[1]] if before else None,
        "after": [after[0].isoformat(), after[1]] if after else None,
        "gap_minutes": None,
        "reason": None,
    }
    if plan.interrupted:
        record["reason"] = "restart or reload during the pre-heat"
    elif before and after and after[0] - before[0] <= BRACKET_SPAN:
        span = (after[0] - before[0]).total_seconds()
        share = (switch - before[0]).total_seconds() / span if span > 0 else 0.0
        record["method"] = "interpolated"
        record["value"] = round(before[1] + (after[1] - before[1]) * share, 2)
        record["gap_minutes"] = round(span / 60, 1)
    else:
        near = [slot for slot in (before, after) if slot and abs(slot[0] - switch) <= NEAREST]
        if near:
            slot = min(near, key=lambda item: abs(item[0] - switch))
            record["method"] = "nearest"
            record["value"] = slot[1]
            record["gap_minutes"] = round(abs(slot[0] - switch).total_seconds() / 60, 1)
        else:
            record["reason"] = "no qualified report within 10 minutes of the switchpoint"
    return replace(plan, capture=record, switch_temp=record["value"])


# A start within this of its planned minute is the control cycle catching up: of the 21
# starts journalled 18 to 21 Nov 2026, 13 began 0.1 to 0.7 minutes after their planned
# time with nothing in the way. Later than this, for whatever reason, the room did not
# get the start it was planned.
LATE_START_MINUTES = 2.0


def held_back(plan) -> bool:
    """Whether this pre-heat did not get the start it was planned.

    A hot water charge was running when it was due (``dhw_held``, set at the time and
    kept, so a charge that ended a minute later still counts), it began while one was
    running (the start reasons other than ``planned``), or it began late for any other
    reason. How the room then arrived says nothing about the plan.
    """
    return (
        plan.dhw_held
        or plan.start_reason not in ("", "planned")
        or plan.dhw_wait_minutes > LATE_START_MINUTES
    )


def lead_error_minutes(plan) -> float | None:
    """Minutes the start was out by, from the room's temperature at the switchpoint.

    Positive: it was short, so it should have started that much earlier. Negative: it was
    over its goal. None when there is nothing fair to learn from: no captured reading, an
    interrupted pre-heat, a plan that never started or was held back by hot water, or a
    room that had no climb to make.
    """
    if plan.interrupted or not plan.capture or plan.capture.get("method") is None:
        return None
    if not plan.started or plan.switch_temp is None or held_back(plan):
        return None
    rise = plan.goal - plan.start_temp
    if rise <= 0 or plan.climb_minutes <= 0:
        return None
    short = plan.goal - plan.switch_temp
    if abs(short) <= ARRIVAL_DEADBAND_K:
        return 0.0
    return round(short * plan.climb_minutes / rise, 1)


def learn_lead(bias, error_minutes, gain) -> float:
    """The extra lead after one more arrival: a share (``gain``) of the error is taken up."""
    if error_minutes is None or not gain or gain <= 0:
        return bias
    step = max(-LEAD_STEP_MAX, min(LEAD_STEP_MAX, gain * error_minutes))
    return round(max(LEAD_BIAS_MIN, min(LEAD_BIAS_MAX, bias + step)), 1)


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
    # Rated radiator output, kW: its say over the flow temperature (None: full say).
    output_kw: float | None = None


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
