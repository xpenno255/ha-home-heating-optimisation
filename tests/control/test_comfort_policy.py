"""State machine tests for core.policy (design note §7, §8)."""

from datetime import datetime, timedelta, timezone

from custom_components.home_heating_optimisation.control.comfort.core.policy import (
    Action,
    OverrideMemory,
    PolicyInputs,
    PolicyParams,
    State,
    ZoneState,
    decide,
)

T0 = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


def inputs(**kw) -> PolicyInputs:
    base = dict(
        now=T0,
        room_enabled=True,
        hub_enabled=True,
        holiday_mode=False,
        within_time_window=True,
        shadow_mode=False,
        computed_setpoint=19.5,
        zone=ZoneState(current_setpoint=19.0, schedule_setpoint=19.0),
        memory=OverrideMemory(),
    )
    base.update(kw)
    return PolicyInputs(**base)


def held(setpoint=19.5, minutes_ago=10) -> OverrideMemory:
    return OverrideMemory(
        last_written_setpoint=setpoint, last_written_at=T0 - timedelta(minutes=minutes_ago)
    )


# --- off / window / no data ------------------------------------------------


def test_disabled_room_does_nothing_when_nothing_held():
    d = decide(inputs(room_enabled=False))
    assert d.state is State.OFF and d.action is Action.NONE


def test_disabled_room_releases_a_held_override_once():
    # zone still carries our 19.5: confirmed ours, released once
    d = decide(inputs(room_enabled=False, memory=held(), zone=ZoneState(19.5, 19.0)))
    assert d.action is Action.RELEASE
    assert d.memory.last_written_at is None
    # second evaluation: nothing left to release
    d2 = decide(inputs(room_enabled=False, memory=d.memory, zone=ZoneState(19.5, 19.0)))
    assert d2.action is Action.NONE


def test_disabled_room_does_not_release_a_superseded_override():
    """The user changed the zone after our write: releasing would cancel their action."""
    d = decide(inputs(room_enabled=False, memory=held(19.5), zone=ZoneState(22.0, 19.0)))
    assert d.action is Action.NONE and "another hand" in d.reason
    assert d.memory.last_written_at is None  # ownership relinquished, no retry


def test_disabled_room_with_unreadable_zone_lets_override_expire():
    d = decide(inputs(room_enabled=False, memory=held(19.5), zone=ZoneState(None, 19.0)))
    assert d.action is Action.NONE and "expire" in d.reason
    assert d.memory.last_written_at is not None  # kept: confirmation may become possible


def test_hub_disabled_and_holiday_are_off():
    assert decide(inputs(hub_enabled=False)).state is State.OFF
    assert decide(inputs(holiday_mode=True)).state is State.OFF


def test_outside_time_window_releases():
    d = decide(inputs(within_time_window=False, memory=held(), zone=ZoneState(19.5, 19.0)))
    assert d.state is State.OUTSIDE_WINDOW and d.action is Action.RELEASE


def test_no_computed_setpoint_releases_and_reports():
    d = decide(inputs(computed_setpoint=None, memory=held(), zone=ZoneState(19.5, 19.0)))
    assert d.state is State.NO_DATA and d.action is Action.RELEASE


# --- active writes ----------------------------------------------------------


def test_first_active_cycle_writes():
    d = decide(inputs())
    assert d.state is State.ACTIVE and d.action is Action.WRITE and d.setpoint == 19.5
    assert d.memory.last_written_setpoint == 19.5 and d.memory.last_written_at == T0


def test_unchanged_setpoint_is_not_rewritten_while_override_valid():
    d = decide(inputs(memory=held(19.5, minutes_ago=10), zone=ZoneState(19.5, 19.0)))
    assert d.action is Action.NONE and "unchanged" in d.reason


def test_unchanged_setpoint_is_refreshed_near_expiry():
    d = decide(inputs(memory=held(19.5, minutes_ago=50), zone=ZoneState(19.5, 19.0)))
    assert d.action is Action.WRITE and d.setpoint == 19.5


