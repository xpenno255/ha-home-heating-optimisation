"""State and constraints for measured, bounded supervisory control."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from math import floor, isfinite

from .model import DemandCorrectionState, Mode, ReturnCorrectionState, WriteMemory


def effective_target(
    target: float, minimum: float, maximum: float, step: float = 1.0
) -> float | None:
    """Resolve a number's grid and bounds without rounding above a hard cap."""
    if (
        not all(isfinite(v) for v in (target, minimum, maximum, step))
        or minimum > maximum
        or step <= 0
    ):
        return None
    value = max(minimum, min(maximum, target))
    ticks = floor((value - minimum) / step + 0.5 + 1e-9)
    max_ticks = floor((maximum - minimum) / step + 1e-9)
    return round(minimum + min(ticks, max_ticks) * step, 6)


@dataclass
class DhwDemandTracker:
    """Confirm inferred demand for two minutes; tolerate brief missing RF data."""

    candidate_since: datetime | None = None
    last_confirmed: datetime | None = None
    active: bool = False
    source: str = "none"

    def update(self, relay: bool | None, inferred: bool, definite_off: bool, now: datetime) -> bool:
        if relay is True:
            self.active, self.source, self.last_confirmed = True, "relay", now
            self.candidate_since = None
        elif inferred:
            self.candidate_since = self.candidate_since or now
            if now - self.candidate_since >= timedelta(minutes=2):
                self.active, self.source, self.last_confirmed = True, "inferred", now
        else:
            self.candidate_since = None
            if (
                definite_off
                or self.last_confirmed is None
                or now - self.last_confirmed >= timedelta(minutes=2)
            ):
                self.active, self.source = False, "none"
            elif self.active:
                self.source = "grace"
        return self.active


@dataclass
class ChargeMonitor:
    starts: int = 0
    started_at: datetime | None = None
    baseline_at: datetime | None = None
    baseline_temp: float | None = None
    fallback: bool = False
    reason: str | None = None

    def update(
        self,
        active: bool,
        temperature: float | None,
        target: float,
        now: datetime,
        progress_minutes: float,
        timeout_minutes: float,
    ) -> None:
        if not active:
            self.starts = 0
            self.started_at = self.baseline_at = None
            self.baseline_temp = None
            self.fallback, self.reason = False, None
            return
        if self.started_at is None:
            self.started_at = now
        if temperature is not None and self.baseline_temp is None:
            self.baseline_temp, self.baseline_at = temperature, now
        if now - self.started_at >= timedelta(minutes=timeout_minutes):
            self.fallback, self.reason = True, "charge_timeout"
        elif (
            temperature is not None
            and temperature < target - 0.5
            and self.baseline_at is not None
            and now - self.baseline_at >= timedelta(minutes=progress_minutes)
        ):
            if temperature - self.baseline_temp < 1.0:
                self.fallback, self.reason = True, "insufficient_temperature_progress"
            self.baseline_at, self.baseline_temp = now, temperature


@dataclass
class RoomFeedback:
    """Bounded warm-up assistance only for sustained, poorly recovering rooms."""

    samples: dict[str, tuple[datetime, float, float]] = field(default_factory=dict)

    def update(
        self, rooms: dict[str, tuple[float, float]], now: datetime, active: bool
    ) -> tuple[float, float | None]:
        self.samples = {k: v for k, v in self.samples.items() if k in rooms and active}
        correction = 0.0
        errors = []
        for entity, (temperature, target) in rooms.items():
            error = target - temperature
            errors.append(error)
            if not active or error < 1.0:
                self.samples.pop(entity, None)
                continue
            sample = self.samples.get(entity)
            if sample is None or abs(sample[2] - target) > 0.1:
                self.samples[entity] = (now, temperature, target)
                continue
            elapsed = (now - sample[0]).total_seconds() / 60
            if elapsed >= 10:
                slope = (temperature - sample[1]) / elapsed
                if slope < 0.05:
                    correction = max(correction, min(8.0, 2.0 * error))
                # Keep a bounded trend window without dropping the correction each poll.
                if elapsed >= 20:
                    self.samples[entity] = (
                        now - timedelta(minutes=10),
                        temperature - slope * 10,
                        target,
                    )
        return correction, max(errors) if errors else None


@dataclass
class ControlState:
    demand: DemandCorrectionState = field(default_factory=DemandCorrectionState)
    return_trim: ReturnCorrectionState = field(default_factory=ReturnCorrectionState)
    rooms: RoomFeedback = field(default_factory=RoomFeedback)
    previous_mode: Mode = Mode.OFF
    heating_since: datetime | None = None
    virtual_memory: WriteMemory = field(default_factory=WriteMemory)
