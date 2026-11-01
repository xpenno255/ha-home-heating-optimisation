"""Overdrive: the written zone target follows how far the room is from its air target."""

from copy import deepcopy
from dataclasses import replace
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
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

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
    # Any lowering is immediate: a shrinking push, crossing over target, going further over.
    later = T0 + timedelta(minutes=11)
    assert overdrive_step(state, 0.1, later, period).value == 0.1
    state = overdrive_step(state, -0.1, later, period)
    assert state.value == -0.1
    state = overdrive_step(state, -0.3, later + timedelta(minutes=1), period)
    assert state.value == -0.3
    # Coming back up from a cut still waits for the period.
    soon = later + timedelta(minutes=5)
    assert overdrive_step(state, 0.0, soon, period) is state
    assert overdrive_step(state, 0.0, later + timedelta(minutes=12), period).value == 0.0
    # Unchanged keeps the original time.
    assert overdrive_step(state, -0.3, soon, period) is state


def test_settings_defaults_and_validation():
    cfg = warmup_config({})
    assert cfg["overdrive_strength"] == 1.0 and cfg["overdrive_period_min"] == 10
    assert cfg["room_check_minutes"] == 1
    assert warmup_config({"warmup_boost": {"room_check_minutes": 11}})["room_check_minutes"] == 1
    cfg = warmup_config({"warmup_boost": {"overdrive_strength": 2, "overdrive_period_min": 30}})
    assert cfg["overdrive_strength"] == 2.0 and cfg["overdrive_period_min"] == 30
    cfg = warmup_config({"warmup_boost": {"overdrive_strength": 9, "overdrive_period_min": 0}})
    assert cfg["overdrive_strength"] == 1.0 and cfg["overdrive_period_min"] == 10
    base = {"mode": "off", "catchup_mode": "off", "start": 0.15, "full": 0.4}
    base["decay_k_per_min"] = 1.0
    assert validation_error({**base, "overdrive_period_min": 1}) is None
    assert validation_error({**base, "overdrive_period_min": 31}) == "warmup_boost_invalid"
    assert validation_error({**base, "overdrive_strength": 0.1}) == "warmup_boost_invalid"
    assert validation_error({**base, "room_check_minutes": 0}) == "warmup_boost_invalid"


async def test_rooms_run_at_the_room_check_interval(hass, controlled):  # noqa: F811
    config = deepcopy(controlled)
    config["warmup_boost"] = {"room_check_minutes": 3}
    entry, c, calls = await start(hass, config)
    assert c.rooms["study"].update_interval == timedelta(minutes=3)


async def test_recent_writes_survive_the_room_store(hass, controlled):  # noqa: F811
    entry, c, calls = await start(hass, controlled)
    room = c.rooms["study"]
    memory = replace(room._memory(), recent_writes=((19.6, T0), (19.8, T0 + timedelta(minutes=2))))
    room._save_memory(memory)
    assert room._memory().recent_writes == memory.recent_writes