def test_change_of_one_step_writes_smaller_change_does_not():
    p = PolicyParams(step=0.5)
    m = held(19.5, minutes_ago=10)
    assert (
        decide(
            inputs(params=p, memory=m, computed_setpoint=20.0, zone=ZoneState(19.5, 19.0))
        ).action
        is Action.WRITE
    )
    assert (
        decide(
            inputs(params=p, memory=m, computed_setpoint=19.7, zone=ZoneState(19.5, 19.0))
        ).action
        is Action.NONE
    )


# --- manual override ---------------------------------------------------------


def test_manual_dial_change_is_respected_then_resumes():
    # We wrote 19.5; zone now says 21.0 and schedule is 19.0 -> someone turned the dial.
    m = held(19.5, minutes_ago=10)
    d = decide(inputs(memory=m, zone=ZoneState(21.0, 19.0)))
    assert d.state is State.MANUAL and d.action is Action.NONE
    assert d.memory.manual_detected_at == T0
    # 90 minutes later, still within the 120-minute hold
    later = inputs(now=T0 + timedelta(minutes=90), memory=d.memory, zone=ZoneState(21.0, 19.0))
    assert decide(later).state is State.MANUAL
    # After the hold expires we resume and write our value again; the manual record is
    # kept (reclaim pending) until the zone echoes our write, then cleared.
    much_later = inputs(
        now=T0 + timedelta(minutes=125), memory=d.memory, zone=ZoneState(21.0, 19.0)
    )
    d3 = decide(much_later)
    assert d3.state is State.ACTIVE and d3.action is Action.WRITE
    echoed = inputs(
        now=T0 + timedelta(minutes=130), memory=d3.memory, zone=ZoneState(d3.setpoint, 19.0)
    )
    assert decide(echoed).memory.manual_detected_at is None


def test_manual_hold_ends_at_next_switchpoint():
    m = held(19.5, minutes_ago=10)
    d = decide(
        inputs(memory=m, zone=ZoneState(21.0, 19.0, next_switchpoint_at=T0 + timedelta(minutes=30)))
    )
    assert d.state is State.MANUAL
    after = inputs(
        now=T0 + timedelta(minutes=31),
        memory=d.memory,
        zone=ZoneState(21.0, 21.0, next_switchpoint_at=T0 + timedelta(minutes=30)),
    )
    # schedule now 21 too, so the zone matches schedule: not manual anymore
    assert decide(after).state is State.ACTIVE


def test_zone_at_schedule_is_not_manual():
    d = decide(inputs(memory=held(19.5), zone=ZoneState(19.0, 19.0)))
    assert d.state is State.ACTIVE


def test_zone_at_our_value_is_not_manual():
    d = decide(inputs(memory=held(19.5), zone=ZoneState(19.5, 19.0)))
    assert d.state is State.ACTIVE


def test_exact_prior_command_reversion_is_not_attributed_to_a_person():
    m = held(19.6, minutes_ago=1)
    d = decide(
        inputs(
            memory=m,
            computed_setpoint=19.6,
            zone=ZoneState(19.5, 19.0),
            command_reverted=True,
            retry_reverted_command=False,
        )
    )
    assert d.state is State.ACTIVE
    assert d.action is Action.NONE
    assert d.memory.manual_detected_at is None


def test_exact_prior_command_reversion_can_request_one_coordinator_bounded_retry():
    m = held(19.6, minutes_ago=1)
    d = decide(
        inputs(
            memory=m,
            computed_setpoint=19.6,
            zone=ZoneState(19.5, 19.0),
            command_reverted=True,
            retry_reverted_command=True,
        )
    )
    assert d.state is State.ACTIVE
    assert d.action is Action.WRITE
    assert d.setpoint == 19.6
    assert "retrying command" in d.reason


