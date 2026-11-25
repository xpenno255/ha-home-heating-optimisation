"""Planned pre-heat, pure parts: options, plan pace, trajectory, hot water, policy, scoring."""

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.home_heating_optimisation.control.comfort.core.policy import (
    Action,
    OverrideMemory,
    PolicyInputs,
    State,
    ZoneState,
    decide,
)
from custom_components.home_heating_optimisation.optimum.model import (
    RoomModel,
    StartParams,
    plan_factor,
    predict_start,
)
from custom_components.home_heating_optimisation.optimum.preheat import (
    DEFAULT_CHARGE_MINUTES,
    PreheatPlan,
    avoid_charge,
    charge_length,
    finalise_capture,
    lead_error_minutes,
    learn_lead,
    next_dhw_on,
    note_report,
)
from custom_components.home_heating_optimisation.optimum.preheat_policy import (
    preheat_config,
    validation_error,
)
from custom_components.home_heating_optimisation.optimum.scoring import evohome_start

T0 = datetime(2026, 10, 22, 5, 0, tzinfo=timezone.utc)
SWITCH = T0 + timedelta(hours=2)
MODEL = RoomModel(20.0, None, None, 50.0, 5, None, delay_minutes=10.0)
PARAMS = StartParams(10, 240)


def start(plan_flow=None, **kw):
    return predict_start(T0, SWITCH, 21.0, 18.0, MODEL, None, 65.0, PARAMS, plan_flow=plan_flow)


# -- options -------------------------------------------------------------------


def test_config_defaults_and_bad_values_fall_back():
    cfg = preheat_config({})
    assert cfg["mode"] == "off" and cfg["plan_flow"] == 55.0 and cfg["max_lead_minutes"] == 240
    assert cfg["arrival_gain"] == 0.5
    assert preheat_config({"preheat": {"arrival_gain": 3}})["arrival_gain"] == 0.5
    bad = preheat_config({"preheat": {"mode": "x", "plan_flow": 5, "fullness_low": 0.9}})
    assert bad["mode"] == "off" and bad["plan_flow"] == 55.0
    assert bad["fullness_low"] < bad["fullness_high"]  # 0.9 is not below the default 0.65


def test_validation():
    cfg = preheat_config({})
    assert validation_error(cfg) is None
    assert validation_error({**cfg, "mode": "x"}) == "preheat_invalid"
    assert validation_error({**cfg, "plan_flow": 99}) == "preheat_invalid"
    assert validation_error({**cfg, "fullness_low": 0.7}) == "preheat_invalid"
    assert validation_error({**cfg, "mode": "active"}) is None
    assert validation_error({**cfg, "mode": "active"}, optimum_enabled=False) == (
        "preheat_needs_optimum"
    )
    assert validation_error({**cfg, "mode": "shadow"}, optimum_enabled=False) is None


# -- plan pace -----------------------------------------------------------------


def test_gentler_plan_flow_starts_earlier_and_scales_pace_not_delay():
    plain, gentle = start(), start(plan_flow=40.0)
    assert plain.plan_factor == 1.0 and gentle.plan_factor > 1.0
    assert gentle.delay_minutes == plain.delay_minutes == 10.0
    assert gentle.lead_minutes > plain.lead_minutes
    assert gentle.base_minutes_per_k == plain.minutes_per_k == 20.0
    assert abs(gentle.minutes_per_k - 20.0 * gentle.plan_factor) < 0.05


def test_hotter_plan_flow_is_faster_and_factor_is_capped():
    assert start(plan_flow=60.0).plan_factor < 1.0
    assert plan_factor(50.0, 30.0, 18.0, 21.0) == 2.0
    assert plan_factor(50.0, 200.0, 18.0, 21.0) == 0.5
    assert plan_factor(None, 40.0, 18.0, 21.0) == 1.0  # unknown supply leaves the plan alone


def test_lead_is_capped_by_the_max_lead():
    slow = predict_start(
        T0, SWITCH, 21.0, 12.0, MODEL, None, 65.0, StartParams(10, 60), plan_flow=40.0
    )
    assert slow.lead_minutes == 60 and slow.late_minutes > 0


