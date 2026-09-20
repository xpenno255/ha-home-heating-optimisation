"""HA glue: gather inputs, run the hub + core, and act once (spec v0.1 §3–§5).

All decisions live in `core.curve` and `core.policy`. This module reads
entities, converts units, calls the pure functions, performs at most one
`number.set_value` service call, and publishes a snapshot for the entities.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from math import isfinite
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_BOILER_RELAY_ENTITY,
    CONF_BURNER_POWER_ENTITY,
    CONF_CURRENT_FLOW_ENTITY,
    CONF_CYLINDER_TARGET_ENTITY,
    CONF_CYLINDER_TEMP_ENTITY,
    CONF_DESIGN_FLOW,
    CONF_DESIGN_OUTDOOR,
    CONF_DHW_DELTA,
    CONF_DHW_FALLBACK_FLOW,
    CONF_DHW_FLOW_MAX,
    CONF_DHW_FLOW_MIN,
    CONF_DHW_PROGRESS_MINUTES,
    CONF_DHW_RETURN_CEILING,
    CONF_DHW_TARGET,
    CONF_DHW_TIMEOUT_MINUTES,
    CONF_FLOW_MAX,
    CONF_FLOW_MIN,
    CONF_FLOW_SETPOINT_ENTITY,
    CONF_HEAT_DEMAND_ENTITY,
    CONF_HEATING_ACTIVE_ENTITY,
    CONF_HW_RELAY_DEMAND_ENTITY,
    CONF_INPUT_FRESHNESS_MINUTES,
    CONF_MANUAL_HOLD_MINUTES,
    CONF_MAX_FLOW_ENTITY,
    CONF_MIN_HOLD_MINUTES,
    CONF_OUTDOOR_FRESHNESS_MINUTES,
    CONF_OUTDOOR_TEMP_ENTITY,
    CONF_RETURN_CEILING,
    CONF_RETURN_TEMP_ENTITY,
    CONF_ROOM_CLIMATE_ENTITIES,
    CONF_ZONE_DEMAND_ENTITIES,
    DEFAULT_DESIGN_FLOW,
    DEFAULT_DESIGN_OUTDOOR,
    DEFAULT_DHW_DELTA,
    DEFAULT_DHW_FLOW_MAX,
    DEFAULT_DHW_FLOW_MIN,
    DEFAULT_DHW_PROGRESS_MINUTES,
    DEFAULT_DHW_RETURN_CEILING,
    DEFAULT_DHW_TARGET,
    DEFAULT_DHW_TIMEOUT_MINUTES,
    DEFAULT_FLOW_MAX,
    DEFAULT_FLOW_MIN,
    DEFAULT_INPUT_FRESHNESS_MINUTES,
    DEFAULT_MANUAL_HOLD_MINUTES,
    DEFAULT_MIN_HOLD_MINUTES,
    DEFAULT_OUTDOOR_FRESHNESS_MINUTES,
    DEFAULT_OVERRIDE,
    DEFAULT_RETURN_CEILING,
    DOMAIN,
    OVERRIDE_AUTO,
    ROOM_DESIGN_TEMP,
    UPDATE_INTERVAL_SECONDS,
)
from .core.control import ChargeMonitor, ControlState, DhwDemandTracker, effective_target
from .core.curve import (
    demand_correction_step,
    dhw_target,
    heating_curve,
    return_ceiling_step,
)
from .core.efficiency import (
    CONF_EFFICIENCY_PROFILE,
    PROFILE_DISABLED,
    STARTUP_MINUTES,
    STATUS_AWAITING_IGNITION,
    STATUS_BURNER_UNAVAILABLE,
    STATUS_DISABLED,
    STATUS_OFF,
    STATUS_RETURN_UNAVAILABLE,
    estimate,
)
from .core.efficiency import (
    MODEL_VERSION as EFFICIENCY_MODEL_VERSION,
)
from .core.model import (
    CurveParams,
    DhwCyclingState,
    DhwParams,
    HysteresisParams,
    ManualHoldParams,
    ManualHoldState,
    Mode,
    ReturnCeilingParams,
)
from .core.policy import (
    Action,
    Decision,
    ModeInputs,
    Override,
    decide_mode,
    decide_write,
    detect_manual_hold,
    infer_dhw_demand,
    zone_max_demand,
)
from .hub import BoilerFlowHub
from .store import BFCStore

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE = ("unknown", "unavailable", "", None)


@dataclass
class BFCCoordinatorData:
    """Snapshot published to entities after each cycle."""

    mode: str = Mode.OFF.value
    reason: str = ""
    action: str = Action.NONE.value
    enabled: bool = True
    override: str = DEFAULT_OVERRIDE
    last_run: datetime | None = None
    last_write: datetime | None = None
    last_written_setpoint: float | None = None
    last_target_change: datetime | None = None
    no_boiler: bool = False
    # Target
    flow_setpoint: float | None = None  # value we did/would write
    would_write: float | None = None
    curve: float | None = None
    demand_correction: float = 0.0
    return_correction: float = 0.0
    cycling_correction: float = 0.0
    demand_filtered: float | None = None
    cycles_10min: int = 0
    return_temperature_used: float | None = None
    return_fresh: bool = False
    manual_hold_active: bool = False
    dhw_issue_raised: bool = False
    # Raw inputs
    outdoor_temp: float | None = None
    current_flow: float | None = None
    heat_demand: float | None = None
    aggregate_heat_demand: float | None = None
    zone_max_demand: float | None = None
    hw_relay_demand: float | None = None
    cylinder_temp: float | None = None
    max_flow: float | None = None
    live_setpoint: float | None = None
    requested_target: float | None = None
    confirmed_setpoint: float | None = None
    write_status: str = "not_attempted"
    dhw_active: bool = False
    dhw_source: str = "none"
    cylinder_target: float | None = None
    dhw_charge_minutes: float = 0.0
    dhw_charge_starts: int = 0
    dhw_status: str = "idle"
    burner_power: float | None = None
    last_burn_seconds: float | None = None
    last_stop_reason: str = "unknown"
    cycling_status: str = "normal"
    room_correction: float = 0.0
    room_error: float | None = None
    # Diagnostics
    estimated_running_efficiency: int | None = None
    efficiency_status: str = STATUS_DISABLED
    efficiency_profile: str = PROFILE_DISABLED
    efficiency_return_temperature: float | None = None
    disabled_features: list[str] = field(default_factory=list)


class BFCCoordinator(DataUpdateCoordinator[BFCCoordinatorData]):
    """Single hub-style coordinator for Boiler Flow Control."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: BFCStore,
        hub: BoilerFlowHub,
        *,
        config=None,
        state_reader=None,
        write_guard=None,
        journal=None,
    ) -> None:
        self._cycle_lock = asyncio.Lock()
        self._entry = entry
        self._store = store
        self._hub = hub
        self.journal = journal
        self._journal_last: dict[str, Any] = {}
        # v0.2.1 review fix 9: options are the complete authoritative config once
        # set (the options flow always submits every field, absent ones cleared);
        # merging `{**entry.data, **entry.options}` let a key removed in options
        # resurface from the original `entry.data`.
        self._config: dict[str, Any] = (
            dict(config) if config is not None else dict(entry.options or entry.data)
        )
        self._state_reader = state_reader or hass.states.get
        self._write_guard = write_guard or (lambda: False)
        self._enabled = bool(store.get("enabled", True))
        self._override = str(
            store.get("mode_override", self._config.get("mode_override", DEFAULT_OVERRIDE))
        )
        self._tunables: dict[str, float] = {
            CONF_DESIGN_FLOW: float(store.get(CONF_DESIGN_FLOW, DEFAULT_DESIGN_FLOW)),
            CONF_DESIGN_OUTDOOR: float(store.get(CONF_DESIGN_OUTDOOR, DEFAULT_DESIGN_OUTDOOR)),
            CONF_RETURN_CEILING: float(store.get(CONF_RETURN_CEILING, DEFAULT_RETURN_CEILING)),
            CONF_DHW_DELTA: float(store.get(CONF_DHW_DELTA, DEFAULT_DHW_DELTA)),
        }
        self._auto_control = ControlState()
        self._shadow_control = ControlState()
        self._dhw_tracker = DhwDemandTracker()
        self._charge = ChargeMonitor()
        self._pending_since: datetime | None = None
        self._confirmed_target: float | None = None
        self._last_max_flow: float | None = None
        self._return_filtered: float | None = None
        self._return_filter_at: datetime | None = None
        self._manual_hold_state = ManualHoldState()
        self._prev_mode: Mode = Mode.OFF
        self._efficiency_interrupted_start: datetime | None = None
        self._efficiency_seen_start: datetime | None = None
        self._efficiency_observed_inputs = False
        super().__init__(
            hass,
            _LOGGER,
            config_entry=entry,
            name="Boiler Flow Control",
            update_interval=timedelta(seconds=UPDATE_INTERVAL_SECONDS),
        )

    # ------------------------------------------------------------------
    # Properties used by entities
    # ------------------------------------------------------------------

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        if value != self._enabled:
            self._auto_control = ControlState()
        self._enabled = value
        self._store.set("enabled", value)

    @property
    def override(self) -> str:
        return self._override

    @override.setter
    def override(self, value: str) -> None:
        if value != self._override:
            self._auto_control = ControlState()
            self._shadow_control = ControlState()
        self._override = value
        self._store.set("mode_override", value)

    async def async_save_settings(self) -> None:
        await self._store.async_save()

    def get_tunable(self, key: str) -> float:
        return self._tunables[key]

    def set_tunable(self, key: str, value: float) -> None:
        self._tunables[key] = float(value)
        self._store.set(key, float(value))

    def _opt(self, key: str, default: float) -> float:
        return float(self._config.get(key, default))

    # ------------------------------------------------------------------
    # Small HA helpers
    # ------------------------------------------------------------------

    def _state(self, entity_id: str | None):
        return self._state_reader(entity_id) if entity_id else None

    @property
    def _control(self) -> ControlState:
        return (
            self._auto_control
            if self._enabled and self._override == OVERRIDE_AUTO
            else self._shadow_control
        )

    @property
    def _demand_state(self):
        return self._control.demand

    @_demand_state.setter
    def _demand_state(self, value):
        self._control.demand = value

    @property
    def _return_state(self):
        return self._control.return_trim

    @_return_state.setter
    def _return_state(self, value):
        self._control.return_trim = value

    def _fresh(self, state, minutes: float, now: datetime) -> bool:
        reported = (state.last_reported or state.last_updated) if state is not None else None
        return reported is not None and timedelta(0) <= now - reported <= timedelta(minutes=minutes)

    @staticmethod
    def _to_celsius(value: float, unit: str | None) -> float | None:
        if unit in (None, "°C", "C"):
            return value
        if unit in ("°F", "F"):
            return (value - 32) * 5 / 9
        if unit == "K":
            return value - 273.15
        return None

    def _float_state(
        self,
        entity_id: str | None,
        *,
        temperature: bool = False,
        freshness: float | None = None,
        now: datetime | None = None,
        attribute: str | None = None,
    ) -> float | None:
        st = self._state(entity_id)
        if st is None or st.state in UNAVAILABLE:
            return None
        if freshness is not None and not self._fresh(st, freshness, now or dt_util.utcnow()):
            return None
        try:
            value = float(st.attributes.get(attribute) if attribute else st.state)
        except ValueError, TypeError:
            return None
        if not isfinite(value):
            return None
        if temperature:
            unit = st.attributes.get("unit_of_measurement", st.attributes.get("temperature_unit"))
            if unit is None and entity_id.split(".")[0] in ("climate", "water_heater"):
                unit = self.hass.config.units.temperature_unit
            value = self._to_celsius(value, unit)
            if value is None or not -60 <= value <= 120:
                return None
        return value

    def _is_on(self, entity_id: str | None, *, freshness: float | None = None) -> bool | None:
        st = self._state(entity_id)
        if (
            st is None
            or st.state in UNAVAILABLE
            or (freshness is not None and not self._fresh(st, freshness, dt_util.utcnow()))
        ):
            return None
        if st.state in ("on", "off"):
            return st.state == "on"
        value = self._float_state(entity_id)
        return value > 0 if value is not None and 0 <= value <= 100 else None

    def _setpoint_constraints(self, max_flow: float | None) -> tuple[float, float, float] | None:
        st = self._state(self._config.get(CONF_FLOW_SETPOINT_ENTITY))
        if st is None:
            return None
        unit = st.attributes.get("unit_of_measurement")
        try:
            lo = self._to_celsius(float(st.attributes.get("min", 5)), unit)
            hi = self._to_celsius(float(st.attributes.get("max", 90)), unit)
            step = float(st.attributes.get("step", 1)) * (5 / 9 if unit in ("°F", "F") else 1)
        except ValueError, TypeError:
            return None
        if lo is None or hi is None or not all(isfinite(v) for v in (lo, hi, step)):
            return None
        return lo, min(hi, max_flow) if max_flow is not None else hi, step

    # ------------------------------------------------------------------
    # Params
    # ------------------------------------------------------------------

    def _curve_params(self) -> CurveParams:
        return CurveParams(
            design_flow=self._tunables[CONF_DESIGN_FLOW],
            design_outdoor=self._tunables[CONF_DESIGN_OUTDOOR],
            room_design=ROOM_DESIGN_TEMP,
            flow_min=self._opt(CONF_FLOW_MIN, DEFAULT_FLOW_MIN),
            flow_max=self._opt(CONF_FLOW_MAX, DEFAULT_FLOW_MAX),
        )

    def _return_params(self) -> ReturnCeilingParams:
        return ReturnCeilingParams(return_ceiling=self._tunables[CONF_RETURN_CEILING])

    def _dhw_params(self) -> DhwParams:
        return DhwParams(
            dhw_delta=self._tunables[CONF_DHW_DELTA],
            dhw_flow_min=self._opt(CONF_DHW_FLOW_MIN, DEFAULT_DHW_FLOW_MIN),
            dhw_flow_max=self._opt(CONF_DHW_FLOW_MAX, DEFAULT_DHW_FLOW_MAX),
            dhw_return_ceiling=self._opt(CONF_DHW_RETURN_CEILING, DEFAULT_DHW_RETURN_CEILING),
        )

    def _hysteresis_params(self) -> HysteresisParams:
        return HysteresisParams(
            min_hold_minutes=self._opt(CONF_MIN_HOLD_MINUTES, DEFAULT_MIN_HOLD_MINUTES)
        )

    def _manual_hold_params(self) -> ManualHoldParams:
        return ManualHoldParams(
            hold_minutes=self._opt(CONF_MANUAL_HOLD_MINUTES, DEFAULT_MANUAL_HOLD_MINUTES)
        )

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Journal hooks: optional, defensive, never affect control
    # ------------------------------------------------------------------

    def _journal(self, kind: str, data: dict[str, Any], origin: str = "controller") -> None:
        journal = self.journal
        if journal is None:
            return
        try:
            journal.record(
                kind,
                scope="boiler",
                origin=origin,
                data=data,
                provenance={"model_version": EFFICIENCY_MODEL_VERSION},
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("BFC: journal hook failed", exc_info=True)

    def _journal_changed(self, key: str, value: Any) -> bool:
        marker = object()
        if self._journal_last.get(key, marker) == value:
            return False
        self._journal_last[key] = value
        return True

    async def _perform(self, decision: Decision) -> bool:
        """Perform the write. Returns True only on a confirmed successful call.

        v0.2.1 review fix 2: the call is now `blocking=True` and its result is
        reported to the caller so `_cycle` only records the write (and thus
        `last_target_change`/`last_written_at`) when it actually succeeded. A
        swallowed failure previously still recorded the write, which later
        read as a manual change (live != written, live != dial) and triggered
        a spurious 30-minute manual hold.
        """
        if decision.action is not Action.WRITE or decision.setpoint is None:
            return False
        command = {
            "action": decision.action.value,
            "setpoint": decision.setpoint,
            "reason": decision.reason,
            "target_changed": decision.target_changed,
            "service": "number.set_value",
        }
        self._journal("command_requested", {**command, "outcome": "requested"})
        if not self._write_guard():
            self._journal("command_result", {**command, "outcome": "blocked"})
            return False
        entity_id = self._config.get(CONF_FLOW_SETPOINT_ENTITY)
        if not entity_id:
            self._journal("command_result", {**command, "outcome": "no_actuator"})
            return False
        value = decision.setpoint  # already constrained; memory uses this same value
        st = self._state(entity_id)
        unit = st.attributes.get("unit_of_measurement") if st else None
        if unit in ("°F", "F"):
            value = value * 9 / 5 + 32
        elif unit == "K":
            value += 273.15
        try:
            await self.hass.services.async_call(
                "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
            )
            _LOGGER.debug("BFC: wrote %.1f to %s (%s)", value, entity_id, decision.reason)
            self._journal("command_sent", {**command, "outcome": "service_succeeded"})
            self._journal("command_result", {**command, "outcome": "service_succeeded"})
            return True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("BFC: number.set_value failed for %s", entity_id)
            self._journal(
                "command_result",
                {**command, "outcome": "service_failed", "error": type(exc).__name__},
            )
            return False

    # ------------------------------------------------------------------
    # Event-driven ignition counter (change 3)
    # ------------------------------------------------------------------

    def async_subscribe_heating_active(self):
        """Subscribe to state changes of the heating-active binary sensor so
        every off->on transition is counted as an ignition, however close
        together (the boiler can short-cycle faster than the 60 s poll: 6
        starts in 5 min have been observed, which a polled read undercounts).
        Returns the unsubscribe callable, or None if not configured; the
        caller (async_setup_entry) registers it with `entry.async_on_unload`.
        """
        entity_id = self._config.get(CONF_HEATING_ACTIVE_ENTITY)
        if not entity_id:
            return None
        stop_heating = async_track_state_change_event(
            self.hass, entity_id, self._handle_heating_active_event
        )
        if self._config.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED) == PROFILE_DISABLED:
            return stop_heating
        sources = list(
            dict.fromkeys(
                self._config[key]
                for key in (CONF_RETURN_TEMP_ENTITY, CONF_BURNER_POWER_ENTITY)
                if self._config.get(key)
            )
        )
        stop_sources = (
            async_track_state_change_event(self.hass, sources, self._handle_efficiency_event)
            if sources
            else None
        )

        @callback
        def unsubscribe():
            stop_heating()
            if stop_sources:
                stop_sources()

        return unsubscribe

    @callback
    def _handle_heating_active_event(self, event) -> None:
        """v0.2.1 review fixes 7-8: decorated `@callback` so HA runs this
        synchronously on the event loop instead of dispatching it to an
        executor thread, which raced the poll's prune of the same
        `_toggle_times` list. Only an exact off->on transition counts as an
        ignition — `unavailable`/`unknown` -> on and initial entity creation
        (no `old_state`) are not ignitions and previously inflated the counter
        on every HA/ems-esp restart."""
        new_state = event.data.get("new_state")
        old_state = event.data.get("old_state")
        now = dt_util.utcnow()
        if new_state is None or new_state.state not in ("on", "off"):
            self._hub.burn_started_at = None
            self._publish_efficiency()
            return
        if old_state is None or old_state.state not in ("on", "off"):
            return
        if old_state.state == "off" and new_state.state == "on":
            count = self._hub.record_ignition(now)
            if self._charge.started_at is not None and self._dhw_tracker.active:
                self._charge.starts += 1
            if self.data is not None:
                self.data.cycles_10min = count
        elif old_state.state == "on" and new_state.state == "off":
            self._hub.record_stop(
                now,
                self._is_on(self._config.get(CONF_BOILER_RELAY_ENTITY), freshness=5),
                self._float_state(
                    self._config.get(CONF_CURRENT_FLOW_ENTITY), temperature=True, freshness=5
                ),
                self._float_state(self._config.get(CONF_FLOW_SETPOINT_ENTITY), temperature=True),
            )

        self._publish_efficiency()

    @callback
    def _handle_efficiency_event(self, event) -> None:
        self._publish_efficiency()

    @callback
    def _publish_efficiency(self) -> None:
        if (
            self.data is not None
            and self._config.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED) != PROFILE_DISABLED
        ):
            self._update_efficiency(self.data, dt_util.utcnow())
            # Publishing diagnostics must not run another actuator/control cycle.
            self.async_update_listeners()

    def _update_efficiency(self, data: BFCCoordinatorData, now: datetime) -> None:
        profile = self._config.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED)
        data.efficiency_profile = profile
        data.estimated_running_efficiency = None
        data.efficiency_return_temperature = None
        if profile == PROFILE_DISABLED:
            data.efficiency_status = STATUS_DISABLED
            return
        start = self._hub.burn_started_at
        if start != self._efficiency_seen_start:
            self._efficiency_seen_start = start
            self._efficiency_interrupted_start = None
            self._efficiency_observed_inputs = False
        active = self._is_on(self._config.get(CONF_HEATING_ACTIVE_ENTITY))
        power_id = self._config.get(CONF_BURNER_POWER_ENTITY)
        return_id = self._config.get(CONF_RETURN_TEMP_ENTITY)
        power = self._float_state(power_id, freshness=5, now=now)
        ret = self._float_state(return_id, temperature=True, freshness=5, now=now)
        elapsed = (now - start).total_seconds() if start else None
        status = None
        if active is None or power is None or not 0 <= power <= 100:
            status = STATUS_BURNER_UNAVAILABLE
        elif active is False or power == 0:
            status = STATUS_OFF
        elif ret is None:
            status = STATUS_RETURN_UNAVAILABLE
        if status:
            # Allow initial source arrival ordering, not a gap after valid
            # firing evidence (even if that gap occurs during warmup).
            if start and (
                self._efficiency_observed_inputs
                or (elapsed is not None and elapsed >= STARTUP_MINUTES * 60)
            ):
                self._efficiency_interrupted_start = start
            data.efficiency_status = status
            return
        if start is None or start == self._efficiency_interrupted_start:
            data.efficiency_status = STATUS_AWAITING_IGNITION
            return
        for entity_id in (power_id, return_id):
            state = self._state(entity_id)
            if (state.last_reported or state.last_updated) < start:
                data.efficiency_status = "awaiting_current_burn_readings"
                return
        self._efficiency_observed_inputs = True
        result = estimate(
            profile,
            heating_active=active,
            burner_power=power,
            return_c=ret,
            burn_seconds=elapsed,
        )
        data.estimated_running_efficiency = result.percent
        data.efficiency_status = result.status
        data.efficiency_return_temperature = ret

    def async_subscribe_dhw(self):
        entity = self._config.get(CONF_HW_RELAY_DEMAND_ENTITY)
        return (
            async_track_state_change_event(self.hass, entity, self._handle_dhw_event)
            if entity
            else None
        )

    @callback
    def _handle_dhw_event(self, event) -> None:
        self._entry.async_create_background_task(
            self.hass, self.async_request_refresh(), "BFC DHW refresh"
        )

    async def async_reset_dhw_cycling(self) -> None:
        """Clear charge diagnostics while retaining the existing button ID."""
        self._hub.set_dhw_cycling(DhwCyclingState())
        self._charge = ChargeMonitor()
        ir.async_delete_issue(self.hass, DOMAIN, "dhw_charge_problem")
        await self._hub.async_save()
        ir.async_delete_issue(self.hass, DOMAIN, "dhw_cycling_unfixable")
        await self.async_request_refresh()

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    async def _async_update_data(self) -> BFCCoordinatorData:
        try:
            async with self._cycle_lock:
                data = await self._cycle()
                # Source events can arrive while control writes or storage await.
                self._update_efficiency(data, dt_util.utcnow())
                return data
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("BFC: update failed")
            raise UpdateFailed(str(exc)) from exc

    async def _cycle(self) -> BFCCoordinatorData:
        now = dt_util.utcnow()
        cfg = self._config
        control = self._control
        d = BFCCoordinatorData(enabled=self._enabled, override=self._override, last_run=now)
        disabled = d.disabled_features
        freshness = self._opt(CONF_INPUT_FRESHNESS_MINUTES, DEFAULT_INPUT_FRESHNESS_MINUTES)
        outdoor_freshness = self._opt(
            CONF_OUTDOOR_FRESHNESS_MINUTES, DEFAULT_OUTDOOR_FRESHNESS_MINUTES
        )

        def temp(key, age=freshness):
            return self._float_state(cfg.get(key), temperature=True, freshness=age, now=now)

        def demand(entity, age=freshness):
            value = self._float_state(entity, freshness=age, now=now)
            return value if value is not None and 0 <= value <= 100 else None

        d.live_setpoint = temp(CONF_FLOW_SETPOINT_ENTITY, None)
        d.outdoor_temp = temp(CONF_OUTDOOR_TEMP_ENTITY, outdoor_freshness)
        d.current_flow = temp(CONF_CURRENT_FLOW_ENTITY, 5)
        d.cylinder_temp = temp(CONF_CYLINDER_TEMP_ENTITY)
        d.max_flow = temp(
            CONF_MAX_FLOW_ENTITY, None
        )  # a static setting, not a periodic measurement
        d.burner_power = demand(cfg.get(CONF_BURNER_POWER_ENTITY), 5)
        if d.burner_power is not None and d.burner_power > 0:
            self._hub.last_burner_power = d.burner_power
        if d.outdoor_temp is None:
            disabled.append("heating curve disabled (outdoor temperature unavailable or stale)")
        if cfg.get(CONF_HEATING_ACTIVE_ENTITY) is None:
            disabled.append("cycling guard disabled (no heating-active sensor)")
        if d.burner_power is None:
            disabled.append("burner power unavailable")

        ret_state = self._state(cfg.get(CONF_RETURN_TEMP_ENTITY))
        ret = temp(CONF_RETURN_TEMP_ENTITY, 10)
        d.return_temperature_used, d.return_fresh = self._hub.sample_return(
            ret, (ret_state.last_reported or ret_state.last_updated) if ret_state else None, now
        )
        if not d.return_fresh or ret is None:
            d.return_fresh = False
            self._return_filtered, self._return_filter_at = None, None
            disabled.append("return ceiling disabled (return temperature unavailable or stale)")
        else:
            elapsed = (
                (now - self._return_filter_at).total_seconds() if self._return_filter_at else 0
            )
            self._return_filtered = (
                ret
                if self._return_filtered is None
                else self._return_filtered
                + max(0, elapsed) / (180 + max(0, elapsed)) * (ret - self._return_filtered)
            )
            self._return_filter_at = now

        zones = cfg.get(CONF_ZONE_DEMAND_ENTITIES) or []
        zone_values = [demand(e) for e in zones]
        all_zones_valid = bool(zones) and all(v is not None for v in zone_values)
        d.aggregate_heat_demand = demand(cfg.get(CONF_HEAT_DEMAND_ENTITY))
        d.zone_max_demand = zone_max_demand(zone_values)
        d.heat_demand = d.zone_max_demand if zones else d.aggregate_heat_demand
        demand_valid = all_zones_valid if zones else d.heat_demand is not None
        if not demand_valid:
            disabled.append("demand adaptation disabled (incomplete or stale demand inputs)")
        d.demand_filtered = self._hub.sample_demand(d.heat_demand if demand_valid else None, now)
        d.hw_relay_demand = demand(cfg.get(CONF_HW_RELAY_DEMAND_ENTITY))
        relay = d.hw_relay_demand > 0 if d.hw_relay_demand is not None else None
        inferred = infer_dhw_demand(
            False,
            bool(zones),
            d.aggregate_heat_demand,
            d.zone_max_demand,
            all_zones_valid=all_zones_valid,
        )
        definite_off = relay is False and (
            d.aggregate_heat_demand is not None and d.aggregate_heat_demand < 90
        )
        d.dhw_active = self._dhw_tracker.update(relay, inferred, definite_off, now)
        d.dhw_source = self._dhw_tracker.source
        if relay is None and not all_zones_valid:
            disabled.append("DHW detection degraded (relay or complete fresh zones required)")

        # The cylinder's requested temperature is a static setting. A live
        # climate/water_heater target can follow existing hygiene schedules.
        target_entity = cfg.get(CONF_CYLINDER_TARGET_ENTITY)
        attribute = (
            "temperature"
            if target_entity and target_entity.split(".")[0] in ("climate", "water_heater")
            else None
        )
        target_reading = self._float_state(target_entity, temperature=True, attribute=attribute)
        d.cylinder_target = (
            target_reading
            if target_reading is not None and 30 <= target_reading <= 90
            else self._opt(CONF_DHW_TARGET, DEFAULT_DHW_TARGET)
        )
        if target_entity and target_reading is None:
            disabled.append("cylinder target unavailable; using configured target")
        self._charge.update(
            d.dhw_active,
            d.cylinder_temp,
            d.cylinder_target,
            now,
            self._opt(CONF_DHW_PROGRESS_MINUTES, DEFAULT_DHW_PROGRESS_MINUTES),
            self._opt(CONF_DHW_TIMEOUT_MINUTES, DEFAULT_DHW_TIMEOUT_MINUTES),
        )
        d.dhw_charge_minutes = (
            (now - self._charge.started_at).total_seconds() / 60 if self._charge.started_at else 0
        )
        d.cycles_10min = self._hub.cycles_10min(now)
        d.dhw_charge_starts = self._charge.starts
        d.last_burn_seconds, d.last_stop_reason = (
            self._hub.last_burn_seconds,
            self._hub.last_stop_reason,
        )
        d.cycling_status = "frequent_starts_diagnostic_only" if d.cycles_10min >= 3 else "normal"

        self._update_efficiency(d, now)

        if d.live_setpoint is None:
            d.mode, d.no_boiler, d.reason = (
                "no_boiler",
                True,
                "flow setpoint unavailable or invalid",
            )
            return d

        # Confirm device readback separately from a successful HA service call.
        memory = self._hub.write_memory()
        if (
            memory.last_written_setpoint is not None
            and abs(d.live_setpoint - memory.last_written_setpoint) <= 0.5
        ):
            self._confirmed_target, self._pending_since = memory.last_written_setpoint, None
        elif (
            memory.last_written_setpoint is not None
            and self._confirmed_target is None
            and self._pending_since is None
        ):
            self._pending_since = now
        d.confirmed_setpoint = self._confirmed_target
        d.write_status = (
            "pending_readback"
            if self._pending_since
            else "confirmed"
            if self._confirmed_target is not None
            else "not_attempted"
        )
        if self._pending_since and now - self._pending_since >= timedelta(minutes=3):
            d.write_status = "unconfirmed_readback"
        manual = False
        if self._pending_since is None and self._confirmed_target is not None:
            manual, self._manual_hold_state = detect_manual_hold(
                d.live_setpoint,
                memory.last_written_setpoint,
                d.max_flow,
                now,
                self._manual_hold_state,
                self._manual_hold_params(),
            )
        d.manual_hold_active = manual
        if self._journal_changed("manual", manual):
            self._journal(
                "manual_override",
                {
                    "status": "set" if manual else "cleared",
                    "live_setpoint": d.live_setpoint,
                    "last_written_setpoint": memory.last_written_setpoint,
                    "dial_value": d.max_flow,
                    "detected_at": self._manual_hold_state.detected_at,
                    "outcome": "holding" if manual else "released",
                },
                origin="user" if manual else "controller",
            )
        mode = decide_mode(
            ModeInputs(
                self._enabled, bool(d.heat_demand and d.heat_demand > 0), d.dhw_active, manual
            )
        )
        physical_mode = decide_mode(
            ModeInputs(True, bool(d.heat_demand and d.heat_demand > 0), d.dhw_active, False)
        )
        operating = self._enabled and self._override != "hold" and not manual
        heating_active = operating and physical_mode is Mode.HEATING
        if heating_active:
            control.heating_since = control.heating_since or now
        else:
            control.heating_since = None

        rooms = {}
        for entity in cfg.get(CONF_ROOM_CLIMATE_ENTITIES) or []:
            st = self._state(entity)
            if st is None or st.state == "off" or st.attributes.get("hvac_action") == "off":
                continue
            current = self._float_state(
                entity,
                temperature=True,
                freshness=freshness,
                now=now,
                attribute="current_temperature",
            )
            requested = self._float_state(entity, temperature=True, attribute="temperature")
            if current is not None and requested is not None:
                rooms[entity] = (current, requested)
        d.room_correction, d.room_error = control.rooms.update(rooms, now, heating_active)
        control.demand = demand_correction_step(
            control.demand, d.demand_filtered if demand_valid else None, now, active=heating_active
        )
        # When rooms are configured they determine comfort pressure. Do not
        # assume a high valve percentage means a room is cold.
        room_configured = bool(cfg.get(CONF_ROOM_CLIMATE_ENTITIES))
        if room_configured and rooms:
            d.demand_correction = 0.0
        else:
            d.demand_correction = control.demand.correction
        comfort_limited = (d.room_error is not None and d.room_error > 0.3) or (
            not rooms and d.demand_filtered is not None and d.demand_filtered > 70
        )
        settled = (
            heating_active
            and control.heating_since is not None
            and now - control.heating_since >= timedelta(minutes=5)
        )
        # Require circulation evidence, not just a room asking for heat.
        circulating = self._is_on(cfg.get(CONF_HEATING_ACTIVE_ENTITY), freshness=5) is True
        curve_params = self._curve_params()
        d.curve = (
            heating_curve(d.outdoor_temp, curve_params) if d.outdoor_temp is not None else None
        )
        floor_limited = (
            d.curve is not None
            and d.curve + d.demand_correction + control.return_trim.correction
            <= curve_params.flow_min
        )
        if physical_mode in (Mode.DHW, Mode.DHW_AND_HEATING):
            # Keep the old heating trim separate; reset its time origin.
            control.return_trim = replace(control.return_trim, sampled_at=now)
        else:
            control.return_trim = return_ceiling_step(
                control.return_trim,
                self._return_filtered,
                d.return_fresh,
                self._return_params(),
                now=now,
                active=settled and circulating and not floor_limited,
                comfort_limited=comfort_limited,
            )
        d.return_correction = 0.0 if comfort_limited else control.return_trim.correction
        d.cycling_correction = 0.0
        heating_value = (
            max(
                curve_params.flow_min,
                min(
                    curve_params.flow_max,
                    d.curve + d.demand_correction + d.return_correction + d.room_correction,
                ),
            )
            if d.curve is not None
            else None
        )
        if d.dhw_active:
            params = self._dhw_params()
            fallback = self._opt(CONF_DHW_FALLBACK_FLOW, params.dhw_flow_max)
            target, _, _ = dhw_target(
                None if self._charge.fallback else d.cylinder_temp,
                None,
                False,
                0,
                DhwCyclingState(),
                now,
                params,
                cylinder_target=d.cylinder_target,
                fallback=fallback,
            )
            d.dhw_status = self._charge.reason or (
                "fallback_missing_temperature" if d.cylinder_temp is None else "charging"
            )
            d.return_correction = d.demand_correction = d.room_correction = 0.0
        else:
            target = d.curve if physical_mode is Mode.IDLE else heating_value
        d.requested_target = target

        constraints = self._setpoint_constraints(d.max_flow)
        if cfg.get(CONF_MAX_FLOW_ENTITY) and d.max_flow is None:
            disabled.append("configured maximum flow unavailable; writes suspended")
            target = None
        if constraints is None:
            disabled.append("invalid setpoint entity limits")
            target = None
        if target is not None:
            lo, hi, step = constraints
            regime_min = self._dhw_params().dhw_flow_min if d.dhw_active else curve_params.flow_min
            regime_max = self._dhw_params().dhw_flow_max if d.dhw_active else curve_params.flow_max
            # Entity grid is anchored at its native minimum, even when a
            # configured heating/DHW floor lies between grid points.
            target = effective_target(target, lo, min(hi, regime_max), step)
            if target is not None and target < regime_min:
                candidate = effective_target(regime_min + step / 2, lo, min(hi, regime_max), step)
                target = candidate if candidate is not None and candidate >= regime_min else None
            if target is None:
                disabled.append(
                    "configured flow range conflicts with boiler limits; writes suspended"
                )

        if d.dhw_active and (target is None or target < d.cylinder_target + 5):
            d.dhw_status = "insufficient_flow_headroom"
        d.dhw_issue_raised = d.dhw_active and d.dhw_status != "charging"
        if self._journal_changed("dhw", (d.dhw_active, d.dhw_status)):
            self._journal(
                "decision",
                {
                    "subject": "dhw",
                    "dhw_active": d.dhw_active,
                    "dhw_status": d.dhw_status,
                    "dhw_source": d.dhw_source,
                    "cylinder_temp": d.cylinder_temp,
                    "cylinder_target": d.cylinder_target,
                    "target": target,
                    "outcome": d.dhw_status if d.dhw_active else "dhw_inactive",
                },
                origin="source",
            )
        if self._enabled and self._override == OVERRIDE_AUTO:
            if d.dhw_issue_raised:
                ir.async_create_issue(
                    self.hass,
                    DOMAIN,
                    "dhw_charge_problem",
                    is_fixable=False,
                    severity=ir.IssueSeverity.WARNING,
                    translation_key="dhw_charge_problem",
                    translation_placeholders={"reason": d.dhw_status},
                )
            elif not d.dhw_active:
                ir.async_delete_issue(self.hass, DOMAIN, "dhw_charge_problem")

        was_dhw = control.previous_mode in (Mode.DHW, Mode.DHW_AND_HEATING)
        transition = was_dhw != d.dhw_active
        policy_memory = control.virtual_memory if self._override != OVERRIDE_AUTO else memory
        if self._override != OVERRIDE_AUTO and policy_memory.last_written_setpoint is None:
            policy_memory = memory
        bounded_old = None
        if constraints is not None and policy_memory.last_written_setpoint is not None:
            bounded_old = effective_target(
                policy_memory.last_written_setpoint,
                constraints[0],
                min(
                    constraints[1],
                    self._dhw_params().dhw_flow_max if d.dhw_active else curve_params.flow_max,
                ),
                constraints[2],
            )
        limits_changed = (
            policy_memory.last_written_setpoint is not None
            and bounded_old != policy_memory.last_written_setpoint
        )
        fallback_needed = d.dhw_active and (self._charge.fallback or d.cylinder_temp is None)
        completion_needed = (
            d.dhw_active
            and policy_memory.last_written_setpoint is not None
            and policy_memory.last_written_setpoint < d.cylinder_target + 5
        )
        decision = decide_write(
            mode,
            target,
            Override(self._override),
            policy_memory,
            now,
            exempt_hysteresis=transition
            or limits_changed
            or fallback_needed
            or completion_needed
            or d.max_flow != self._last_max_flow,
            hysteresis_params=self._hysteresis_params(),
        )
        d.mode, d.reason, d.action = mode.value, decision.reason, decision.action.value
        d.flow_setpoint = (
            decision.setpoint if decision.action is Action.WRITE else decision.would_write
        )
        d.would_write = decision.would_write
        if self._journal_changed("decision", (d.mode, d.action, d.reason, d.would_write)):
            self._journal(
                "decision",
                {
                    "mode": d.mode,
                    "action": d.action,
                    "reason": d.reason,
                    "target": d.would_write,
                    "override": self._override,
                    "bounds": list(constraints[:2]) if constraints else None,
                    "curve": d.curve,
                    "outdoor_temp": d.outdoor_temp,
                    "heat_demand": d.heat_demand,
                    "dhw_active": d.dhw_active,
                    "outcome": "shadow"
                    if self._override != OVERRIDE_AUTO
                    else "command_requested"
                    if d.action != Action.NONE.value
                    else "no_action",
                },
            )
        if self._override == "shadow" and operating and decision.would_write is not None:
            control.virtual_memory = replace(
                policy_memory,
                last_written_setpoint=decision.would_write,
                last_written_at=now,
                last_target_change=now
                if decision.target_changed
                else policy_memory.last_target_change,
            )
        wrote_ok = await self._perform(decision)
        if decision.action is Action.WRITE and wrote_ok:
            self._hub.record_write(decision.setpoint, now, decision.target_changed)
            readback = temp(CONF_FLOW_SETPOINT_ENTITY, None)
            if readback is not None and abs(readback - decision.setpoint) <= 0.5:
                self._confirmed_target, self._pending_since = decision.setpoint, None
                d.write_status = "confirmed"
            else:
                if decision.target_changed or self._pending_since is None:
                    self._pending_since = now
                d.write_status = (
                    "unconfirmed_readback"
                    if now - self._pending_since >= timedelta(minutes=3)
                    else "pending_readback"
                )
        elif decision.action is Action.WRITE:
            d.write_status = "service_failed"
        d.confirmed_setpoint = self._confirmed_target
        d.last_written_setpoint, d.last_write = (
            self._hub.last_written_setpoint,
            self._hub.last_written_at,
        )
        d.last_target_change = self._hub.last_target_change
        if self._journal_changed("readback", (d.write_status, d.confirmed_setpoint)):
            self._journal(
                "readback",
                {
                    "status": d.write_status,
                    "live_setpoint": d.live_setpoint,
                    "confirmed_setpoint": d.confirmed_setpoint,
                    "last_written_setpoint": d.last_written_setpoint,
                    "outcome": d.write_status,
                },
                origin="source",
            )
        if wrote_ok or (
            self._override == "shadow" and operating and decision.would_write is not None
        ):
            control.previous_mode = physical_mode
        self._prev_mode, self._last_max_flow = mode, d.max_flow
        await self._hub.async_save()
        return d
