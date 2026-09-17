"""Tests for hub.py: demand low-pass filter, toggle counter, return freshness, write memory."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.boiler.hub import BoilerFlowHub

T0 = datetime(2026, 1, 15, 7, 0, tzinfo=timezone.utc)


def test_demand_filter_none_returns_current_value():
    hub = BoilerFlowHub()
    assert hub.sample_demand(None, T0) is None


def test_demand_filter_seeds_then_smooths():
    hub = BoilerFlowHub()
    assert hub.sample_demand(50.0, T0) == pytest.approx(50.0)
    # 10 minutes later, tau=10min -> alpha=0.5
    value = hub.sample_demand(100.0, T0 + timedelta(minutes=10))
    assert value == pytest.approx(75.0)


def test_demand_filter_holds_value_when_input_drops_out():
    hub = BoilerFlowHub()
    hub.sample_demand(50.0, T0)
    assert hub.sample_demand(None, T0 + timedelta(minutes=5)) == pytest.approx(50.0)


def test_demand_filter_snaps_to_zero_instead_of_decaying_forever():
    hub = BoilerFlowHub()
    hub.sample_demand(50.0, T0)
    now = T0
    for _ in range(120):  # two hours of zero demand at 1-minute samples
        now = now + timedelta(minutes=1)
        value = hub.sample_demand(0.0, now)
    assert value == 0.0


def test_demand_filter_zero_snap_only_when_raw_is_zero():
    hub = BoilerFlowHub()
    hub.sample_demand(0.05, T0)
    value = hub.sample_demand(0.05, T0 + timedelta(minutes=1))
    assert value == pytest.approx(0.05)


def test_ignition_counter_counts_sub_minute_events():
    # change 3: event-driven, so ignitions closer together than the 60 s poll
    # interval (6 starts in 5 min observed in the field) are all counted.
    hub = BoilerFlowHub()
    now = T0
    for _ in range(6):
        now = now + timedelta(seconds=10)
        count = hub.record_ignition(now)
    assert count == 6


def test_ignition_counter_prunes_outside_window():
    hub = BoilerFlowHub()
    now = T0
    hub.record_ignition(now)  # ignition 1
    now = now + timedelta(minutes=1)
    hub.record_ignition(now)  # ignition 2
    now = now + timedelta(minutes=15)  # well outside the 10-minute window
    count = hub.record_ignition(now)  # ignition 3, but 1 & 2 have aged out
    assert count == 1


def test_cycles_10min_reads_current_window_without_adding():
    hub = BoilerFlowHub()
    now = T0
    hub.record_ignition(now)
    assert hub.cycles_10min(now + timedelta(minutes=1)) == 1
    assert hub.cycles_10min(now + timedelta(minutes=11)) == 0


def test_return_freshness():
    hub = BoilerFlowHub()
    value, fresh = hub.sample_return(55.0, T0, T0)
    assert value == 55.0 and fresh
    # stale sample: no new reading for 11 minutes, but the last value is still reported
    value, fresh = hub.sample_return(None, None, T0 + timedelta(minutes=11))
    assert value == 55.0 and not fresh
    # fresh again once a new reading arrives
    value, fresh = hub.sample_return(60.0, T0 + timedelta(minutes=11), T0 + timedelta(minutes=11))
    assert value == 60.0 and fresh


def test_return_freshness_uses_sensor_last_reported_not_poll_time():
    # v0.2.1 review fix 5: a wedged-but-numeric sensor (last_reported frozen in
    # the past) must go stale even though every 60 s poll sees a numeric state.
    hub = BoilerFlowHub()
    stuck_at = T0
    value, fresh = hub.sample_return(55.0, stuck_at, T0)
    assert value == 55.0 and fresh
    # 11 "polls" later the sensor still reports the same last_reported timestamp
    # (it is wedged), even though its state is still numeric.
    value, fresh = hub.sample_return(55.0, stuck_at, T0 + timedelta(minutes=11))
    assert value == 55.0 and not fresh


def test_return_freshness_falls_back_to_now_when_no_last_reported():
    hub = BoilerFlowHub()
    value, fresh = hub.sample_return(55.0, None, T0)
    assert value == 55.0 and fresh


def test_ignitions_since_none_counts_whole_window():
    hub = BoilerFlowHub()
    now = T0
    hub.record_ignition(now)
    hub.record_ignition(now + timedelta(minutes=1))
    assert hub.ignitions_since(None, now + timedelta(minutes=2)) == 2


def test_ignitions_since_only_counts_after_intervention():
    hub = BoilerFlowHub()
    now = T0
    hub.record_ignition(now)  # before intervention
    intervention_at = now + timedelta(minutes=1)
    hub.record_ignition(now + timedelta(minutes=2))  # after intervention
    hub.record_ignition(now + timedelta(minutes=3))  # after intervention
    assert hub.ignitions_since(intervention_at, now + timedelta(minutes=4)) == 2


def test_write_memory_round_trip():
    hub = BoilerFlowHub()
    assert hub.write_memory().last_written_setpoint is None
    hub.record_write(52.5, T0, target_changed=True)
    m = hub.write_memory()
    assert (
        m.last_written_setpoint == 52.5 and m.last_written_at == T0 and m.last_target_change == T0
    )


def test_write_memory_re_assert_advances_last_written_at_only():
    hub = BoilerFlowHub()
    hub.record_write(52.5, T0, target_changed=True)
    later = T0 + timedelta(minutes=1)
    hub.record_write(52.5, later, target_changed=False)
    m = hub.write_memory()
    assert m.last_written_setpoint == 52.5
    assert m.last_written_at == later  # advances every re-assertion
    assert m.last_target_change == T0  # target itself has not changed