# -- trajectory ----------------------------------------------------------------


def plan(**kw):
    base = dict(
        room_id="study",
        key=1,
        start_at=T0,
        switch_at=SWITCH,
        schedule_target=21.0,
        goal=21.0,
        start_temp=18.0,
        delay_minutes=10.0,
        climb_minutes=60.0,
        planned_minutes=100,
        started=True,
    )
    base.update(kw)
    return PreheatPlan(**base)


def test_expected_trajectory_flat_then_straight_line():
    p = plan()
    assert p.expected_at(T0 + timedelta(minutes=5)) == 18.0
    assert abs(p.expected_at(T0 + timedelta(minutes=40)) - 19.5) < 1e-9
    assert p.expected_at(T0 + timedelta(minutes=100)) == 21.0
    assert p.on_plan_k(T0 + timedelta(minutes=40), 19.2) == -0.3
    assert plan(started=False).on_plan_k(T0, 18.0) is None


def captured(value, **kw):
    """A started plan whose temperature at the switchpoint was captured as ``value``."""
    return plan(switch_temp=value, capture={"method": "nearest", "value": value}, **kw)


def test_the_lead_error_is_the_shortfall_at_the_switchpoint_in_minutes():
    # 3 K to climb in 60 minutes: 20 minutes per degree.
    assert lead_error_minutes(captured(20.4)) == 12.0  # 0.6 short
    assert lead_error_minutes(captured(21.5)) == -10.0  # half a degree over
    # Within 0.2 K either way is on time: a 0.1 K sensor step and a reading some minutes
    # from the switchpoint cannot tell it apart.
    assert lead_error_minutes(captured(20.85)) == 0.0
    assert lead_error_minutes(captured(20.8)) == 0.0
    assert lead_error_minutes(captured(21.2)) == 0.0
    assert lead_error_minutes(captured(20.7)) == 6.0
    # Nothing fair to learn from.
    assert lead_error_minutes(plan(switch_temp=None)) is None
    assert lead_error_minutes(plan(switch_temp=20.0)) is None  # a number nobody captured
    assert lead_error_minutes(captured(20.0, started=False)) is None
    # Held back by hot water: it waited for a charge, or began during one.
    assert lead_error_minutes(captured(20.0, dhw_wait_minutes=12.0)) is None
    assert lead_error_minutes(captured(20.0, dhw_wait_minutes=2.3)) is None
    assert lead_error_minutes(captured(20.0, start_reason="dhw_grace")) is None
    assert lead_error_minutes(captured(20.0, start_reason="latest_start")) is None
    # Beginning on the control cycle after the planned minute is not a wait.
    assert lead_error_minutes(captured(20.4, dhw_wait_minutes=0.7, start_reason="planned")) == 12.0
    assert lead_error_minutes(captured(20.0, start_temp=21.0)) is None
    assert lead_error_minutes(captured(20.0, interrupted=True)) is None
    failed = plan(switch_temp=20.0, capture={"method": None, "value": None, "reason": "x"})
    assert lead_error_minutes(failed) is None


def reports(*pairs, base=None):
    """A started plan with ``(minutes from the switchpoint, value)`` reports filed."""
    p = base or plan()
    for minutes, value in pairs:
        p = note_report(p, SWITCH + timedelta(minutes=minutes), value)
    return finalise_capture(p)


def test_the_switchpoint_temperature_is_interpolated_between_two_reports_around_it():
    p = reports((-8, 20.0), (11, 20.95))
    assert p.capture["method"] == "interpolated" and p.capture["gap_minutes"] == 19.0
    assert p.switch_temp == p.capture["value"] == 20.4  # 8/19 of the way from 20.0 to 20.95
    assert p.capture["before"] == [(SWITCH - timedelta(minutes=8)).isoformat(), 20.0]
    assert p.capture["after"] == [(SWITCH + timedelta(minutes=11)).isoformat(), 20.95]
    assert lead_error_minutes(p) == 12.0
    # The order they arrive in does not matter, and a report exactly on the switchpoint
    # is the reading itself.
    assert reports((11, 20.95), (-8, 20.0)).capture == p.capture
    assert reports((0, 20.6), (9, 21.0)).switch_temp == 20.6
    # Up to 25 minutes apart; 15 before and 10 after is the widest pair there is.
    assert reports((-15, 20.0), (10, 21.0)).capture["method"] == "interpolated"