def test_different_readback_without_reversion_provenance_remains_manual():
    d = decide(
        inputs(
            memory=held(19.6, minutes_ago=1),
            computed_setpoint=19.6,
            zone=ZoneState(19.5, 19.0),
        )
    )
    assert d.state is State.MANUAL


def test_reversion_flag_does_not_override_a_standing_manual_hold():
    manual = OverrideMemory(
        manual_detected_at=T0 - timedelta(minutes=5),
        manual_release_at=T0 + timedelta(minutes=30),
        manual_setpoint=21.0,
    )
    d = decide(
        inputs(
            memory=manual,
            zone=ZoneState(21.0, 19.0),
            command_reverted=True,
            retry_reverted_command=True,
        )
    )
    assert d.state is State.MANUAL
    assert d.action is Action.NONE


def test_reversion_flag_does_not_override_zone_off_protection():
    d = decide(
        inputs(
            zone=ZoneState(5.0, 19.0),
            command_reverted=True,
            retry_reverted_command=True,
        )
    )
    assert d.state is State.OFF
    assert d.action is Action.NONE


# --- window / door -----------------------------------------------------------


def test_window_open_needs_delay_then_writes_setback():
    p = PolicyParams(window_open_delay_minutes=5, window_setpoint=10.0)
    d = decide(inputs(params=p, any_window_open=True))
    assert d.state is State.ACTIVE  # opened just now; delay not elapsed
    assert d.memory.window_open_since == T0
    d2 = decide(
        inputs(params=p, any_window_open=True, now=T0 + timedelta(minutes=6), memory=d.memory)
    )
    assert d2.state is State.WINDOW_OPEN and d2.action is Action.WRITE and d2.setpoint == 10.0


def test_window_close_delay_keeps_setback_then_resumes():
    p = PolicyParams(window_close_delay_minutes=15)
    m = OverrideMemory(
        last_written_setpoint=10.0,
        last_written_at=T0 - timedelta(minutes=1),
        window_open_since=T0 - timedelta(minutes=30),
    )
    d = decide(inputs(params=p, any_window_open=False, memory=m, zone=ZoneState(10.0, 19.0)))
    assert d.state is State.WINDOW_OPEN and d.memory.window_closed_at == T0
    d2 = decide(
        inputs(
            params=p,
            any_window_open=False,
            now=T0 + timedelta(minutes=16),
            memory=d.memory,
            zone=ZoneState(10.0, 19.0),
        )
    )
    assert d2.state is State.ACTIVE and d2.action is Action.WRITE and d2.setpoint == 19.5


def test_adjacent_door_open_writes_plain_schedule():
    d = decide(inputs(any_adjacent_door_open=True))
    assert d.state is State.DOOR_OPEN and d.setpoint == 19.0


def test_window_beats_door():
    m = OverrideMemory(window_open_since=T0 - timedelta(minutes=10))
    d = decide(inputs(any_window_open=True, any_adjacent_door_open=True, memory=m))
    assert d.state is State.WINDOW_OPEN


# --- pre-heat ----------------------------------------------------------------


def test_preheat_release_before_upward_switchpoint():
    z = ZoneState(
        19.5, 18.0, next_switchpoint_at=T0 + timedelta(minutes=40), next_switchpoint_setpoint=20.0
    )
    d = decide(inputs(memory=held(19.5), zone=z))
    assert d.state is State.PREHEAT and d.action is Action.RELEASE
    # once released the zone follows its schedule; nothing more to do until the switchpoint passes
    z_released = ZoneState(
        18.0, 18.0, next_switchpoint_at=z.next_switchpoint_at, next_switchpoint_setpoint=20.0
    )
    d2 = decide(inputs(memory=d.memory, zone=z_released))
    assert d2.state is State.PREHEAT and d2.action is Action.NONE


