"""Hub-level shared state for v2: flow temperature sampling and running-mean outdoor temperature.

One instance per Home Assistant, owned by the hub config entry. Room coordinators
call `sample()` each cycle with what they read; the hub keeps the shared answer.
Persisted through `OTStore` so restarts do not lose the running mean.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime

from .core.model import running_mean_outdoor
from .store import OTStore

_LOGGER = logging.getLogger(__name__)


@dataclass
class OTHubData:
    """Runtime data for the global hub entry."""

    global_enabled: bool = True
    restore_complete: bool = False  # set by setup once the global-enable switch has restored
    store: OTStore | None = None
    schedule_fetch_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    # Flow temperature: last value seen while DHW was not active.
    flow_temp_used: float | None = None
    flow_temp_source: str = "none"
    dhw_active_seen: bool = False
    # Running mean outdoor temperature (EN 16798, alpha 0.8, daily).
    running_mean: float | None = None
    outdoor_used: float | None = None
    days_completed: int = 0  # full days folded into the running mean
    MIN_DAYS_FOR_ADAPTIVE: int = 3
    _listeners: list = field(default_factory=list, repr=False)
    _day: date | None = field(default=None, repr=False)
    _day_sum: float = field(default=0.0, repr=False)
    _day_n: int = field(default=0, repr=False)

    # ------------------------------------------------------------------
    def add_listener(self, cb) -> None:
        """Register a callback run after each sample so hub sensors update immediately."""
        self._listeners.append(cb)

    def remove_listener(self, cb) -> None:
        if cb in self._listeners:
            self._listeners.remove(cb)

    def _notify(self) -> None:
        for cb in list(self._listeners):
            try:
                cb()
            except Exception:  # noqa: BLE001
                _LOGGER.debug("hub listener failed", exc_info=True)

    def load(self) -> None:
        if self.store is None:
            return
        rm = self.store.get("running_mean")
        self.running_mean = float(rm) if rm is not None else None
        self.days_completed = int(self.store.get("days_completed", 0) or 0)
        ft = self.store.get("flow_temp_used")
        self.flow_temp_used = float(ft) if ft is not None else None
        d = self.store.get("day")
        if d:
            try:
                self._day = date.fromisoformat(str(d))
                self._day_sum = float(self.store.get("day_sum", 0.0))
                self._day_n = int(self.store.get("day_n", 0))
            except ValueError, TypeError:
                self._day = None

    def _persist(self) -> None:
        if self.store is None:
            return
        self.store.set("running_mean", self.running_mean)
        self.store.set("days_completed", self.days_completed)
        self.store.set("flow_temp_used", self.flow_temp_used)
        self.store.set("day", self._day.isoformat() if self._day else None)
        self.store.set("day_sum", self._day_sum)
        self.store.set("day_n", self._day_n)

    # ------------------------------------------------------------------
    def sample_flow_temp(
        self, value: float | None, dhw_active: bool | None, manual: float | None
    ) -> float | None:
        """Update the flow temperature in use. Returns the value to use now."""
        self.dhw_active_seen = bool(dhw_active)
        # Only accept a reading when DHW is known inactive; unknown may be DHW-elevated.
        if value is not None and dhw_active is False:
            self.flow_temp_used = value
            self.flow_temp_source = "entity"
        elif self.flow_temp_used is None and manual is not None:
            self.flow_temp_used = manual
            self.flow_temp_source = "manual"
        self._notify()
        return self.flow_temp_used

    def sample_outdoor(self, t_out: float, now: datetime) -> float | None:
        """Accumulate today's outdoor mean; roll the running mean at the first sample of a new day."""
        today = now.date()
        self.outdoor_used = t_out
        if self._day is None:
            self._day = today
        if today != self._day:
            if self._day_n > 0:
                yesterday_mean = self._day_sum / self._day_n
                self.running_mean = running_mean_outdoor(self.running_mean, yesterday_mean)
                self.days_completed += 1
            self._day, self._day_sum, self._day_n = today, 0.0, 0
        self._day_sum += t_out
        self._day_n += 1
        if self.running_mean is None and self._day_n >= 1:
            # Seed with the first reading so adaptive comfort has something on day one.
            self.running_mean = self._day_sum / self._day_n
        self._persist()
        self._notify()
        return self.running_mean

    @property
    def running_mean_ready(self) -> bool:
        """True once enough full days are in the mean to trust an adaptive shift."""
        return self.running_mean is not None and self.days_completed >= self.MIN_DAYS_FOR_ADAPTIVE

    async def async_save(self) -> None:
        if self.store is not None:
            await self.store.async_save()