def test_one_report_within_ten_minutes_is_taken_as_it_is_and_never_projected():
    p = reports((-9, 20.3))
    assert p.capture["method"] == "nearest" and p.switch_temp == 20.3
    assert p.capture["gap_minutes"] == 9.0 and p.capture["after"] is None
    assert reports((10, 20.9)).switch_temp == 20.9
    # Two reports on the same side: the one nearest the switchpoint, not a line through
    # them carried on to it.
    assert reports((-14, 19.0), (-6, 20.0)).switch_temp == 20.0
    assert reports((4, 20.5), (9, 21.0)).switch_temp == 20.5
    # A pair too far apart to interpolate: the nearer one, if it is near enough.
    wide = reports((-14, 20.0), (12, 21.0))
    assert wide.capture["method"] is None  # 26 minutes apart, neither within 10
    assert reports((-15, 20.0), (11, 21.0)).capture["method"] is None
    near = reports((-3, 20.2), (14, 21.0))  # 17 apart: interpolated
    assert near.capture["method"] == "interpolated"


def test_without_a_report_near_the_switchpoint_nothing_is_captured_or_learnt():
    for case in ([(-12, 20.3)], [(45, 21.4)], [(-180, 18.0)], [], [(11, 21.0)]):
        p = reports(*case)
        assert p.capture["method"] is None and p.capture["value"] is None
        assert p.switch_temp is None
        assert "no qualified report" in p.capture["reason"]
        error = lead_error_minutes(p)
        assert error is None and learn_lead(4.0, error, 0.5) == 4.0  # the lead stands
    # A report outside the quarter hour either side is not even kept.
    assert note_report(plan(), SWITCH + timedelta(minutes=45), 21.4) == plan()
    assert note_report(plan(), SWITCH - timedelta(minutes=16), 20.0) == plan()


def test_one_report_is_one_piece_of_evidence_however_often_it_is_seen():
    at = SWITCH - timedelta(minutes=9)
    once = note_report(plan(), at, 20.3)
    assert note_report(note_report(once, at, 20.3), at, 20.3) == once
    # The latest before the switchpoint and the earliest after it are the two kept.
    p = reports((-12, 20.0), (-9, 20.3), (-11, 20.1), (6, 20.8), (3, 20.6), (9, 21.0))
    assert p.capture["before"][1] == 20.3 and p.capture["after"][1] == 20.6
    # Once settled a capture does not move, whatever is reported afterwards.
    assert note_report(p, SWITCH + timedelta(minutes=1), 25.0) == p
    assert finalise_capture(p) == p


def test_an_interrupted_pre_heat_keeps_its_outcome_but_teaches_nothing():
    p = reports((-8, 20.0), (11, 20.95), base=plan(interrupted=True))
    assert p.obs_before is None and p.obs_after is None  # nothing is filed after a restart
    assert p.capture["method"] is None and "restart" in p.capture["reason"]
    assert lead_error_minutes(p) is None


def test_a_share_of_each_arrival_goes_into_the_next_start_within_limits():
    assert learn_lead(0.0, 12.0, 0.5) == 6.0  # short: start earlier
    assert learn_lead(6.0, -10.0, 0.5) == 1.0  # over: start later
    assert learn_lead(0.0, 100.0, 0.5) == 20.0  # one morning moves it 20 minutes at most
    assert learn_lead(55.0, 100.0, 0.5) == 60.0 and learn_lead(-25.0, -100.0, 0.5) == -30.0
    assert learn_lead(4.0, None, 0.5) == 4.0 and learn_lead(4.0, 12.0, 0) == 4.0


