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
    HOLD_MAX_K,
    HOLD_TAU_S,
    OverdriveState,
    hold_step,
    overdrive_step,
    overdrive_wanted,
    with_hold,
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


def test_the_hold_builds_near_target_and_fades_when_over():
    # A 0.3 K push kept up for the whole time constant becomes 0.3 K of hold.
    hold = 0.0
    for _ in range(HOLD_TAU_S // 60):
        hold = hold_step(hold, 0.2, 0.3, 60, 1.0)
    assert round(hold, 2) == 0.3
    # Slowly: an hour of it is a tenth.
    assert round(hold_step(0.0, 0.2, 0.3, 600, 1.0) * 6, 2) == 0.1
    # Never above its ceiling, nor above a smaller limit.
    assert hold_step(HOLD_MAX_K, 0.2, 0.3, 600, 1.0) == HOLD_MAX_K
    assert hold_step(0.2, 0.2, 0.3, 600, 0.2) == 0.2
    # Over target, however far, the push is negative and the hold shrinks to nothing.
    assert hold_step(0.3, -0.2, -0.2, 600, 1.0) < 0.3
    assert hold_step(0.3, -1.0, -0.5, 600, 1.0) < 0.3
    assert hold_step(0.01, -0.3, -0.3, 600, 1.0) == 0.0


def test_the_hold_only_learns_from_steady_running():
    # Well below target the room is recovering, not holding.
    assert hold_step(0.1, 0.8, 1.0, 60, 2.0) == 0.1
    assert hold_step(0.1, 0.3, 0.5, 60, 2.0) == 0.1
    # A long gap (restart, sensor drop-out) or no limit teaches nothing.
    assert hold_step(0.1, 0.2, 0.3, 3600, 1.0) == 0.1
    assert hold_step(0.1, 0.2, 0.3, None, 1.0) == 0.1
    assert hold_step(0.1, 0.2, 0.3, 60, None) == 0.1


def test_the_hold_is_a_floor_near_target_and_never_adds_to_a_push():
    assert with_hold(0.0, 0.24, 1.0, 0.0) == 0.2  # on target: the hold alone
    assert with_hold(0.1, 0.24, 1.0, 0.1) == 0.2  # a little short: still the hold
    assert with_hold(0.5, 0.2, 1.0, 0.3) == 0.5  # a real push is not made any bigger
    assert with_hold(1.0, 0.3, 1.0, 0.6) == 1.0
    assert with_hold(0.3, 0.3, 0.2, 0.2) == 0.2  # the room's limit
    # Over target it fades out: three quarters just over, half at 0.1 over (where it
    # about cancels what is taken off), gone at 0.2 over.
    assert with_hold(0.0, 0.3, 1.0, -0.05) == 0.2
    assert with_hold(-0.1, 0.24, 1.0, -0.1) == 0.0
    assert with_hold(-0.2, 0.3, 1.0, -0.2) == -0.2
    assert with_hold(-0.5, 0.3, 1.0, -1.0) == -0.5
    assert with_hold(0.5, 0.3, None, 0.3) == 0.0
    assert with_hold(0.5, 0.3, 1.0, None) == 0.0


def test_a_step_keeps_the_hold():
    state = OverdriveState(0.5, T0, hold=0.2, hold_at=T0)
    after = overdrive_step(state, 0.2, T0 + timedelta(minutes=1), 600)
    assert (after.value, after.hold, after.hold_at) == (0.2, 0.2, T0)


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


async def test_a_room_held_just_short_keeps_part_of_its_push_at_target(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.migration import handover

    hass.states.async_set("climate.study", "auto", {"current_temperature": 19.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    await c.refresh()
    target = room.data.air_setpoint
    # Ninety minutes a couple of tenths short: the push is there and a little is remembered.
    for _ in range(90):
        hass.states.async_set("sensor.air", round(target - 0.2, 2), {"unit_of_measurement": "°C"})
        freezer.tick(timedelta(minutes=1))
        await c.refresh()
    hold = room.data.overdrive_hold_k
    # The push itself is no bigger than it would be without the hold.
    assert 0.1 <= hold <= 0.2 and room.data.overdrive_k == 0.3
    assert room._store.get("overdrive")["hold"] > 0
    # On target: the remembered part stays on instead of the push dropping to nothing.
    hass.states.async_set("sensor.air", target, {"unit_of_measurement": "°C"})
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.overdrive_k == round(hold, 1) > 0
    # Sitting above target wears it away again.
    for _ in range(150):
        hass.states.async_set("sensor.air", round(target + 0.3, 2), {"unit_of_measurement": "°C"})
        freezer.tick(timedelta(minutes=1))
        await c.refresh()
    assert room.data.overdrive_hold_k == 0.0 and room.data.overdrive_k == -0.3


async def test_a_room_strength_pushes_harder_under_target_only(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.migration import handover

    hass.states.async_set("climate.study", "auto", {"current_temperature": 19.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    await c.refresh()
    target = room.data.air_setpoint

    async def at(offset):
        hass.states.async_set(
            "sensor.air", round(target + offset, 2), {"unit_of_measurement": "°C"}
        )
        room._overdrive = OverdriveState(hold=room._overdrive.hold, hold_at=room._overdrive.hold_at)
        freezer.tick(timedelta(minutes=1))
        await c.refresh()
        return room.data.overdrive_k

    # The house strength: +0.5 at 0.3 short, +0.3 at 0.2 short, -0.3 at 0.3 over.
    assert await at(-0.3) == 0.5 and await at(-0.2) == 0.3 and await at(0.3) == -0.3
    assert room.get_tunable("overdrive_strength") == 0.0
    # The room's own strength doubles the push under target, within the same limit...
    room.set_tunable("overdrive_strength", 2.0)
    assert await at(-0.3) == 1.0 and await at(-0.2) == 0.6 and await at(-0.5) == 1.0
    # ...and changes nothing over target.
    assert await at(0.3) == -0.3 and await at(0.1) == -0.1
    # Zero hands the room back to the house strength.
    room.set_tunable("overdrive_strength", 0.0)
    assert await at(-0.3) == 0.5


async def test_a_room_strength_does_not_fill_the_hold_faster(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.migration import handover

    async def held(strength):
        hass.states.async_set(
            "climate.study", "auto", {"current_temperature": 19.0, "temperature": 20}
        )
        entry, c, calls = await start(hass, controlled)
        await handover(c)
        await c.set_mode("study", "active")
        room = c.rooms["study"]
        settings = {
            "catchup_overdrive_k": 1.0,
            "overdrive_strength": 1.0,
            "overdrive_period_min": 10,
        }
        room._overdrive_settings = lambda: settings
        room.set_tunable("overdrive_strength", strength)
        room._overdrive = OverdriveState()
        await c.refresh()
        target = room.data.air_setpoint
        for _ in range(60):
            hass.states.async_set(
                "sensor.air", round(target - 0.2, 2), {"unit_of_measurement": "°C"}
            )
            freezer.tick(timedelta(minutes=1))
            await c.refresh()
        result = room._overdrive.hold, room.data.overdrive_k
        await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        return result

    hold, push = await held(0.0)
    hold_strong, push_strong = await held(2.0)
    # The push is twice as big, the hold has grown exactly as it would have without it.
    assert push == 0.3 and push_strong == 0.6
    assert hold > 0 and abs(hold_strong - hold) < 1e-9


async def test_the_hold_is_kept_while_the_room_is_outside_its_window(  # noqa: F811
    hass, controlled, sources
):
    from custom_components.home_heating_optimisation.control.migration import handover

    hass.states.async_set("climate.study", "auto", {"current_temperature": 19.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    await c.refresh()
    room._overdrive = OverdriveState(0.3, hold=0.2)
    room._within_time_window = lambda _now: False
    await c.refresh()
    assert room.data.overdrive_k == 0.0 and room._overdrive.hold == 0.2
    # Switched off altogether, nothing is kept.
    settings["catchup_overdrive_k"] = 0
    await c.refresh()
    assert room._overdrive == OverdriveState()


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


async def stale_room(hass, controlled):  # noqa: F811
    """A room whose sensor came back from a restart reading a degree below the truth."""
    from custom_components.home_heating_optimisation.control.migration import handover

    hass.states.async_set("climate.study", "auto", {"current_temperature": 21.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    room._overdrive_settings = lambda: settings
    # The room knows its sensor and the thermostat normally agree.
    room._store.set("sensor_gap", 0.0)
    return c, room


async def test_a_thermostat_reading_unchanged_since_the_start_is_checked_against_the_zone(  # noqa: F811
    hass, controlled, sources, freezer
):
    c, room = await stale_room(hass, controlled)
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp == 21.0 and "unchanged since" in room.data.air_temp_source
    assert any("unchanged since the start" in f for f in room.data.fallbacks)
    assert room.data.sensor_gap_k == 0.0 and room._store.get("sensor_gap") == 0.0
    # No push on the strength of the old reading.
    assert room.data.overdrive_k <= 0
    # The same value written again is not news: the integration does that as it starts.
    old = hass.states.get("sensor.air")
    hass.states.async_set("sensor.air", old.state, old.attributes, force_update=True)
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp == 21.0
    # A new reading: it is believed from then on, whatever it says.
    hass.states.async_set("sensor.air", 20.2, {"unit_of_measurement": "°C"})
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp == 20.2 and room.data.air_temp_source == "sensor.air"
    assert not any("unchanged since" in f for f in room.data.fallbacks)


async def test_a_separate_sensor_is_left_alone_and_a_quiet_one_is_accepted_in_time(  # noqa: F811
    hass, controlled, sources, freezer
):
    from custom_components.home_heating_optimisation.control.comfort.coordinator import (
        AIR_UNHEARD_MAX,
    )

    c, room = await stale_room(hass, controlled)
    reading = float(hass.states.get("sensor.air").state)
    # A separate room sensor (its usual gap to the thermostat is large) is left alone.
    assert abs(reading - 21.0) > 1.0
    room._store.set("sensor_gap", reading - 21.0)
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp == reading and room.data.air_temp_source == "sensor.air"
    # Disagreeing, it is replaced, but only for so long.
    room._store.set("sensor_gap", 0.0)
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.air_temp == 21.0
    freezer.tick(AIR_UNHEARD_MAX)
    await c.refresh()
    assert room.data.air_temp == reading and room.data.air_temp_source == "sensor.air"


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
