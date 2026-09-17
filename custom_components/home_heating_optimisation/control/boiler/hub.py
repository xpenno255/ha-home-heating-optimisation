"""Hub-level stateful, non-HA-API logic for Boiler Flow Control.

One instance per config entry. Owns the 10-minute low-pass filter on aggregate
heat demand, the heating-active toggle counter over a rolling 10-minute window,
the return-temperature freshness check, and the last-write memory used for
manual-hold detection (persisted via `BFCStore` so it survives restart).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.util import dt as dt_util

from .const import CYCLING_WINDOW_MINUTES, RETURN_FRESHNESS_MINUTES
from .core.model import DhwCyclingState, WriteMemory
from .store import BFCStore

_LOGGER = logging.getLogger(__name__)

DEMAND_FILTER_TAU_MINUTES = 10.0
# Below this the EMA is indistinguishable from zero demand; snap so the
# sensor doesn't sit on denormal residue (e.g. 2.8e-148) for hours.
DEMAND_FILTER_ZERO_SNAP = 0.1


@dataclass
class BoilerFlowHub:
    """Runtime data for the hub config entry."""

    store: BFCStore | None = None

    # Demand low-pass filter
    demand_filtered: float | None = None
    _last_demand_sample_at: datetime | None = field(default=None, repr=False)

    # Heating-active ignition counter (change 3: event-driven, not polled)
    _toggle_times: list = field(default_factory=list, repr=False)

    # Return-temperature freshness
    return_last_seen_at: datetime | None = None
    last_return_value: float | None = None

    # Last-write memory (manual-hold detection, revert-aware re-assertion)
    last_written_setpoint: float | None = None
    last_written_at: datetime | None = None
    last_target_change: datetime | None = None

    # DHW cycling guard state
    dhw_cycling: DhwCyclingState = field(default_factory=DhwCyclingState)

    global_enabled: bool = True
    burn_started_at: datetime | None = None
    last_burn_seconds: float | None = None
    last_stop_reason: str = "unknown"
    last_burner_power: float | None = None

    # ------------------------------------------------------------------
    def load(self) -> None:
        if self.store is None:
            return
        sp = self.store.get("last_written_setpoint")
        self.last_written_setpoint = float(sp) if sp is not None else None
        at = self.store.get("last_written_at")
        parsed = dt_util.parse_datetime(str(at)) if at else None
        self.last_written_at = dt_util.as_utc(parsed) if parsed else None
        change_at = self.store.get("last_target_change")
        parsed_change = dt_util.parse_datetime(str(change_at)) if change_at else None
        self.last_target_change = dt_util.as_utc(parsed_change) if parsed_change else None
        df = self.store.get("demand_filtered")
        self.demand_filtered = float(df) if df is not None else None
        # v0.3 retires automatic cycling interventions. Old holds must never
        # survive an upgrade and prevent a cylinder charge completing.
        self.dhw_cycling = DhwCyclingState()
        if self.store.get("control_version", 0) < 3:
            self.last_written_setpoint = None
            self.last_written_at = self.last_target_change = None

    def _persist(self) -> None:
        if self.store is None:
            return
        self.store.set("control_version", 3)
        self.store.set("last_written_setpoint", self.last_written_setpoint)
        self.store.set(
            "last_written_at", self.last_written_at.isoformat() if self.last_written_at else None
        )
        self.store.set(
            "last_target_change",
            self.last_target_change.isoformat() if self.last_target_change else None,
        )
        self.store.set("demand_filtered", self.demand_filtered)
        self.store.set("dhw_cycling_attempts", self.dhw_cycling.attempts)
        self.store.set("dhw_cycling_holding", self.dhw_cycling.holding)
        self.store.set(
            "dhw_cycling_last_intervention_at",
            self.dhw_cycling.last_intervention_at.isoformat()
            if self.dhw_cycling.last_intervention_at
            else None,
        )

    # ------------------------------------------------------------------
    def sample_demand(self, raw: float | None, now: datetime) -> float | None:
        """10-minute low-pass filter on aggregate heat demand (§3.2.2)."""
        if raw is None:
            return self.demand_filtered
        if self.demand_filtered is None:
            self.demand_filtered = raw
        else:
            dt_s = (
                (now - self._last_demand_sample_at).total_seconds()
                if self._last_demand_sample_at
                else 60.0
            )
            alpha = dt_s / (DEMAND_FILTER_TAU_MINUTES * 60.0 + dt_s)
            self.demand_filtered += alpha * (raw - self.demand_filtered)
            if raw == 0.0 and self.demand_filtered < DEMAND_FILTER_ZERO_SNAP:
                self.demand_filtered = 0.0
        self._last_demand_sample_at = now
        return self.demand_filtered

    def record_ignition(self, now: datetime) -> int:
        """Record one off->on transition of the heating-active binary sensor
        (change 3: event-driven, via `async_track_state_change_event` in the
        coordinator, not the 60 s poll — the boiler can short-cycle faster than
        once a minute, so a polled read undercounts). Returns the ignition
        count in the trailing `CYCLING_WINDOW_MINUTES` window (§3.2.4, §3.3.3)."""
        self.burn_started_at = now
        self._toggle_times.append(now)
        return self.cycles_10min(now)

    def record_stop(
        self, now: datetime, relay_on: bool | None, current_flow: float | None, target: float | None
    ) -> None:
        self.last_burn_seconds = (
            (now - self.burn_started_at).total_seconds() if self.burn_started_at else None
        )
        self.burn_started_at = None
        if relay_on is False:
            self.last_stop_reason = "relay_request_ended"
        elif (
            relay_on is True
            and current_flow is not None
            and target is not None
            and current_flow >= target - 1
        ):
            self.last_stop_reason = "possible_flow_limit"
        elif relay_on is True:
            self.last_stop_reason = "burner_stopped_during_heat_call"
        else:
            self.last_stop_reason = "unknown_relay_state"

    def cycles_10min(self, now: datetime) -> int:
        """Current ignition count in the trailing window, pruning stale entries
        without recording a new one (used by the coordinator's 60 s poll to read
        the sensor value)."""
        cutoff = now - timedelta(minutes=CYCLING_WINDOW_MINUTES)
        self._toggle_times = [t for t in self._toggle_times if t >= cutoff]
        return len(self._toggle_times)

    def ignitions_since(self, since: datetime | None, now: datetime) -> int:
        """Ignition count in the trailing window that occurred strictly after
        `since` (v0.2.1 review fix 3a: the DHW cycling guard's last
        intervention), or the whole window's count when `since` is None (no
        intervention yet). Used instead of `cycles_10min` for the DHW cycling
        guard so a single batch of ignitions cannot re-trigger an attempt on
        every 60 s poll while it ages out of the window."""
        cutoff = now - timedelta(minutes=CYCLING_WINDOW_MINUTES)
        self._toggle_times = [t for t in self._toggle_times if t >= cutoff]
        if since is None:
            return len(self._toggle_times)
        return len([t for t in self._toggle_times if t > since])

    def sample_return(
        self, value: float | None, last_reported: datetime | None, now: datetime
    ) -> tuple[float | None, bool]:
        """Return (value_to_use, fresh). Fresh means the sensor's own
        `last_reported` (v0.2.1 review fix 5) is within RETURN_FRESHNESS_MINUTES
        of `now` — not merely that the state was numeric at poll time, which let
        a wedged-but-numeric sensor stay "fresh" forever. Grace: once the
        sensor goes unavailable, the last known value/timestamp keeps being
        used until that same deadline passes."""
        if value is not None:
            self.last_return_value = value
            self.return_last_seen_at = last_reported or now
        fresh = self.return_last_seen_at is not None and now - self.return_last_seen_at < timedelta(
            minutes=RETURN_FRESHNESS_MINUTES
        )
        return self.last_return_value, fresh

    def record_write(self, value: float, now: datetime, target_changed: bool = True) -> None:
        """Record a `number.set_value` call. `target_changed` is False for a
        re-assertion of an unchanged target (change 2): `last_written_at`
        always advances, `last_target_change` only advances when the value
        itself changed."""
        self.last_written_setpoint = value
        self.last_written_at = now
        if target_changed:
            self.last_target_change = now
        self._persist()

    def write_memory(self) -> WriteMemory:
        return WriteMemory(
            last_written_setpoint=self.last_written_setpoint,
            last_written_at=self.last_written_at,
            last_target_change=self.last_target_change,
        )

    def set_dhw_cycling(self, state: DhwCyclingState) -> None:
        self.dhw_cycling = state
        self._persist()

    async def async_save(self) -> None:
        self._persist()
        if self.store is not None:
            await self.store.async_save()