def test_the_learnt_lead_and_the_switch_temperature_round_trip():
    p = plan(switch_temp=20.4, lead_bias_minutes=6.0)
    again = PreheatPlan.from_dict(p.to_dict())
    assert again.switch_temp == 20.4 and again.lead_bias_minutes == 6.0
    # So do the reports around the switchpoint, the capture and an interruption, through
    # the JSON the store keeps.
    part = note_report(plan(lead_bias_minutes=6.0), SWITCH - timedelta(minutes=8), 20.0)
    again = PreheatPlan.from_dict(json.loads(json.dumps(part.to_dict())))
    assert again == part and again.capture is None
    done = reports((11, 20.95), base=again)
    stored = PreheatPlan.from_dict(json.loads(json.dumps(done.to_dict())))
    assert stored.capture == done.capture and stored.switch_temp == 20.4
    assert lead_error_minutes(stored) == 12.0
    cut = PreheatPlan.from_dict(json.loads(json.dumps(plan(interrupted=True).to_dict())))
    assert cut.interrupted and not PreheatPlan.from_dict(plan().to_dict()).interrupted


def test_held_back_plan_runs_from_when_it_began_and_round_trips():
    p = plan(started_at=T0 + timedelta(minutes=30))
    assert p.expected_at(T0 + timedelta(minutes=35)) == 18.0
    again = PreheatPlan.from_dict(p.to_dict())
    assert again == p


# -- hot water -----------------------------------------------------------------


def cloud(state, at):
    return SimpleNamespace(
        state="heat",
        attributes={"status": {"setpoints": {"next_sp_state": state, "next_sp_from": at}}},
    )


def test_next_dhw_on_only_for_an_on_period():
    assert next_dhw_on(cloud("On", "2026-10-22T05:30:00+00:00")) == T0 + timedelta(minutes=30)
    assert next_dhw_on(cloud("Off", "2026-10-22T05:30:00+00:00")) is None
    assert next_dhw_on(cloud("On", "junk")) is None
    assert next_dhw_on(None) is None
    assert next_dhw_on(SimpleNamespace(state="unavailable", attributes={})) is None


def test_charge_length_is_median_of_recent_charges_else_default():
    assert charge_length([]) == DEFAULT_CHARGE_MINUTES
    assert charge_length([30, 50]) == DEFAULT_CHARGE_MINUTES
    assert charge_length([30, 50, 35, 1000]) == 35


def test_start_moves_past_an_overlapping_charge_and_reports_late_risk():
    # start 05:30 for 07:00 with a 90 minute lead; a charge 05:45-06:25 overlaps it
    begin = T0 + timedelta(minutes=30)
    moved, late = avoid_charge(begin, SWITCH, 90, T0 + timedelta(minutes=45), 40)
    assert moved == T0 + timedelta(minutes=85) and late == 55  # 06:25 + 90 min = 07:55
    assert avoid_charge(begin, SWITCH, 90, T0 + timedelta(hours=3), 40) == (begin, 0)
    assert avoid_charge(begin, SWITCH, 90, T0 - timedelta(hours=2), 40) == (begin, 0)
    assert avoid_charge(begin, SWITCH, 90, None, 40) == (begin, 0)
    # a short lead leaves room after the charge: moved but not late
    assert avoid_charge(begin, SWITCH, 30, T0 + timedelta(minutes=45), 40) == (
        T0 + timedelta(minutes=85),
        0,
    )


# -- scoring -------------------------------------------------------------------


def test_hho_own_write_is_not_scored_as_evohome_early():
    samples = [[(T0 + timedelta(minutes=5 * i)).timestamp(), 18, 21.0, 5] for i in range(4)]
    switch = SWITCH.timestamp()
    assert evohome_start(samples, 21.0, switch)[:2] == (samples[0][0], True)
    hho = samples[2][0]
    # the setpoints before HHO's write were not at target; from it they are HHO's
    quiet = [[s[0], s[1], 16.0 if s[0] < hho else 21.0, 5] for s in samples]
    assert evohome_start(quiet, 21.0, switch, hho) == (hho, False, False)
    # a genuine earlier Evohome start still counts
    assert evohome_start(samples, 21.0, switch, hho)[:2] == (samples[0][0], True)


