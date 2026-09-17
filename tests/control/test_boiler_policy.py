"""Tests for core.policy: mode table, manual hold, write-or-not decision (spec §3.1, §3.2.6)."""

from datetime import datetime, timedelta, timezone

from custom_components.home_heating_optimisation.control.boiler.core.model import (
    ManualHoldParams,
    ManualHoldState,
    Mode,
    WriteMemory,
)
from custom_components.home_heating_optimisation.control.boiler.core.policy import (
    Action,
    ModeInputs,
    Override,
    decide_mode,
    decide_write,
    detect_manual_hold,
    infer_dhw_demand,
    zone_max_demand,
)

T0 = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


# --- mode table (§3.1) -------------------------------------------------------


def test_mode_off_when_disabled():
    assert (
        decide_mode(
            ModeInputs(enabled=False, heat_demand=True, dhw_demand=True, manual_hold_active=True)
        )
        is Mode.OFF
    )


def test_mode_manual_hold_beats_demand():
    assert (
        decide_mode(
            ModeInputs(enabled=True, heat_demand=True, dhw_demand=True, manual_hold_active=True)
        )
        is Mode.MANUAL_HOLD
    )


def test_mode_idle_when_no_demand():
    assert (
        decide_mode(
            ModeInputs(enabled=True, heat_demand=False, dhw_demand=False, manual_hold_active=False)
        )
        is Mode.IDLE
    )


def test_mode_heating_only():
    assert (
        decide_mode(
            ModeInputs(enabled=True, heat_demand=True, dhw_demand=False, manual_hold_active=False)
        )
        is Mode.HEATING
    )


def test_mode_dhw_only():
    assert (
        decide_mode(
            ModeInputs(enabled=True, heat_demand=False, dhw_demand=True, manual_hold_active=False)
        )
        is Mode.DHW
    )


def test_mode_dhw_and_heating():
    assert (
        decide_mode(
            ModeInputs(enabled=True, heat_demand=True, dhw_demand=True, manual_hold_active=False)
        )
        is Mode.DHW_AND_HEATING
    )


# --- zone-max demand and DHW inference (change 1) ----------------------------


def test_zone_max_demand_ignores_unavailable():
    assert zone_max_demand([None, 0.0, 40.0, None]) == 40.0


def test_zone_max_demand_none_when_all_unavailable():
    assert zone_max_demand([None, None]) is None


def test_zone_max_demand_empty_list_is_none():
    assert zone_max_demand([]) is None


def test_infer_dhw_relay_signal_always_wins():
    assert infer_dhw_demand(
        relay_demand_on=True, zone_configured=False, aggregate_demand=None, zone_max=None
    )
    assert infer_dhw_demand(
        relay_demand_on=True, zone_configured=True, aggregate_demand=50.0, zone_max=20.0
    )


def test_infer_dhw_no_zone_list_never_infers():
    assert not infer_dhw_demand(
        relay_demand_on=False, zone_configured=False, aggregate_demand=100.0, zone_max=None
    )


def test_infer_dhw_only_charge_signature():
    # aggregate includes DHW: 100 while every zone reads 0 -> inferred DHW-only
    assert infer_dhw_demand(
        relay_demand_on=False, zone_configured=True, aggregate_demand=100.0, zone_max=0.0
    )


def test_infer_dhw_below_threshold_not_inferred():
    assert not infer_dhw_demand(
        relay_demand_on=False, zone_configured=True, aggregate_demand=85.0, zone_max=0.0
    )


def test_infer_dhw_zone_max_nonzero_not_inferred():
    # a genuine heating demand, not DHW
    assert not infer_dhw_demand(
        relay_demand_on=False, zone_configured=True, aggregate_demand=100.0, zone_max=15.0
    )


def test_infer_dhw_all_zones_unavailable_is_not_inferred():
    # cannot confirm zero demand across the zones -> do not infer
    assert not infer_dhw_demand(
        relay_demand_on=False, zone_configured=True, aggregate_demand=100.0, zone_max=None
    )


# --- manual hold detection ----------------------------------------------------


def test_manual_hold_no_data_is_false():
    active, state = detect_manual_hold(None, 50.0, None, T0, ManualHoldState())
    assert not active and state.detected_at is None
    active, state = detect_manual_hold(55.0, None, None, T0, ManualHoldState())
    assert not active


def test_manual_hold_within_tolerance_is_false():
    active, state = detect_manual_hold(50.2, 50.0, None, T0, ManualHoldState())
    assert not active


def test_manual_hold_detected_then_holds_then_expires():
    active, state = detect_manual_hold(60.0, 50.0, None, T0, ManualHoldState())
    assert active and state.detected_at == T0
    # 20 minutes later, still within the 30-minute default hold
    active, state = detect_manual_hold(60.0, 50.0, None, T0 + timedelta(minutes=20), state)
    assert active and state.detected_at == T0
    # 31 minutes later: hold has expired, resume
    active, state = detect_manual_hold(60.0, 50.0, None, T0 + timedelta(minutes=31), state)
    assert not active and state.detected_at is None