def test_no_preheat_for_downward_or_distant_switchpoint():
    down = ZoneState(
        19.5, 20.0, next_switchpoint_at=T0 + timedelta(minutes=40), next_switchpoint_setpoint=18.0
    )
    far = ZoneState(
        19.5, 18.0, next_switchpoint_at=T0 + timedelta(minutes=200), next_switchpoint_setpoint=20.0
    )
    assert decide(inputs(zone=down, memory=held(19.5))).state is State.ACTIVE
    assert decide(inputs(zone=far, memory=held(19.5))).state is State.ACTIVE


# --- shadow ------------------------------------------------------------------


def test_shadow_never_acts_but_reports_would_write():
    d = decide(inputs(shadow_mode=True))
    assert d.state is State.SHADOW and d.action is Action.NONE and d.would_write == 19.5
    dw = decide(
        inputs(
            shadow_mode=True,
            any_window_open=True,
            memory=OverrideMemory(window_open_since=T0 - timedelta(minutes=10)),
        )
    )
    assert dw.state is State.SHADOW and dw.would_write == 10.0
    assert dw.memory.last_written_at is None


def test_shadow_still_releases_when_turned_off():
    """Switching a room off while a v1-era override is held should still release it."""
    d = decide(inputs(shadow_mode=True, room_enabled=False, memory=held(19.0)))
    assert d.action is Action.RELEASE


def test_unknown_schedule_is_never_manual():
    """Right after a restart the schedule can be missing; a non-matching zone setpoint is not 'manual'."""
    d = decide(inputs(computed_setpoint=None, zone=ZoneState(19.0, None)))
    assert d.state is State.NO_DATA


def test_zone_at_next_switchpoint_value_near_switchpoint_is_not_manual():
    """Cloud schedule still says 18 at 06:31 while the zone already moved to 19 at 06:30."""
    z = ZoneState(
        19.0, 18.0, next_switchpoint_at=T0 - timedelta(minutes=1), next_switchpoint_setpoint=19.0
    )
    d = decide(inputs(memory=held(18.2), zone=z))
    assert d.state is not State.MANUAL
    far = ZoneState(
        19.0, 18.0, next_switchpoint_at=T0 + timedelta(hours=3), next_switchpoint_setpoint=19.0
    )
    assert decide(inputs(memory=held(18.2), zone=far)).state is State.MANUAL


def test_zone_parked_at_off_floor_is_off_not_manual():
    """A zone the owner turned off (evohome 5.0 floor) is deliberately off, not a dial change."""
    d = decide(inputs(zone=ZoneState(5.0, 18.0)))
    assert d.state is State.OFF and d.action is Action.NONE and "off floor" in d.reason
    # the 5.0 superseded any override of ours: never release it back to schedule
    d2 = decide(inputs(zone=ZoneState(5.0, 18.0), memory=held()))
    assert d2.state is State.OFF and d2.action is Action.NONE
    assert d2.memory.last_written_at is None  # ownership relinquished


def test_zone_raised_by_optimum_start_is_not_manual():
    """Evohome optimum start moves the zone to the next value up to an hour before the switchpoint."""
    z = ZoneState(
        19.0, 18.0, next_switchpoint_at=T0 + timedelta(minutes=55), next_switchpoint_setpoint=19.0
    )
    d = decide(inputs(memory=held(18.2), zone=z))
    assert d.state is not State.MANUAL


def test_manual_hold_deadline_is_frozen_at_detection():
    """A schedule source that later advertises the FOLLOWING switchpoint must not extend the hold."""
    z = ZoneState(
        21.0, 19.0, next_switchpoint_at=T0 + timedelta(minutes=30), next_switchpoint_setpoint=18.0
    )
    d = decide(inputs(memory=held(19.5), zone=z))
    assert d.state is State.MANUAL and d.memory.manual_release_at == T0 + timedelta(minutes=30)
    # 31 min on: the switchpoint passed; cloud already advertises tomorrow's switchpoint
    later_zone = ZoneState(
        21.0, 19.0, next_switchpoint_at=T0 + timedelta(hours=9), next_switchpoint_setpoint=18.0
    )
    d2 = decide(inputs(now=T0 + timedelta(minutes=31), memory=d.memory, zone=later_zone))
    assert d2.state is not State.MANUAL  # hold ended at the original switchpoint