# -- room policy ---------------------------------------------------------------


def inputs(**kw):
    zone = ZoneState(
        16.0,
        16.0,
        next_switchpoint_at=T0 + timedelta(minutes=60),
        next_switchpoint_setpoint=21.0,
    )
    base = dict(
        now=T0,
        room_enabled=True,
        hub_enabled=True,
        holiday_mode=False,
        within_time_window=True,
        shadow_mode=False,
        computed_setpoint=16.4,
        zone=zone,
        memory=OverrideMemory(),
        preheat_setpoint=21.3,
    )
    base.update(kw)
    return PolicyInputs(**base)


def test_preheat_writes_the_goal_in_active_and_shadows_in_shadow():
    d = decide(inputs())
    assert d.state is State.PREHEAT and d.action is Action.WRITE and d.setpoint == 21.3
    s = decide(inputs(shadow_mode=True))
    assert s.state is State.SHADOW and s.action is Action.NONE and s.would_write == 21.3
    assert "pre-heat (shadow)" in s.reason
    # without a pre-heat the old behaviour stays: leave the zone to Evohome's optimum start
    old = decide(inputs(preheat_setpoint=None))
    assert old.state is State.PREHEAT and old.action is Action.NONE and "optimum" in old.reason


def test_preheat_needs_the_switchpoint_ahead():
    zone = ZoneState(16.0, 21.0, next_switchpoint_at=T0 - timedelta(minutes=1))
    assert decide(inputs(zone=zone)).state is not State.PREHEAT


def test_preheat_runs_before_the_operating_window_opens():
    # Live 25 Oct: windows open at 06:30, so the 05:45 and 06:22 starts waited until 06:30.
    d = decide(inputs(within_time_window=False))
    assert d.state is State.PREHEAT and d.action is Action.WRITE and d.setpoint == 21.3
    # Without a due pre-heat the window still keeps HHO's hands off.
    assert decide(inputs(within_time_window=False, preheat_setpoint=None)).state is (
        State.OUTSIDE_WINDOW
    )
    passed = ZoneState(16.0, 21.0, next_switchpoint_at=T0 - timedelta(minutes=1))
    late = decide(inputs(within_time_window=False, zone=passed))
    assert late.state is State.OUTSIDE_WINDOW


def test_manual_hold_and_open_window_beat_preheat():
    hold = OverrideMemory(
        manual_detected_at=T0 - timedelta(minutes=5),
        manual_release_at=T0 + timedelta(minutes=30),
        manual_setpoint=18.0,
    )
    zone = ZoneState(
        18.0,
        16.0,
        next_switchpoint_at=T0 + timedelta(minutes=60),
        next_switchpoint_setpoint=21.0,
    )
    assert decide(inputs(memory=hold, zone=zone)).state is State.MANUAL
    assert (
        decide(
            inputs(
                any_window_open=True,
                memory=OverrideMemory(window_open_since=T0 - timedelta(minutes=10)),
            )
        ).state
        is State.WINDOW_OPEN
    )


def test_own_preheat_override_is_not_manual_next_cycle_and_not_rewritten():
    first = decide(inputs())
    zone = ZoneState(
        21.3,
        16.0,
        next_switchpoint_at=T0 + timedelta(minutes=55),
        next_switchpoint_setpoint=21.0,
    )
    later = decide(inputs(now=T0 + timedelta(minutes=5), memory=first.memory, zone=zone))
    assert later.state is State.PREHEAT and later.action is Action.NONE
    # at the switchpoint the model setpoint is the same value: no new write
    after = ZoneState(
        21.3, 21.0, next_switchpoint_at=T0 + timedelta(hours=9), next_switchpoint_setpoint=16.0
    )
    refreshed = OverrideMemory(
        last_written_setpoint=21.3, last_written_at=T0 + timedelta(minutes=45)
    )
    handed = decide(
        inputs(
            now=T0 + timedelta(minutes=61),
            memory=refreshed,
            zone=after,
            computed_setpoint=21.3,
            preheat_setpoint=None,
        )
    )
    assert handed.state is State.ACTIVE and handed.action is Action.NONE
