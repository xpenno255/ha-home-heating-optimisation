"""Physical constraints, charge monitoring and room feedback regressions."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.control import (
    ChargeMonitor,
    DhwDemandTracker,
    RoomFeedback,
    effective_target,
)

T0 = datetime(2026, 9, 13, 10, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("requested", "lo", "hi", "step", "expected"),
    [
        (70, 5, 60, 1, 60),
        (55.6, 5, 70, 1, 56),
        (65, 5, 64.8, 1, 64),
        (35, 35, 30, 1, None),
        (50, 5, 90, 0, None),
        (float("nan"), 5, 90, 1, None),
    ],
)
def test_effective_target_respects_grid_and_cap(requested, lo, hi, step, expected):
    assert effective_target(requested, lo, hi, step) == expected


def test_inferred_dhw_requires_sustain_and_expires_without_evidence():
    state = DhwDemandTracker()
    assert not state.update(False, True, False, T0)
    assert state.update(False, True, False, T0 + timedelta(minutes=2))
    assert state.source == "inferred"
    assert state.update(None, False, False, T0 + timedelta(minutes=3))
    assert state.source == "grace"
    assert not state.update(None, False, False, T0 + timedelta(minutes=4))


def test_relay_dhw_is_immediate_and_definite_off_is_immediate():
    state = DhwDemandTracker()
    assert state.update(True, False, False, T0)
    assert not state.update(False, False, True, T0 + timedelta(seconds=10))


def test_charge_progress_fallback_lasts_until_charge_end():
    charge = ChargeMonitor()
    charge.update(True, 40, 60, T0, 30, 120)
    charge.update(True, 40.5, 60, T0 + timedelta(minutes=30), 30, 120)
    assert charge.fallback and charge.reason == "insufficient_temperature_progress"
    charge.update(True, 50, 60, T0 + timedelta(minutes=60), 30, 120)
    assert charge.fallback
    charge.update(False, 60, 60, T0 + timedelta(minutes=61), 30, 120)
    assert not charge.fallback and charge.started_at is None


def test_charge_normal_progress_and_timeout_even_without_sensor():
    charge = ChargeMonitor()
    charge.update(True, 40, 60, T0, 30, 120)
    charge.update(True, 45, 60, T0 + timedelta(minutes=30), 30, 120)
    assert not charge.fallback
    charge.update(True, None, 60, T0 + timedelta(minutes=120), 30, 120)
    assert charge.reason == "charge_timeout"


def test_room_assistance_requires_sustained_deficit_and_slow_recovery():
    rooms = RoomFeedback()
    assert rooms.update({"studio": (18, 20)}, T0, True) == (0, 2)
    correction, error = rooms.update({"studio": (18.2, 20)}, T0 + timedelta(minutes=10), True)
    assert correction == pytest.approx(3.6) and error == pytest.approx(1.8)
    assert rooms.update({"studio": (19.8, 20)}, T0 + timedelta(minutes=11), True)[0] == 0


def test_room_fast_recovery_and_schedule_change_do_not_boost():
    rooms = RoomFeedback()
    rooms.update({"studio": (17, 20)}, T0, True)
    assert rooms.update({"studio": (18, 20)}, T0 + timedelta(minutes=10), True)[0] == 0
    assert rooms.update({"studio": (18, 22)}, T0 + timedelta(minutes=11), True)[0] == 0
    rooms.update({}, T0 + timedelta(minutes=12), False)
    assert not rooms.samples