def test_manual_readjustment_restarts_the_hold():
    d = decide(inputs(memory=held(19.5), zone=ZoneState(21.0, 19.0)))
    assert d.state is State.MANUAL and d.memory.manual_setpoint == 21.0
    # 110 min later the user picks a new value: a fresh hold starts
    late = T0 + timedelta(minutes=110)
    d2 = decide(inputs(now=late, memory=d.memory, zone=ZoneState(22.5, 19.0)))
    assert d2.state is State.MANUAL and d2.memory.manual_detected_at == late
    # 30 min after that (past the original 120-min mark) it is still holding
    d3 = decide(
        inputs(now=late + timedelta(minutes=30), memory=d2.memory, zone=ZoneState(22.5, 19.0))
    )
    assert d3.state is State.MANUAL


def test_written_target_is_clamped_and_remembered_as_transmitted():
    """Memory must hold the value that went on the wire, or the echo looks manual."""
    d = decide(inputs(computed_setpoint=31.0))
    assert d.action is Action.WRITE and d.setpoint == 30.0
    assert d.memory.last_written_setpoint == 30.0 and "clamped" in d.reason
    # zone echoes 30: recognised as ours, not manual
    d2 = decide(
        inputs(
            now=T0 + timedelta(minutes=5),
            memory=d.memory,
            computed_setpoint=31.0,
            zone=ZoneState(30.0, 19.0),
        )
    )
    assert d2.state is State.ACTIVE and d2.action is Action.NONE


