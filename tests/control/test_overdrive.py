"""Overdrive: the written zone target follows how far the room is from its air target."""

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

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
    assert warmup_config({})["room_write_step"] == 0.3
    assert warmup_config({"warmup_boost": {"room_write_step": 0.1}})["room_write_step"] == 0.1
    assert warmup_config({"warmup_boost": {"room_write_step": 0.6}})["room_write_step"] == 0.3
    assert validation_error({**base, "room_write_step": 0.5}) is None
    assert validation_error({**base, "room_write_step": 0.05}) == "warmup_boost_invalid"


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


async def test_a_room_limit_replaces_the_house_limit(hass, controlled, sources):  # noqa: F811
    from custom_components.home_heating_optimisation.control.migration import handover

    hass.states.async_set("climate.study", "auto", {"current_temperature": 17.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    await c.refresh()
    assert room.data.overdrive_k == 1.0  # house limit
    room.set_tunable("overdrive_limit", 2.0)
    room._overdrive = OverdriveState()
    await c.refresh()
    assert room.data.overdrive_k == 2.0
    room.set_tunable("overdrive_limit", 0.0)
    room._overdrive = OverdriveState()
    await c.refresh()
    assert room.data.overdrive_k == 1.0


async def test_overdrive_is_frozen_while_the_room_sensor_is_missing(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.comfort.coordinator import (
        AIR_FALLBACK_HOLD,
    )
    from custom_components.home_heating_optimisation.control.migration import handover

    # The thermostat's own sensor reads far warmer than the room sensor.
    hass.states.async_set("climate.study", "auto", {"current_temperature": 22.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    await c.refresh()
    assert room.data.air_temp_source == "sensor.air" and room.data.overdrive_k == 1.0
    assert room._store.get("overdrive")["value"] == 1.0
    # The room sensor drops out: the air reading falls back, but the push is not cut.
    hass.states.async_set("sensor.air", "unavailable")
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp_source != "sensor.air" and room.data.air_temp == 22.0
    assert room.data.overdrive_k == 1.0
    # Still missing after the hold: the thermostat's reading is accepted.
    freezer.tick(AIR_FALLBACK_HOLD + timedelta(minutes=1))
    await c.refresh()
    assert room.data.overdrive_k < 0
    # Back again: the hold is rearmed for the next drop-out.
    hass.states.async_set("sensor.air", 18, {"unit_of_measurement": "°C"})
    freezer.tick(timedelta(minutes=11))
    await c.refresh()
    assert room.data.overdrive_k == 1.0 and room._air_fallback_since is None


async def test_overdrive_carries_over_a_restart_and_waits_for_start_up(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.comfort.coordinator import (
        OVERDRIVE_RESTORE_MAX_AGE,
        OTCoordinator,
    )

    entry, c, calls = await start(hass, controlled)
    room = c.rooms["study"]
    now = dt_util.utcnow()
    stamp = {"value": 0.6, "changed_at": now.isoformat(), "seen_at": now.isoformat()}
    room._store.set("overdrive", stamp)
    restored = OTCoordinator._restored_overdrive(room._store)
    assert restored.value == 0.6 and restored.changed_at == now
    # While the room is still starting up, nothing moves it.
    room._overdrive = restored
    room._restore_complete = False
    assert room._hold_overdrive(room.data, now, "sensor.air") is True
    assert room._overdrive_now(room.data, now, False, hold=True) == 0.6
    room._restore_complete = True
    room._startup_reconciliation_pending = True
    assert room._hold_overdrive(room.data, now, "sensor.air") is True
    room._startup_reconciliation_pending = False
    assert room._hold_overdrive(room.data, now, "sensor.air") is False
    # Too old, absent or nonsense: start from nothing.
    freezer.tick(OVERDRIVE_RESTORE_MAX_AGE + timedelta(minutes=1))
    assert OTCoordinator._restored_overdrive(room._store).value == 0.0
    for bad in (None, {}, {"value": "x", "seen_at": now.isoformat()}, {"value": 99, "seen_at": 1}):
        room._store.set("overdrive", bad)
        assert OTCoordinator._restored_overdrive(room._store) == OverdriveState()
