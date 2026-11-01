"""Overdrive: the written zone target follows how far the room is from its air target."""

from datetime import datetime, timedelta, timezone

from custom_components.home_heating_optimisation.control.boiler.warmup_policy import (
    validation_error,
    warmup_config,
)
from custom_components.home_heating_optimisation.control.comfort.core.overdrive import (
    OverdriveState,
    overdrive_step,
    overdrive_wanted,
)

T0 = datetime(2026, 11, 1, 10, 0, tzinfo=timezone.utc)


def test_scale_follows_the_shortfall():
    assert overdrive_wanted(0.0, 1.0, 1.0) == 0.0
    assert overdrive_wanted(0.04, 1.0, 1.0) == 0.0  # on target
    assert overdrive_wanted(0.1, 1.0, 1.0) == 0.1
    assert overdrive_wanted(0.2, 1.0, 1.0) == 0.3
    assert overdrive_wanted(0.3, 1.0, 1.0) == 0.5
    assert overdrive_wanted(0.5, 1.0, 1.0) == 0.8
    assert overdrive_wanted(0.6, 1.0, 1.0) == 1.0
    assert overdrive_wanted(0.25, 1.0, 1.0) == 0.4  # straight line between points
    # Past the table the last slope carries on, up to the limit.
    assert overdrive_wanted(0.7, 1.0, 2.0) == 1.2
    assert overdrive_wanted(1.5, 1.0, 1.0) == 1.0


def test_strength_and_limit():
    assert overdrive_wanted(0.2, 2.0, 2.0) == 0.6
    assert overdrive_wanted(0.3, 0.5, 1.0) == 0.2  # 0.25 rounds to the 0.1 step
    assert overdrive_wanted(0.5, 1.0, 0.5) == 0.5
    assert overdrive_wanted(0.5, 1.0, None) == 0.0
    assert overdrive_wanted(None, 1.0, 1.0) == 0.0


def test_over_target_takes_a_little_off():
    assert overdrive_wanted(-0.1, 1.0, 1.0) == -0.1
    assert overdrive_wanted(-0.3, 1.0, 1.0) == -0.3
    assert overdrive_wanted(-1.0, 1.0, 1.0) == -0.5  # never more than 0.5 off
    assert overdrive_wanted(-1.0, 1.0, 0.3) == -0.3  # nor more than the limit


def test_changes_at_most_once_per_period_but_eases_back_at_once():
    period = 600
    state = overdrive_step(OverdriveState(), 0.1, T0, period)
    assert state == OverdriveState(0.1, T0)
    # Growing waits for the period.
    assert overdrive_step(state, 0.3, T0 + timedelta(minutes=5), period) == state
    state = overdrive_step(state, 0.3, T0 + timedelta(minutes=10), period)
    assert state.value == 0.3
    # Easing towards the normal target is immediate, even across zero.
    later = T0 + timedelta(minutes=11)
    assert overdrive_step(state, 0.1, later, period).value == 0.1
    assert overdrive_step(state, -0.1, later, period).value == -0.1
    # Unchanged keeps the original time.
    assert overdrive_step(state, 0.3, later, period) is state


def test_settings_defaults_and_validation():
    cfg = warmup_config({})
    assert cfg["overdrive_strength"] == 1.0 and cfg["overdrive_period_min"] == 10
    cfg = warmup_config({"warmup_boost": {"overdrive_strength": 2, "overdrive_period_min": 30}})
    assert cfg["overdrive_strength"] == 2.0 and cfg["overdrive_period_min"] == 30
    cfg = warmup_config({"warmup_boost": {"overdrive_strength": 9, "overdrive_period_min": 0}})
    assert cfg["overdrive_strength"] == 1.0 and cfg["overdrive_period_min"] == 10
    base = {"mode": "off", "catchup_mode": "off", "start": 0.15, "full": 0.4}
    base["decay_k_per_min"] = 1.0
    assert validation_error({**base, "overdrive_period_min": 1}) is None
    assert validation_error({**base, "overdrive_period_min": 31}) == "warmup_boost_invalid"
    assert validation_error({**base, "overdrive_strength": 0.1}) == "warmup_boost_invalid"