def test_dial_returned_to_our_old_value_during_hold_stays_manual():
    """User sets 22, then changes their mind back to our unexpired 20.5: still their call."""
    m = held(20.5, minutes_ago=20)
    d = decide(inputs(memory=m, zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL
    d2 = decide(inputs(now=T0 + timedelta(minutes=10), memory=d.memory, zone=ZoneState(20.5, 19.0)))
    assert d2.state is State.MANUAL  # readjustment, not our echo
    assert d2.memory.manual_detected_at == T0 + timedelta(minutes=10)  # fresh hold


def test_standing_hold_outranks_preheat_release():
    """User at 20.5 during a hold, next switchpoint also 20.5 soon: still MANUAL, no release."""
    m = held(20.5, minutes_ago=20)
    d = decide(inputs(memory=m, zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL
    z = ZoneState(
        20.5, 19.0, next_switchpoint_at=T0 + timedelta(minutes=40), next_switchpoint_setpoint=20.5
    )
    d2 = decide(inputs(now=T0 + timedelta(minutes=10), memory=d.memory, zone=z))
    assert d2.state is State.MANUAL and d2.action is Action.NONE


def test_standing_hold_ends_when_zone_back_at_schedule():
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL
    d2 = decide(inputs(now=T0 + timedelta(minutes=10), memory=d.memory, zone=ZoneState(19.0, 19.0)))
    assert d2.state is State.ACTIVE and d2.memory.manual_detected_at is None


def test_floor_clamped_write_echo_is_not_user_off():
    """Computed 4 -> write 5; the echo of our own 5 must not release/loop."""
    d = decide(inputs(computed_setpoint=4.0, zone=ZoneState(6.0, 6.0)))
    assert d.action is Action.WRITE and d.setpoint == 5.0
    d2 = decide(
        inputs(
            now=T0 + timedelta(minutes=5),
            memory=d.memory,
            computed_setpoint=4.0,
            zone=ZoneState(5.0, 6.0),
        )
    )
    assert d2.action is Action.NONE and d2.state is State.ACTIVE
    # but a 5.0 we did NOT write is still the owner's off
    d3 = decide(inputs(computed_setpoint=4.0, zone=ZoneState(5.0, 6.0)))
    assert d3.state is State.OFF


def test_non_finite_targets_are_never_written():
    inf = float("inf")
    d = decide(inputs(computed_setpoint=inf))
    assert d.action is Action.NONE and "non-finite" in d.reason
    # door branch with an infinite schedule target
    d2 = decide(
        inputs(computed_setpoint=None, any_adjacent_door_open=True, zone=ZoneState(None, inf))
    )
    assert d2.action is not Action.WRITE
    # shadow preview matches: nothing to preview
    d3 = decide(inputs(shadow_mode=True, computed_setpoint=inf))
    assert d3.would_write is None


def test_shadow_would_write_is_bounded_like_active():
    d = decide(inputs(shadow_mode=True, computed_setpoint=31.0))
    assert d.would_write == 30.0


def test_manual_takeover_relinquishes_write_ownership():
    """Once manual control is detected, our old write is dead: disable must not release."""
    d = decide(inputs(memory=held(20.5, minutes_ago=10), zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL and d.memory.last_written_at is None
    # user returns to our old value, then OT is disabled: their setting stays
    d2 = decide(inputs(now=T0 + timedelta(minutes=10), memory=d.memory, zone=ZoneState(20.5, 19.0)))
    assert d2.state is State.MANUAL
    d3 = decide(
        inputs(
            now=T0 + timedelta(minutes=15),
            room_enabled=False,
            memory=d2.memory,
            zone=ZoneState(20.5, 19.0),
        )
    )
    assert d3.action is Action.NONE


def test_user_five_after_manual_takeover_is_owner_off():
    """OT wrote 5 (clamp), user set 22, user set 5: that 5 is the owner's off, not our echo."""
    d = decide(inputs(computed_setpoint=4.0, zone=ZoneState(6.0, 6.0)))
    assert d.setpoint == 5.0
    d2 = decide(
        inputs(
            now=T0 + timedelta(minutes=5),
            memory=d.memory,
            computed_setpoint=4.0,
            zone=ZoneState(22.0, 6.0),
        )
    )
    assert d2.state is State.MANUAL
    d3 = decide(
        inputs(
            now=T0 + timedelta(minutes=10),
            memory=d2.memory,
            computed_setpoint=4.0,
            zone=ZoneState(5.0, 6.0),
        )
    )
    assert d3.state is State.OFF and d3.action is Action.NONE


def test_cancelled_hold_survives_no_data_and_preheat_paths():
    """Zone back at schedule clears the hold even when the branch taken is NO_DATA/PREHEAT."""
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL
    # back at schedule + no computed setpoint: NO_DATA must persist the cleared hold
    d2 = decide(
        inputs(
            now=T0 + timedelta(minutes=10),
            memory=d.memory,
            computed_setpoint=None,
            zone=ZoneState(19.0, 19.0),
        )
    )
    assert d2.state is State.NO_DATA and d2.memory.manual_detected_at is None
    # back at schedule + imminent upward switchpoint: PREHEAT must persist it too
    z = ZoneState(
        19.0, 19.0, next_switchpoint_at=T0 + timedelta(minutes=40), next_switchpoint_setpoint=21.0
    )
    d3 = decide(inputs(now=T0 + timedelta(minutes=10), memory=d.memory, zone=z))
    assert d3.state is State.PREHEAT and d3.memory.manual_detected_at is None


def test_expired_hold_is_not_redetected_before_reclaim():
    """Hold expires during a NO_DATA gap; on recovery the unchanged value must resume, not re-hold."""
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    assert d.state is State.MANUAL
    after = T0 + timedelta(minutes=125)  # past the 120-min hold
    # data gap at expiry: NO_DATA keeps the (expired) record
    d2 = decide(
        inputs(now=after, memory=d.memory, computed_setpoint=None, zone=ZoneState(22.0, 19.0))
    )
    assert d2.state is State.NO_DATA and d2.memory.manual_release_at is not None
    # data recovers: resume and write, no fresh hold
    d3 = decide(
        inputs(now=after + timedelta(minutes=5), memory=d2.memory, zone=ZoneState(22.0, 19.0))
    )
    assert d3.state is State.ACTIVE and d3.action is Action.WRITE
    # once the zone echoes our write, the manual record is finally cleared
    d4 = decide(
        inputs(
            now=after + timedelta(minutes=10), memory=d3.memory, zone=ZoneState(d3.setpoint, 19.0)
        )
    )
    assert d4.memory.manual_detected_at is None


def test_expired_hold_readjustment_still_starts_fresh_hold():
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    after = T0 + timedelta(minutes=125)
    d2 = decide(inputs(now=after, memory=d.memory, zone=ZoneState(23.5, 19.0)))
    assert d2.state is State.MANUAL and d2.memory.manual_detected_at == after


def test_missing_schedule_does_not_clear_reclaim_record():
    """Schedule vanishes while a reclaim is pending: the record must survive its return."""
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    after = T0 + timedelta(minutes=125)
    d2 = decide(
        inputs(now=after, memory=d.memory, computed_setpoint=None, zone=ZoneState(22.0, None))
    )
    assert d2.memory.manual_release_at is not None  # kept: no schedule, no evidence
    # schedule returns: unchanged 22 resumes with a write, no fresh hold
    d3 = decide(
        inputs(now=after + timedelta(minutes=5), memory=d2.memory, zone=ZoneState(22.0, 19.0))
    )
    assert d3.state is State.ACTIVE and d3.action is Action.WRITE


def test_accepted_reclaim_is_not_resent_every_cycle():
    """Zone hasn't echoed the reclaim write yet: normal suppression applies, no write storm."""
    d = decide(inputs(zone=ZoneState(22.0, 19.0)))
    after = T0 + timedelta(minutes=125)
    d2 = decide(inputs(now=after, memory=d.memory, zone=ZoneState(22.0, 19.0)))
    assert d2.action is Action.WRITE  # the reclaim
    # next cycle: zone still shows 22 (echo lag) — no second write
    d3 = decide(
        inputs(now=after + timedelta(minutes=5), memory=d2.memory, zone=ZoneState(22.0, 19.0))
    )
    assert d3.action is Action.NONE and "unchanged" in d3.reason


def test_zero_minute_hold_reclaims_immediately():
    p = PolicyParams(manual_hold_minutes=0)
    d = decide(inputs(params=p, memory=held(19.5), zone=ZoneState(22.0, 19.0)))
    assert d.state is State.ACTIVE and d.action is Action.WRITE


def test_expired_write_value_is_not_treated_as_ours():
    """Someone selecting the same number as an old, expired OT write is a manual change."""
    m = held(19.5, minutes_ago=90)  # override expired (60 min)
    d = decide(inputs(memory=m, zone=ZoneState(19.5, 19.0)))
    assert d.state is State.MANUAL


def test_zone_lagging_after_downward_switchpoint_is_not_manual():
    """At 21:00 the schedule drops to 16 but the zone still reports 19 for a few minutes."""
    z = ZoneState(
        19.0, 16.0, previous_schedule_setpoint=19.0, schedule_changed_at=T0 - timedelta(minutes=5)
    )
    d = decide(inputs(memory=held(19.2, minutes_ago=70), zone=z))
    assert d.state is not State.MANUAL
    # but a stale change long past no longer excuses it
    old = ZoneState(
        19.0, 16.0, previous_schedule_setpoint=19.0, schedule_changed_at=T0 - timedelta(hours=2)
    )
    assert decide(inputs(memory=held(19.2, minutes_ago=70), zone=old)).state is State.MANUAL
