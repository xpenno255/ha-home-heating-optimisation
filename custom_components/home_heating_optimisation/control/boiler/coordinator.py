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
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_state_report_event,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    ACTUATOR_CALL_TIMEOUT,
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
    CYCLE_TIMEOUT,
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
from .core.cycling import (
    START_LOSS_KWH,
    BurnTracker,
    add_burn,
    adjusted_efficiency,
    covered_days,
    fuel_fire_kwh,
    load_burns,
    summarise,
)
from .core.efficiency import (
    CONF_EFFICIENCY_PROFILE,
    NOMINAL_KW,
    PROFILE_DISABLED,
    STARTUP_MINUTES,
    STATUS_AWAITING_IGNITION,
    STATUS_BURNER_UNAVAILABLE,
    STATUS_DISABLED,
    STATUS_OFF,
    STATUS_RETURN_UNAVAILABLE,
    add_sample,
    aggregate,
    dump_buckets,
    estimate,
    load_buckets,
    prune,
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
from .core.warmup import (
    CATCHUP_MAX_FLOW,
    FullnessState,
    WarmupBoostParams,
    WarmupBoostState,
    boost_step,
    catchup_need,
    catchup_step,
    fullness,
    fullness_cap,
    fullness_step,
    house_demand,
    need_uplift,
    overshoot,
    plan_floor,
    preheat_step,
    return_scale,
    trim_factor,
    update_baselines,
)
from .dhw_policy import DEFAULTS as DHW_BOILER_DEFAULTS
from .dhw_policy import hot_cylinder_flow
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
    # Warm-up flow boost (mode off: untouched)
    warmup_mode: str = "off"
    house_demand: float | None = None
    room_equivalents: float | None = None
    warmup_boost: float = 0.0
    boost_flow: float | None = None
    boost_overshoot_k: float | None = None
    boost_overshoot_room: str | None = None
    boost_overshoot_baseline_k: float | None = None
    boost_trim: float = 1.0
    # Planned pre-heat flow (see core.warmup). `radiator_fullness` is always published while
    # heating; the rest only when pre-heat is on. In shadow they are what active would do.
    preheat_mode: str = "off"
    radiator_fullness: float | None = None
    fullness_cap: float | None = None
    preheat_need: float | None = None
    preheat_floor: float | None = None
    preheat_uplift: float = 0.0
    preheat_flow: float | None = None
    preheat_rooms: int = 0
    # Catch-up: a room stuck below target outside a pre-heat (`warmup_boost.catchup_mode`).
    # In shadow they are what active would do.
    catchup_mode: str = "off"
    catchup_room: str | None = None
    catchup_shortfall_k: float | None = None
    catchup_need: float | None = None
    catchup_uplift: float = 0.0
    catchup_flow: float | None = None
    # Anti-cycle override: disabled / normal / dhw / catchup / restoring / failed, the burn
    # minimum put back afterwards, and the DHW flow source (normal or hot_cylinder).
    dhw_anticycle_state: str = "disabled"
    dhw_anticycle_normal: float | None = None
    dhw_anticycle_value: float | None = None
    dhw_flow_source: str = "normal"
    # Diagnostics
    estimated_running_efficiency: int | None = None
    efficiency_status: str = STATUS_DISABLED
    efficiency_profile: str = PROFILE_DISABLED
    efficiency_return_temperature: float | None = None
    # Heat-weighted: the latest burn, and rolling windows (None without valid firing)
    last_burn_efficiency: float | None = None
    last_burn_ended_at: datetime | None = None
    estimated_efficiency_24h: float | None = None
    estimated_efficiency_7d: float | None = None
    efficiency_rolling: dict = field(default_factory=dict)
    # Burner cycling from the modulation readings, and the cycling-adjusted estimate
    burner_starts_24h: int | None = None
    mean_burn_minutes_24h: float | None = None
    estimated_efficiency_adjusted_24h: float | None = None
    estimated_efficiency_adjusted_7d: float | None = None
    cycling: dict = field(default_factory=dict)
    disabled_features: list[str] = field(default_factory=list)
    fallbacks: list[str] = field(default_factory=list)


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
        target_pending=None,
        warmup_config=None,
        warmup_rooms=None,
        preheat_config=None,
        preheat_rooms=None,
        dhw_boiler_config=None,
        dhw_elevated=None,
    ) -> None:
        self._cycle_lock = asyncio.Lock()
        self._target_pending = target_pending or (lambda: False)
        # Warm-up flow boost: options come from their own section, rooms from the controls.
        self._warmup_config = warmup_config or (lambda: {"mode": "off"})
        self._warmup_rooms = warmup_rooms or (lambda: [])
        self._warmup_state = WarmupBoostState()
        # Planned pre-heat: options, the rooms warming under a plan, and the filtered
        # radiator fullness (held while the burner is off) with the shadow uplift ramp.
        self._preheat_config = preheat_config or (lambda: {"mode": "off"})
        self._preheat_rooms = preheat_rooms or (lambda _now: [])
        self._dhw_boiler_config = dhw_boiler_config or (lambda: dict(DHW_BOILER_DEFAULTS))
        self._dhw_elevated = dhw_elevated or (lambda: False)
        # DHW anti-cycle override (runtime; the remembered value is persisted in the store).
        self._ac_state = "disabled"
        self._ac_reason = None
        self._ac_first = True
        self._ac_off_since: datetime | None = None
        self._ac_expected: float | None = None
        self._ac_written_at: datetime | None = None
        self._ac_retried = False
        self._ac_restoring = False
        self._ac_next_try: datetime | None = None
        self._fullness = FullnessState()
        self._preheat_state = WarmupBoostState()
        self._catchup_state = WarmupBoostState()
        self._catchup_tracks: dict = {}
        self._warmup_on = False
        self._warmup_journaled = 0.0
        # Overshoot of each room when the current boost began (memory only).
        self._warmup_baselines: dict[str, float] = {}
        # Keeps baselines after a trim ends the boost while the house still calls, so the
        # boost cannot restart against a room it has already overheated.
        self._warmup_hold = False
        self._warmup_stats = {
            "day": None,
            "minutes": 0.0,
            "peak_house_demand": 0.0,
            "peak_uplift": 0.0,
            "peak_overshoot": 0.0,
        }
        self._entry = entry
        self._store = store
        self._hub = hub
        self.journal = journal
        self._journal_last: dict[str, Any] = {}
        self._last_write_failure = "service_failed"
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
        # Rolling heat-weighted efficiency: 5-minute buckets kept in the boiler store.
        self._efficiency_buckets = load_buckets(store.get("efficiency_buckets"))
        self._burns = load_burns(store.get("burn_log"))
        self._burn_tracker = BurnTracker()
        self._efficiency_sample: tuple[float, float] | None = None
        self._efficiency_last_at: datetime | None = None
        self._burn_start: datetime | None = None
        self._burn_weight = self._burn_weighted = 0.0
        self._burn_last_at: datetime | None = None
        ended = store.get("last_burn_ended_at")
        parsed = dt_util.parse_datetime(str(ended)) if ended else None
        self._last_burn: tuple[float | None, datetime | None] = (
            store.get("last_burn_efficiency"),
            dt_util.as_utc(parsed) if parsed else None,
        )
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

    # ------------------------------------------------------------------
    # DHW anti-cycle override
    # ------------------------------------------------------------------

    def _ac_journal(self, event, data=None):
        self._journal("dhw_anticycle", {"event": event, "outcome": event, **(data or {})})

    async def _ac_write(self, entity, value, now, event, reason=None) -> bool:
        """One ``number.set_value``; records what is expected back. False when not sent."""
        if not self._write_guard():
            self._ac_journal("blocked", {"entity": entity, "value": value, "action": event})
            return False
        try:
            async with asyncio.timeout(ACTUATOR_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": entity, "value": value}, blocking=True
                )
        except Exception as exc:  # noqa: BLE001 - a failed write is retried, never raised
            self._ac_journal(
                "failed", {"entity": entity, "value": value, "error": type(exc).__name__}
            )
            return False
        self._ac_expected, self._ac_written_at = value, now
        self._ac_journal(
            event, {"entity": entity, "value": value, **({"reason": reason} if reason else {})}
        )
        return True

    async def _dhw_anticycle(self, d, now) -> None:
        """Lower the boiler's minimum burn period for a hot water charge or a room catch-up,
        then put it back.

        The remembered normal value and a pending-restore flag are persisted, so a restart
        in the middle of either still restores it. Restoring needs only the write guard;
        lowering needs the boiler in auto as well. It is only ever lowered, never raised
        above the normal value.
        """
        cfg = self._dhw_boiler_config()
        store = self._store
        pending = bool(store.get("anticycle_pending"))
        dhw_on = cfg["anticycle_enabled"]
        catchup_on = cfg["catchup_minutes"] is not None
        enabled = bool((dhw_on or catchup_on) and cfg["anticycle_entity"])
        entity = cfg["anticycle_entity"] if enabled else store.get("anticycle_entity")
        first, self._ac_first = self._ac_first, False
        want, reason = None, None
        if enabled and d.dhw_active and dhw_on:
            want, reason = float(cfg["anticycle_minutes"]), "dhw"
        elif enabled and catchup_on and d.catchup_mode == "active" and d.catchup_uplift > 0:
            want, reason = float(cfg["catchup_minutes"]), "catchup"
        d.dhw_anticycle_normal = store.get("anticycle_normal")
        d.dhw_anticycle_value = (
            want if want is not None else float(cfg["anticycle_minutes"]) if dhw_on else None
        )
        if not enabled and not pending:
            self._ac_state = d.dhw_anticycle_state = "disabled"
            return
        current = self._float_state(entity, now=now)
        # Confirm or retry the last write.
        if self._ac_expected is not None:
            if current is not None and abs(current - self._ac_expected) < 0.01:
                if self._ac_restoring:
                    store.set("anticycle_pending", False)
                    pending = False
                self._ac_expected, self._ac_restoring = None, False
                self._ac_journal("confirmed", {"entity": entity, "value": current})
                await store.async_save()
            elif now - self._ac_written_at >= timedelta(minutes=2):
                if not self._ac_retried:
                    self._ac_retried = True
                    self._ac_journal("warning", {"entity": entity, "expected": self._ac_expected})
                    await self._ac_write(entity, self._ac_expected, now, "retry")
                else:
                    self._ac_journal("failed", {"entity": entity, "expected": self._ac_expected})
                    self._ac_expected, self._ac_state = None, "failed"
                    self._ac_next_try = now + timedelta(minutes=10)
        waiting = self._ac_next_try is not None and now < self._ac_next_try
        if want is not None:
            self._ac_off_since = None
            normal = store.get("anticycle_normal") if pending else current
            target = want if normal is None else min(want, normal)
            if (
                self._ac_expected is None
                and not waiting
                and self._enabled
                and self._override == OVERRIDE_AUTO
                and current is not None
                and abs(current - target) > 0.01
            ):
                if not pending:
                    store.set("anticycle_normal", current)
                    store.set("anticycle_entity", entity)
                    store.set("anticycle_pending", True)
                    await store.async_save()
                    pending = True
                # Also moves between the charge and catch-up values without a restore.
                self._ac_retried = self._ac_restoring = False
                if await self._ac_write(entity, target, now, "set", reason):
                    self._ac_state = reason
                else:
                    self._ac_next_try = now + timedelta(minutes=10)
            if self._ac_state != "failed" and (pending or store.get("anticycle_pending")):
                self._ac_state = self._ac_reason = reason
        elif pending:
            if self._ac_off_since is None:
                self._ac_off_since = now
            normal = store.get("anticycle_normal")
            if normal is None:
                store.set("anticycle_pending", False)
                self._ac_state = "normal"
            elif (
                self._ac_expected is None
                and not waiting
                and (first or now - self._ac_off_since >= timedelta(minutes=2))
            ):
                if current is not None and abs(current - normal) < 0.01:
                    store.set("anticycle_pending", False)
                    self._ac_state = "normal"
                    await store.async_save()
                else:
                    self._ac_retried, self._ac_restoring = False, True
                    if await self._ac_write(entity, normal, now, "restore"):
                        self._ac_state = "restoring"
                    else:
                        self._ac_state = "failed"
                        self._ac_next_try = now + timedelta(minutes=10)
            elif self._ac_state not in ("failed", "restoring"):
                self._ac_state = self._ac_reason or "dhw"
        else:
            # Outside a charge or catch-up the entity's value is the normal one, even when
            # the user changes it, unless a write of ours is still being confirmed.
            if self._ac_state != "failed" or not waiting:
                self._ac_state = "normal"
            if (
                enabled
                and self._ac_expected is None
                and current is not None
                and store.get("anticycle_normal") != current
            ):
                store.set("anticycle_normal", current)
                store.set("anticycle_entity", entity)
                await store.async_save()
        d.dhw_anticycle_state = self._ac_state
        d.dhw_anticycle_normal = store.get("anticycle_normal")

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
    # Warm-up flow boost
    # ------------------------------------------------------------------

    def _warmup_boost(self, d, now, physical_mode, base, curve_params, freshness) -> float:
        """Publish the boost; returns the uplift to add to the target.

        Two drivers. The house-demand boost (``warmup_boost`` mode) is the fallback. While
        any room is pre-heating under a plan and pre-heat is on, the uplift instead follows
        the rooms' lag behind their plans, capped by how full the radiators already are; in
        pre-heat ``shadow`` that is only published (``preheat_*``) and the house-demand
        boost carries on as before.
        """
        cfg = self._warmup_config()
        mode = d.warmup_mode = cfg["mode"]
        catch_mode = d.catchup_mode = cfg.get("catchup_mode", "off")
        pre_mode = d.preheat_mode = self._preheat_config()["mode"]
        heating = base is not None and physical_mode is Mode.HEATING
        rooms = []
        hot = []
        catch_rooms = []
        if heating:
            for entity, output, enabled, name, air, target, *window in self._warmup_rooms():
                if not enabled:
                    continue
                value = self._float_state(
                    entity, freshness=freshness, now=now, attribute="heat_demand"
                )
                value = value if value is not None and 0 <= value <= 1 else None
                hot.append((name, air, target, value))
                catch_rooms.append((name, air, target, value, window[0] if window else True))
                rooms.append((value, output))
        else:
            # Radiators cool while the boiler does something else: start the filter afresh.
            self._fullness = FullnessState()
        if not heating or (mode == "off" and pre_mode == "off" and catch_mode == "off"):
            if self._warmup_on:
                self._warmup_journal("ended", d)
            self._warmup_state, self._warmup_on, self._warmup_journaled = (
                WarmupBoostState(),
                False,
                0.0,
            )
            self._preheat_state = WarmupBoostState()
            self._catchup_state, self._catchup_tracks = WarmupBoostState(), {}
            self._warmup_baselines = {}
            self._warmup_hold = False
            if heating:
                self._publish_fullness(d, now, hot)
            return 0.0
        house = house_demand(rooms)
        pre = self._preheat_config()
        params = WarmupBoostParams(
            start=cfg["start"],
            full=cfg["full"],
            boost_max=curve_params.flow_max,
            decay_k_per_min=cfg["decay_k_per_min"],
            fullness_low=pre.get("fullness_low", 0.45),
            fullness_high=pre.get("fullness_high", 0.65),
        )
        filtered = self._publish_fullness(d, now, hot)
        scale = (
            return_scale(self._return_filtered, self._return_params().return_ceiling, params)
            if mode == "active"
            else 1.0
        )
        calling = house is not None and house.fraction >= params.start
        if not calling:
            self._warmup_hold = False
        episode = self._warmup_on or self._warmup_hold
        self._warmup_baselines = update_baselines(hot, self._warmup_baselines, episode)
        worst = overshoot(hot, self._warmup_baselines)
        trim = trim_factor(worst[0] if worst else None, params)
        previous = self._warmup_state
        planned = []
        if pre_mode != "off" and not d.dhw_active:
            try:
                planned = list(self._preheat_rooms(now))
            except Exception:  # noqa: BLE001 - the plan must never break the boiler cycle
                _LOGGER.debug("BFC: pre-heat rooms unavailable", exc_info=True)
        d.preheat_rooms = len(planned)
        pre_driving = bool(planned)
        applied = mode == "active"
        if pre_mode != "off":
            guard = return_scale(
                self._return_filtered, self._return_params().return_ceiling, params
            )
            need = need_uplift(planned, base, params.boost_max, params)
            cap = fullness_cap(
                filtered, base, params.boost_max, params.fullness_low, params.fullness_high
            )
            floor = plan_floor(planned, base, pre.get("plan_flow"), params.boost_max)
            # A room behind its plan outranks one running warm: the trim waits until none is.
            wanted = max(floor, min(need, cap) * (1.0 if need > 0 else trim)) * guard
            wanted = wanted if pre_driving else 0.0
            before = self._preheat_state
            if pre_mode == "active" and pre_driving:
                # The plan, not the house demand, sets the flow while rooms are pre-heating.
                state = preheat_step(previous, wanted, base, now, params)
                before, applied = state, True
            else:
                before = preheat_step(before, wanted, base, now, params)
                state = None
            self._preheat_state = before
            d.preheat_need = round(need, 2) if pre_driving else None
            d.preheat_floor = round(floor, 2) if pre_driving else None
            d.fullness_cap = round(cap, 2)
            d.preheat_uplift = round(before.uplift, 2)
            d.preheat_flow = round(min(curve_params.flow_max, base + before.uplift), 1)
        else:
            state = None
            self._preheat_state = WarmupBoostState()
        if state is None:
            if mode != "off":
                state = boost_step(
                    previous, house.fraction if house else None, base, now, params, scale, trim
                )
            else:
                # Only pre-heat (or catch-up) is on and no room is warming: fall away at the
                # decay rate.
                state = preheat_step(previous, 0.0, base, now, params)
                applied = pre_mode == "active"
        catch = self._catchup(d, now, catch_rooms, pre_driving, base, curve_params, params, trim)
        if catch_mode == "active" and catch.uplift > 0 and not pre_driving:
            # A room stuck below target outranks a smaller (or only shadowed) house boost.
            if not applied or catch.uplift > state.uplift:
                state, applied = catch, True
        self._warmup_state = state
        d.house_demand = None if house is None else round(house.fraction, 3)
        d.room_equivalents = None if house is None else round(house.room_equivalents, 2)
        d.warmup_boost = round(state.uplift, 2)
        d.boost_overshoot_k = None if worst is None else round(worst[0], 2)
        d.boost_overshoot_room = None if worst is None else worst[1]
        d.boost_overshoot_baseline_k = None if worst is None else round(worst[2], 2)
        d.boost_trim = round(trim, 2)
        d.boost_flow = round(min(curve_params.flow_max, base + state.uplift), 1)
        stats = self._warmup_stats
        today = dt_util.as_local(now).date()
        if stats["day"] != today:
            stats.update(
                day=today, minutes=0.0, peak_house_demand=0.0, peak_uplift=0.0, peak_overshoot=0.0
            )
        if state.uplift > 0 and previous.updated_at is not None:
            stats["minutes"] += min(
                10.0, max(0.0, (now - previous.updated_at).total_seconds() / 60)
            )
        stats["peak_house_demand"] = max(stats["peak_house_demand"], d.house_demand or 0.0)
        stats["peak_uplift"] = max(stats["peak_uplift"], d.warmup_boost)
        stats["peak_overshoot"] = max(stats["peak_overshoot"], d.boost_overshoot_k or 0.0)
        if d.warmup_boost > 0 and not self._warmup_on:
            self._warmup_on, self._warmup_journaled = True, d.warmup_boost
            self._warmup_journal("started", d)
        elif d.warmup_boost <= 0 and self._warmup_on:
            self._warmup_on, self._warmup_journaled = False, 0.0
            # Baselines stay while the house still calls (see _warmup_hold).
            self._warmup_hold = calling
            if not calling:
                self._warmup_baselines = {}
            self._warmup_journal("ended", d)
        elif self._warmup_on and abs(d.warmup_boost - self._warmup_journaled) >= 2.0:
            self._warmup_journaled = d.warmup_boost
            self._warmup_journal("updated", d, routine=True)
        return state.uplift if applied else 0.0

    def _catchup(self, d, now, rooms, pre_driving, base, curve_params, params, trim):
        """Track rooms stuck below target and publish the catch-up; returns its state.

        Not while a planned pre-heat drives the flow. The uplift is capped by radiator
        fullness, faded by the overshoot trim and the return guard, and rises at most
        ``rise_k_per_min``.
        """
        if d.catchup_mode == "off" or pre_driving or d.dhw_active:
            self._catchup_tracks = {}
        else:
            self._catchup_tracks = catchup_step(self._catchup_tracks, rooms, now)
        ceiling = min(curve_params.flow_max, CATCHUP_MAX_FLOW)
        need, room, short = catchup_need(self._catchup_tracks, rooms, base, ceiling)
        guard = return_scale(self._return_filtered, self._return_params().return_ceiling, params)
        cap = fullness_cap(
            self._fullness.value, base, params.boost_max, params.fullness_low, params.fullness_high
        )
        wanted = min(need, cap) * trim * guard
        was_pushing = d.catchup_mode != "off" and self._catchup_state.uplift > 0
        self._catchup_state = preheat_step(self._catchup_state, wanted, base, now, params)
        d.catchup_room = room
        d.catchup_shortfall_k = None if short is None else round(short, 2)
        d.catchup_need = round(need, 2)
        d.catchup_uplift = round(self._catchup_state.uplift, 2)
        d.catchup_flow = round(min(curve_params.flow_max, base + self._catchup_state.uplift), 1)
        if room is not None and not was_pushing and self._catchup_state.uplift > 0:
            self._warmup_journal("catchup_started", d)
        elif was_pushing and self._catchup_state.uplift <= 0:
            self._warmup_journal("catchup_ended", d)
        return self._catchup_state

    def _publish_fullness(self, d, now, hot):
        """Update and publish the filtered radiator fullness; returns it (None if unknown).

        Flow is the current flow reading, return the fresh return reading, room the mean air
        temperature of rooms calling for heat (else of all heated rooms). Only a firing burner
        moves the filter: the return jumps towards the flow once it stops.
        """
        cfg = self._config
        calling = [air for _n, air, _t, demand in hot if air is not None and (demand or 0) > 0]
        airs = calling or [air for _n, air, _t, _d in hot if air is not None]
        room = sum(airs) / len(airs) if airs else None
        ret = d.return_temperature_used if d.return_fresh else None
        raw = fullness(d.current_flow, ret, room)
        active = self._state(cfg.get(CONF_HEATING_ACTIVE_ENTITY))
        firing = (d.burner_power or 0) > 0 or (active is not None and active.state == "on")
        self._fullness = fullness_step(self._fullness, raw, firing, now)
        value = self._fullness.value
        d.radiator_fullness = None if value is None else round(value, 3)
        return value

    def _warmup_journal(self, event, d, routine=False):
        self._journal(
            "warmup_boost",
            {
                "event": event,
                "outcome": event,
                "mode": d.warmup_mode,
                "house_demand": d.house_demand,
                "room_equivalents": d.room_equivalents,
                "uplift": d.warmup_boost,
                "boost_flow": d.boost_flow,
                "overshoot_k": d.boost_overshoot_k,
                "overshoot_room": d.boost_overshoot_room,
                "overshoot_baseline_k": d.boost_overshoot_baseline_k,
                "trim": d.boost_trim,
                "return_used": self._return_filtered,
                "preheat_mode": d.preheat_mode,
                "preheat_rooms": d.preheat_rooms,
                "preheat_need": d.preheat_need,
                "preheat_floor": d.preheat_floor,
                "fullness_cap": d.fullness_cap,
                "radiator_fullness": d.radiator_fullness,
                "preheat_uplift": d.preheat_uplift,
                "catchup_mode": d.catchup_mode,
                "catchup_room": d.catchup_room,
                "catchup_shortfall_k": d.catchup_shortfall_k,
                "catchup_need": d.catchup_need,
                "catchup_uplift": d.catchup_uplift,
            },
            routine=routine,
        )

    def warmup_report(self):
        stats = self._warmup_stats
        data = self.data
        return {
            "mode": self._warmup_config()["mode"],
            "house_demand": data.house_demand if data else None,
            "uplift": data.warmup_boost if data else 0.0,
            "minutes_in_boost_today": round(stats["minutes"], 1),
            "peak_house_demand": stats["peak_house_demand"],
            "peak_uplift": stats["peak_uplift"],
            "overshoot_k": data.boost_overshoot_k if data else None,
            "overshoot_room": data.boost_overshoot_room if data else None,
            "trim": data.boost_trim if data else 1.0,
            "preheat_mode": self._preheat_config()["mode"],
            "preheat_uplift": data.preheat_uplift if data else 0.0,
            "radiator_fullness": data.radiator_fullness if data else None,
            "peak_overshoot_today": stats["peak_overshoot"],
            "catchup_mode": self._warmup_config().get("catchup_mode", "off"),
            "catchup_room": data.catchup_room if data else None,
            "catchup_shortfall_k": data.catchup_shortfall_k if data else None,
            "catchup_uplift": data.catchup_uplift if data else 0.0,
            "catchup_flow": data.catchup_flow if data else None,
        }

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Journal hooks: optional, defensive, never affect control
    # ------------------------------------------------------------------

    def _journal(
        self,
        kind: str,
        data: dict[str, Any],
        origin: str = "controller",
        routine: bool = False,
    ) -> None:
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
                routine=routine,
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
        # The 60 s re-assertion of an unchanged target is routine: only its failures
        # are kept at the standard journal level.
        routine = not decision.target_changed
        self._journal("command_requested", {**command, "outcome": "requested"}, routine=routine)
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
            async with asyncio.timeout(ACTUATOR_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    "number", "set_value", {"entity_id": entity_id, "value": value}, blocking=True
                )
            _LOGGER.debug("BFC: wrote %.1f to %s (%s)", value, entity_id, decision.reason)
            self._journal(
                "command_sent", {**command, "outcome": "service_succeeded"}, routine=routine
            )
            self._journal(
                "command_result", {**command, "outcome": "service_succeeded"}, routine=routine
            )
            return True
        except TimeoutError:
            self._last_write_failure = "service_timeout"
            _LOGGER.warning(
                "BFC: number.set_value for %s did not return within %.0f s; treating as failed",
                entity_id,
                ACTUATOR_CALL_TIMEOUT,
            )
            self._journal("command_result", {**command, "outcome": "service_timeout"})
            return False
        except Exception as exc:  # noqa: BLE001
            self._last_write_failure = "service_failed"
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
        self._efficiency_sample = None
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
        if result.exact is not None:
            self._efficiency_sample = (power, result.exact)
        data.efficiency_return_temperature = ret

    def _accumulate_efficiency(self, d: BFCCoordinatorData, now: datetime) -> None:
        """Once per control cycle: bucket the valid estimate and publish the rolling figures."""
        sample, last_at = self._efficiency_sample, self._efficiency_last_at
        start = self._hub.burn_started_at
        if start != self._burn_start:
            if self._burn_start is not None and self._burn_weight > 0:
                self._last_burn = (
                    round(self._burn_weighted / self._burn_weight, 1),
                    self._burn_last_at,
                )
                self._store.set("last_burn_efficiency", self._last_burn[0])
                self._store.set("last_burn_ended_at", self._burn_last_at.isoformat())
            self._burn_start = start
            self._burn_weight = self._burn_weighted = 0.0
        if sample is None:
            self._efficiency_last_at = None
        else:
            power, efficiency = sample
            self._efficiency_last_at = now
            if last_at is not None:
                elapsed = min((now - last_at).total_seconds(), 120.0)
                mode = "dhw" if d.dhw_active else "heating"
                add_sample(self._efficiency_buckets, now, elapsed, power, efficiency, mode)
                self._burn_weight += power * elapsed
                self._burn_weighted += power * elapsed * efficiency
                self._burn_last_at = now
                prune(self._efficiency_buckets, now)
                self._store.set("efficiency_buckets", dump_buckets(self._efficiency_buckets))
        if self._burn_weight > 0:
            d.last_burn_efficiency = round(self._burn_weighted / self._burn_weight, 1)
        else:
            d.last_burn_efficiency, d.last_burn_ended_at = self._last_burn
        windows = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
        d.efficiency_rolling = {
            name: aggregate(self._efficiency_buckets, now, window)
            for name, window in windows.items()
        }
        d.estimated_efficiency_24h = d.efficiency_rolling["24h"]["percent"]
        d.estimated_efficiency_7d = d.efficiency_rolling["7d"]["percent"]
        self._update_cycling(d, now)

    def async_subscribe_burns(self):
        """Record every burn from the burner modulation entity's state changes.

        Event driven, not on the 60 s cycle: the burner can start and stop within a minute.
        Reports of an unchanged value count too: a steady modulation sends no state change,
        and must not look like a gap. Returns the unsubscribe callable, or None when no
        burner entity is configured.
        """
        entity_id = self._config.get(CONF_BURNER_POWER_ENTITY)
        if not entity_id:
            return None
        stops = (
            async_track_state_change_event(self.hass, entity_id, self._handle_burn_event),
            async_track_state_report_event(self.hass, entity_id, self._handle_burn_event),
        )

        @callback
        def unsubscribe():
            for stop in stops:
                stop()

        return unsubscribe

    @callback
    def _handle_burn_event(self, event) -> None:
        new_state = event.data.get("new_state") or self._state(event.data.get("entity_id"))
        power = None
        if new_state is not None and new_state.state not in UNAVAILABLE:
            try:
                power = float(new_state.state)
            except ValueError:
                power = None
        now = dt_util.utcnow()
        dhw = bool(self.data.dhw_active) if self.data is not None else False
        row = self._burn_tracker.update(now, power, dhw)
        if row is not None:
            add_burn(self._burns, row, now)
            self._store.set("burn_log", [list(b) for b in self._burns])
            if self.data is not None:
                self._update_cycling(self.data, now)
                self.async_update_listeners()

    def _update_cycling(self, d: BFCCoordinatorData, now: datetime) -> None:
        """Cycling aggregates and the cycling-adjusted efficiency for 24 h and 7 d."""
        nominal = NOMINAL_KW.get(self._config.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED))
        windows = {"24h": timedelta(hours=24), "7d": timedelta(days=7)}
        cycling = {}
        for name, window in windows.items():
            figures = summarise(self._burns, now, window)
            fuel = fuel_fire_kwh(self._burns, now, window, nominal) if nominal else None
            firing = (d.efficiency_rolling.get(name) or {}).get("percent")
            figures.update(
                firing_efficiency=firing,
                fuel_fire_kwh=None if fuel is None else round(fuel, 2),
                start_loss_kwh_assumed=START_LOSS_KWH,
                adjusted_efficiency=adjusted_efficiency(firing, fuel, figures["starts"]),
            )
            cycling[name] = figures
        days = covered_days(self._burns, now, windows["7d"])
        cycling["7d"].update(
            starts_per_day=round(cycling["7d"]["starts"] / days, 1), days_covered=round(days, 1)
        )
        d.cycling = cycling
        recorded = bool(self._burns)
        d.burner_starts_24h = cycling["24h"]["starts"] if recorded else None
        d.mean_burn_minutes_24h = cycling["24h"]["mean_burn_minutes"]
        d.estimated_efficiency_adjusted_24h = cycling["24h"]["adjusted_efficiency"]
        d.estimated_efficiency_adjusted_7d = cycling["7d"]["adjusted_efficiency"]

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
            # The lock is released by ``async with`` whichever way the cycle ends,
            # including the cancellation that asyncio.timeout uses to enforce the bound.
            async with self._cycle_lock:
                async with asyncio.timeout(CYCLE_TIMEOUT):
                    data = await self._cycle()
                # Source events can arrive while control writes or storage await.
                self._update_efficiency(data, dt_util.utcnow())
                return data
        except TimeoutError:
            _LOGGER.error("BFC: control cycle exceeded %.0f s and was abandoned", CYCLE_TIMEOUT)
            self._journal("decision", {"outcome": "cycle_timeout", "timeout_s": CYCLE_TIMEOUT})
            if self.data is not None:
                note = "cycle timed out; showing last good data"
                if note not in self.data.fallbacks:
                    self.data.fallbacks = [*self.data.fallbacks, note]
                return self.data
            raise UpdateFailed("control cycle timed out") from None
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
        if self._target_pending():
            # A requested target is not confirmed until the controller's reply is observed.
            disabled.append("cylinder target change requested; controller reply not yet confirmed")
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
        self._accumulate_efficiency(d, now)

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
        uplift = self._warmup_boost(d, now, physical_mode, heating_value, curve_params, freshness)
        if uplift and heating_value is not None:
            heating_value = min(curve_params.flow_max, heating_value + uplift)
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
            hot = self._dhw_boiler_config()
            target, d.dhw_flow_source = hot_cylinder_flow(
                target, d.cylinder_temp, self._dhw_elevated(), hot["hot_flow"], hot["hot_threshold"]
            )
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
            d.write_status = self._last_write_failure
        d.confirmed_setpoint = self._confirmed_target
        d.last_written_setpoint, d.last_write = (
            self._hub.last_written_setpoint,
            self._hub.last_written_at,
        )
        d.last_target_change = self._hub.last_target_change
        if d.write_status != "not_attempted" and self._journal_changed(
            "readback", (d.write_status, d.confirmed_setpoint)
        ):
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
        await self._dhw_anticycle(d, now)
        await self._hub.async_save()
        return d
