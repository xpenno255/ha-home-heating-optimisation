"""Burner cycling metrics and a cycling-adjusted efficiency estimate (pure; clock injected).

The firing estimate leaves out the first minutes of every burn and any per-start losses
(pre/post purge, ignition, re-heating the boiler's own mass). A house that starts the
burner dozens of times a day pays those losses far more often than one with a few long
burns, so burns are recorded from the burner modulation reading and a per-start loss is
*assumed*. The assumption is a constant, not a measurement.
"""

from __future__ import annotations

import statistics
from datetime import datetime, timedelta
from math import isfinite

# Assumed energy lost per start (purge, ignition, reheating the boiler's own mass), kWh.
START_LOSS_KWH = 0.02
SHORT_BURN_SECONDS = 300.0
# The modulation entity only publishes on change, so a steady burn can be silent for many
# minutes and the last value simply continues. A burn silent for longer than this is
# treated as lost (incomplete) rather than guessed at.
MAX_SILENCE_SECONDS = 2 * 3600.0
RETENTION = timedelta(days=7)


class BurnTracker:
    """Turns burner modulation readings into completed burns.

    A burn starts when a reading above zero follows an observed zero, and ends at the next
    zero. A steady value is a continuation. A burn whose readings go unavailable, or that is
    silent for over two hours, is dropped,
    as is one already running when tracking began (an HA restart), because its start is
    unknown.
    """

    def __init__(self):
        self.reset()
        self.last_power: float | None = None

    def reset(self):
        self.start: datetime | None = None
        self.last_at: datetime | None = None
        self.weight = 0.0
        self.dhw_seconds = 0.0
        self.last_power = None

    @property
    def active(self):
        return self.start is not None

    def update(self, now: datetime, power: float | None, dhw: bool):
        """Feed a reading (None when invalid); returns a completed burn row or None.

        A row is ``[start epoch, duration s, mean modulation %, 1 if DHW else 0]``.
        """
        valid = power is not None and isfinite(power) and 0 <= power <= 100
        if self.active:
            held = self.last_power or 0.0
            elapsed = (now - self.last_at).total_seconds()
            if not valid or elapsed > MAX_SILENCE_SECONDS or elapsed < 0:
                self.reset()
                return None
            self.weight += held * elapsed
            if dhw:
                self.dhw_seconds += elapsed
            self.last_at = now
            if power == 0:
                duration = (now - self.start).total_seconds()
                row = None
                if duration > 0:
                    row = [
                        self.start.timestamp(),
                        round(duration, 1),
                        round(self.weight / duration, 1),
                        1 if self.dhw_seconds > duration / 2 else 0,
                    ]
                self.reset()
                self.last_power = 0.0
                return row
            self.last_power = power
            return None
        if not valid:
            self.last_power = None
            return None
        if power > 0 and self.last_power == 0:
            self.start = self.last_at = now
            self.weight = self.dhw_seconds = 0.0
        self.last_power = power
        return None


def add_burn(burns: list, row: list, now: datetime) -> None:
    burns.append(row)
    prune(burns, now)


def prune(burns: list, now: datetime) -> None:
    cutoff = (now - RETENTION).timestamp()
    burns[:] = [b for b in burns if b[0] >= cutoff]


def load_burns(raw) -> list:
    """Valid four-number rows only; anything else is ignored."""
    burns = []
    for row in raw if isinstance(raw, list) else []:
        if (
            isinstance(row, list)
            and len(row) == 4
            and all(isinstance(v, (int, float)) and isfinite(v) for v in row)
            and row[1] > 0
        ):
            burns.append([float(row[0]), float(row[1]), float(row[2]), int(row[3])])
    return sorted(burns)


def _window(burns, now: datetime, window: timedelta):
    cutoff = (now - window).timestamp()
    return [b for b in burns if b[0] >= cutoff]


def summarise(burns, now: datetime, window: timedelta) -> dict:
    """Cycling figures for the burns that started inside the window."""
    inside = _window(burns, now, window)
    seconds = [b[1] for b in inside]
    total = sum(seconds)
    short = sum(s for s in seconds if s < SHORT_BURN_SECONDS)
    return {
        "starts": len(inside),
        "heating_starts": sum(1 for b in inside if not b[3]),
        "dhw_starts": sum(1 for b in inside if b[3]),
        "firing_minutes": round(total / 60, 1),
        "mean_burn_minutes": round(total / len(inside) / 60, 1) if inside else None,
        "median_burn_minutes": round(statistics.median(seconds) / 60, 1) if inside else None,
        "short_burn_share": round(short / total, 3) if total else None,
        "starts_per_firing_hour": round(len(inside) / (total / 3600), 1) if total else None,
    }


def covered_days(burns, now: datetime, window: timedelta) -> float:
    """Days of log behind a window, at least one, so per-day figures are not understated."""
    if not burns:
        return 1.0
    span = (now.timestamp() - min(b[0] for b in burns)) / 86400
    return max(1.0, min(window.total_seconds() / 86400, span))


def fuel_fire_kwh(burns, now: datetime, window: timedelta, nominal_kw: float) -> float:
    """Gas energy burnt while firing: nominal input x modulation x time (net basis)."""
    return sum(nominal_kw * b[2] / 100 * b[1] / 3600 for b in _window(burns, now, window))


def adjusted_efficiency(firing_percent, fuel_kwh, starts, loss_kwh=START_LOSS_KWH):
    """Firing estimate reduced by the assumed per-start loss; None without both sides."""
    if firing_percent is None or fuel_kwh is None or fuel_kwh <= 0:
        return None
    return round(firing_percent * fuel_kwh / (fuel_kwh + starts * loss_kwh), 1)
