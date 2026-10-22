"""Planned pre-heat, pure parts: options, plan pace, trajectory, hot water, policy, scoring."""

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
    next_dhw_on,
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