def test_manual_hold_custom_params():
    p = ManualHoldParams(hold_minutes=5.0, tolerance=1.0)
    active, state = detect_manual_hold(50.5, 50.0, None, T0, ManualHoldState(), p)
    assert not active  # within the wider tolerance
    active, state = detect_manual_hold(55.0, 50.0, None, T0, ManualHoldState(), p)
    assert active
    active, state = detect_manual_hold(55.0, 50.0, None, T0 + timedelta(minutes=6), state, p)
    assert not active


def test_manual_hold_revert_to_dial_is_not_a_manual_hold():
    # boiler decayed selflowtemp back to the front-panel dial value (max-flow
    # entity), 70 here, ~2 minutes after we last wrote 50: not a manual change.
    active, state = detect_manual_hold(70.0, 50.0, 70.0, T0, ManualHoldState())
    assert not active and state.detected_at is None


def test_manual_hold_genuine_change_away_from_both_is_detected():
    # differs from what we wrote (50) and from the dial (70) -> genuine hand-turn
    active, state = detect_manual_hold(60.0, 50.0, 70.0, T0, ManualHoldState())
    assert active and state.detected_at == T0


def test_manual_hold_hand_turn_to_exactly_the_dial_value_is_ignored():
    # documented limitation: indistinguishable from a revert, deliberately ignored
    active, state = detect_manual_hold(70.0, 50.0, 70.0, T0, ManualHoldState())
    assert not active


# --- write-or-not decision (§3.2.6, §4) --------------------------------------


def test_decide_write_off_never_writes():
    d = decide_write(Mode.OFF, 50.0, Override.AUTO, WriteMemory(), T0)
    assert d.action is Action.NONE and d.would_write is None


def test_decide_write_no_target_reports_none():
    d = decide_write(Mode.HEATING, None, Override.AUTO, WriteMemory(), T0)
    assert d.action is Action.NONE and d.would_write is None


def test_decide_write_manual_hold_reports_would_write_but_no_action():
    d = decide_write(Mode.MANUAL_HOLD, 50.0, Override.AUTO, WriteMemory(), T0)
    assert d.action is Action.NONE and d.would_write == 50.0


def test_decide_write_override_hold_never_writes():
    d = decide_write(Mode.HEATING, 50.0, Override.HOLD, WriteMemory(), T0)
    assert d.action is Action.NONE and d.would_write == 50.0


def test_decide_write_first_cycle_writes_in_auto():
    d = decide_write(Mode.HEATING, 50.0, Override.AUTO, WriteMemory(), T0)
    assert d.action is Action.WRITE and d.setpoint == 50.0
    assert d.target_changed
    assert d.memory.last_written_setpoint == 50.0 and d.memory.last_written_at == T0
    assert d.memory.last_target_change == T0


def test_decide_write_shadow_never_writes_but_reports_would_write():
    d = decide_write(Mode.HEATING, 50.0, Override.SHADOW, WriteMemory(), T0)
    assert d.action is Action.NONE and d.would_write == 50.0
    assert d.memory.last_written_setpoint is None  # shadow does not persist a write


# --- change 2: re-assert every cycle in auto vs target-change gating --------


def test_decide_write_unchanged_still_re_asserts_every_cycle():
    m = WriteMemory(last_written_setpoint=50.0, last_written_at=T0, last_target_change=T0)
    d = decide_write(Mode.HEATING, 50.4, Override.AUTO, m, T0 + timedelta(minutes=30))
    # target unchanged (within hysteresis), but auto must still re-assert the
    # value every cycle so the boiler does not decay it back to the dial (change 2)
    assert d.action is Action.WRITE and d.setpoint == 50.0
    assert not d.target_changed
    assert d.memory.last_written_at == T0 + timedelta(minutes=30)
    assert d.memory.last_target_change == T0  # target itself has not changed


def test_decide_write_big_change_within_min_hold_re_asserts_old_target():
    m = WriteMemory(last_written_setpoint=50.0, last_written_at=T0, last_target_change=T0)
    d = decide_write(Mode.HEATING, 53.0, Override.AUTO, m, T0 + timedelta(minutes=2))
    # target may not change yet (min_hold not elapsed), but the old value is
    # still re-asserted every cycle
    assert d.action is Action.WRITE and d.setpoint == 50.0
    assert not d.target_changed
    assert d.memory.last_written_at == T0 + timedelta(minutes=2)
    assert d.memory.last_target_change == T0


def test_decide_write_exempt_bypasses_min_hold():
    m = WriteMemory(last_written_setpoint=50.0, last_written_at=T0, last_target_change=T0)
    d = decide_write(
        Mode.HEATING, 53.0, Override.AUTO, m, T0 + timedelta(minutes=2), exempt_hysteresis=True
    )
    assert d.action is Action.WRITE and d.setpoint == 53.0
    assert d.target_changed
    assert d.memory.last_target_change == T0 + timedelta(minutes=2)


def test_decide_write_idle_also_re_asserts_park_value():
    m = WriteMemory(last_written_setpoint=45.0, last_written_at=T0, last_target_change=T0)
    d = decide_write(Mode.IDLE, 45.0, Override.AUTO, m, T0 + timedelta(minutes=1))
    assert d.action is Action.WRITE and d.setpoint == 45.0
    assert not d.target_changed
