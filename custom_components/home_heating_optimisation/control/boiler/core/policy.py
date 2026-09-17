"""Mode decision table, manual-hold detection, and the write-or-not decision
(spec v0.1 §3.1, §3.2.6, §4).

Pure functions over plain inputs. The coordinator gathers the inputs from Home
Assistant, calls `decide_mode` then `decide_write`, and performs at most one
`number.set_value` call.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum

from .curve import should_write
from .model import HysteresisParams, ManualHoldParams, ManualHoldState, Mode, WriteMemory


class Action(str, Enum):
    NONE = "none"
    WRITE = "write"


class Override(str, Enum):
    AUTO = "auto"
    SHADOW = "shadow"
    HOLD = "hold"


@dataclass(frozen=True)
class ModeInputs:
    enabled: bool
    heat_demand: bool
    dhw_demand: bool
    manual_hold_active: bool


@dataclass(frozen=True)
class Decision:
    mode: Mode
    action: Action
    setpoint: float | None  # value to write when action is WRITE
    reason: str
    would_write: float | None  # what would be written if override were "auto"
    memory: WriteMemory  # updated memory for the coordinator to persist
    target_changed: bool = False  # True when the TARGET value itself changed this cycle


# ---------------------------------------------------------------------------
# Demand source and DHW inference (change 1, docs/spec.md v0.2 field findings)
# ---------------------------------------------------------------------------


def zone_max_demand(values: list[float | None]) -> float | None:
    """Max of the configured zone demand sensors' numeric states, ignoring
    unknown/unavailable (None) readings. None if none of them are available."""
    available = [v for v in values if v is not None]
    return max(available) if available else None


def infer_dhw_demand(
    relay_demand_on: bool,
    zone_configured: bool,
    aggregate_demand: float | None,
    zone_max: float | None,
    aggregate_threshold: float = 90.0,
    all_zones_valid: bool = True,
) -> bool:
    """DHW detection. The HW relay sensor can miss some charges over
    RF, so it cannot be the sole DHW signal. When a zone list is configured we
    can additionally infer a DHW-only charge from the aggregate-includes-DHW
    signature: the aggregate controller sensor reads 100 during a DHW-only
    charge while every per-zone sensor reads 0.

    Limitation: this inference cannot separate `dhw_and_heating` from plain high
    heating demand when the relay is silent, because a genuine mixed charge
    looks the same from the aggregate alone once any zone has demand > 0 (which
    also makes `zone_max == 0` false, so the inference naturally does not fire).
    `dhw_and_heating` therefore still requires the relay signal.
    """
    if relay_demand_on:
        return True
    if not all_zones_valid or not zone_configured or aggregate_demand is None or zone_max is None:
        return False
    return aggregate_demand >= aggregate_threshold and zone_max == 0.0


# ---------------------------------------------------------------------------
# Manual hold detection (§3.1 manual_hold row)
# ---------------------------------------------------------------------------


def detect_manual_hold(
    current_value: float | None,
    last_written: float | None,
    dial_value: float | None,
    now: datetime,
    state: ManualHoldState,
    params: ManualHoldParams = ManualHoldParams(),
) -> tuple[bool, ManualHoldState]:
    """True while the live setpoint differs from what we last wrote, for up to
    `hold_minutes`. Without a live reading or a prior write we cannot tell.

    v0.2 field finding: the boiler silently reverts `selflowtemp` to the dial
    value (the max-flow entity, front panel, 70 C in the owner's install) within
    about 2 minutes of nothing rewriting it. That revert is boiler behaviour,
    never a manual hold, so a live value that differs from what we wrote is only
    treated as a manual change if it *also* differs from the current dial value.
    A genuine hand-turn to exactly the dial value is therefore deliberately
    indistinguishable from a revert and is ignored here — the enable switch /
    override select is the escape hatch for that edge case.
    """
    if current_value is None or last_written is None:
        return False, ManualHoldState(None)
    if abs(current_value - last_written) <= params.tolerance:
        return False, ManualHoldState(None)
    if dial_value is not None and abs(current_value - dial_value) <= params.tolerance:
        return False, ManualHoldState(None)  # revert-to-dial, not a manual change
    since = state.detected_at or now
    if now - since < timedelta(minutes=params.hold_minutes):
        return True, ManualHoldState(since)
    return False, ManualHoldState(None)  # hold expired: resume


# ---------------------------------------------------------------------------
# Mode decision table (§3.1)
# ---------------------------------------------------------------------------


def decide_mode(inputs: ModeInputs) -> Mode:
    if not inputs.enabled:
        return Mode.OFF
    if inputs.manual_hold_active:
        return Mode.MANUAL_HOLD
    if inputs.dhw_demand and inputs.heat_demand:
        return Mode.DHW_AND_HEATING
    if inputs.dhw_demand:
        return Mode.DHW
    if inputs.heat_demand:
        return Mode.HEATING
    return Mode.IDLE


# ---------------------------------------------------------------------------
# Write-or-not decision (§3.2.6, §4)
# ---------------------------------------------------------------------------


def decide_write(
    mode: Mode,
    target: float | None,
    override: Override,
    memory: WriteMemory,
    now: datetime,
    exempt_hysteresis: bool = False,
    hysteresis_params: HysteresisParams = HysteresisParams(),
) -> Decision:
    """One policy evaluation for the current cycle. `exempt_hysteresis` is set by
    the coordinator for the return-ceiling case and for leaving DHW mode (§3.3.4),
    both of which may act every cycle.

    v0.2 field finding (change 2): ems-esp decays a written flow setpoint back to
    the dial value if nothing rewrites it for ~2 minutes, so hysteresis/min_hold
    now gate whether the TARGET may change, not whether a write happens. In auto
    mode, once a mode is reached that should be driving the boiler (heating, dhw,
    dhw_and_heating, or idle parked at the curve value) we call `number.set_value`
    every cycle, re-asserting the current target even when it has not changed.
    """
    if mode is Mode.OFF:
        return Decision(mode, Action.NONE, None, "integration disabled", None, memory)
    if target is None:
        return Decision(mode, Action.NONE, None, "no target computed", None, memory)
    if mode is Mode.MANUAL_HOLD:
        return Decision(
            mode, Action.NONE, None, "manual hold: selflowtemp set by hand", target, memory
        )

    target_changed = should_write(target, memory, now, hysteresis_params, exempt=exempt_hysteresis)
    write_value = (
        target
        if target_changed or memory.last_written_setpoint is None
        else memory.last_written_setpoint
    )
    new_memory = WriteMemory(
        last_written_setpoint=write_value,
        last_written_at=now,
        last_target_change=now if target_changed else memory.last_target_change,
    )
    reason = (
        f"mode={mode.value}"
        if target_changed
        else f"mode={mode.value}; re-asserting unchanged target"
    )
    if override is not Override.AUTO:
        # The caller maintains separate virtual memory for shadow operation.
        return Decision(
            mode,
            Action.NONE,
            None,
            f"{override.value}: {reason}",
            write_value,
            memory,
            target_changed,
        )
    return Decision(
        mode, Action.WRITE, write_value, reason, write_value, new_memory, target_changed
    )
