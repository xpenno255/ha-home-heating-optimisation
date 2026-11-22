"""Heat spreading: enough radiators stay open for the boiler to deliver into."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from custom_components.home_heating_optimisation.control.boiler.warmup_policy import (
    validation_error,
    warmup_config,
)
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.control.spread import (
    HOLD_SECONDS,
    SpreadRoom,
    SpreadState,
    open_capacity,
    room_lift,
    spread_step,
)
from custom_components.home_heating_optimisation.scorecard.core import (
    add_spread,
    empty_day,
    finalise,
)
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

T0 = datetime(2026, 11, 22, 17, 0, tzinfo=timezone.utc)
HOLD = timedelta(seconds=HOLD_SECONDS)


def house(living=0.4, kitchen=0.5, hall=0.05, study=0.1):
    """Two big rooms at target, two small ones just warm with their valves nearly shut."""
    return [
        SpreadRoom("living", 0.0, living, 7.5),
        SpreadRoom("kitchen", 0.1, kitchen, 4.4),
        SpreadRoom("hall", 0.4, hall, 2.1),
        SpreadRoom("study", 0.3, study, 2.5),
    ]


# -- rules --------------------------------------------------------------------


def test_open_capacity_is_valve_times_rated_output():
    assert open_capacity(house()) == round(0.4 * 7.5 + 0.5 * 4.4 + 0.05 * 2.1 + 0.1 * 2.5, 2)
    assert open_capacity([SpreadRoom("a", 0.0, None, 3.0)]) == 0.0


def test_a_lift_puts_the_target_just_above_the_room_and_is_capped():
    assert room_lift(SpreadRoom("a", 0.0, 0.4, 2.0), 0.5) == 0.2  # on target
    assert room_lift(SpreadRoom("a", 0.2, 0.1, 2.0), 0.5) == 0.4  # a little warm
    assert room_lift(SpreadRoom("a", 0.5, 0.0, 2.0), 0.5) == 0.5  # never past the cap
    assert room_lift(SpreadRoom("a", 0.8, 0.0, 2.0), 0.5) == 0.5
    # Too warm to take part, short (overdrive has it), not eligible, no radiator, no data.
    assert room_lift(SpreadRoom("a", 0.9, 0.0, 2.0), 0.5) == 0.0
    assert room_lift(SpreadRoom("a", -0.3, 0.8, 2.0), 0.5) == 0.0
    assert room_lift(SpreadRoom("a", 0.2, 0.1, 2.0, eligible=False), 0.5) == 0.0
    assert room_lift(SpreadRoom("a", 0.2, 0.1, 0.0), 0.5) == 0.0
    assert room_lift(SpreadRoom("a", None, 0.1, 2.0), 0.5) == 0.0


def test_engages_only_while_calling_with_too_little_open():
    state, lifts, capacity = spread_step(SpreadState(), house(), True, T0, 8.0, 0.5)
    assert state.engaged and capacity < 8.0
    assert lifts == {"living": 0.2, "kitchen": 0.3, "hall": 0.5, "study": 0.5}
    # Not calling: nothing, however little is open.
    state, lifts, _ = spread_step(SpreadState(), house(), False, T0, 8.0, 0.5)
    assert not state.engaged and lifts == {}
    # Plenty open already: nothing.
    state, lifts, _ = spread_step(SpreadState(), house(living=0.9, kitchen=0.9), True, T0, 8.0, 0.5)
    assert not state.engaged and lifts == {}


def test_holds_then_lets_go_when_plenty_is_open_or_the_call_ends():
    state, _, _ = spread_step(SpreadState(), house(), True, T0, 8.0, 0.5)
    wide = house(living=0.9, kitchen=0.9, hall=0.6, study=0.6)
    # Inside the hold it stays engaged even with plenty open (its own doing).
    held, lifts, _ = spread_step(state, wide, True, T0 + timedelta(minutes=5), 8.0, 0.5)
    assert held.engaged and lifts
    # After the hold: released once well above the engage level ...
    after, lifts, capacity = spread_step(state, wide, True, T0 + HOLD, 8.0, 0.5)
    assert capacity >= 12.0 and not after.engaged and lifts == {}
    # ... but not when only a little above it.
    modest = house(living=0.7, kitchen=0.7)
    still, _, capacity = spread_step(state, modest, True, T0 + HOLD, 8.0, 0.5)
    assert 8.0 <= capacity < 12.0 and still.engaged
    # The end of the call releases at once, hold or not.
    ended, lifts, _ = spread_step(state, house(), False, T0 + timedelta(minutes=1), 8.0, 0.5)
    assert not ended.engaged and lifts == {}
    # And after a release it waits out the hold before engaging again.
    again, _, _ = spread_step(ended, house(), True, T0 + timedelta(minutes=2), 8.0, 0.5)
    assert not again.engaged
    again, _, _ = spread_step(ended, house(), True, T0 + timedelta(minutes=1) + HOLD, 8.0, 0.5)
    assert again.engaged


def test_settings_default_off_and_validated():
    cfg = warmup_config({})
    assert cfg["spread_mode"] == "off" and cfg["spread_exclude"] == []
    bad = {"spread_mode": "x", "spread_capacity_kw": 99, "spread_exclude": "study"}
    cfg = warmup_config({"warmup_boost": bad})
    assert cfg["spread_mode"] == "off" and cfg["spread_capacity_kw"] == 8.0
    assert cfg["spread_exclude"] == []
    good = warmup_config({"warmup_boost": {"spread_mode": "shadow", "spread_exclude": ["study"]}})
    assert good["spread_mode"] == "shadow" and good["spread_exclude"] == ["study"]
    assert validation_error(good) is None
    assert validation_error({**good, "spread_mode": "x"}) == "warmup_boost_invalid"
    assert validation_error({**good, "spread_max_k": 3}) == "warmup_boost_invalid"
    assert validation_error({**good, "spread_exclude": "study"}) == "warmup_boost_invalid"


# -- rooms --------------------------------------------------------------------


async def spreading(hass, controlled, mode, **extra):  # noqa: F811
    config = deepcopy(controlled)
    config["warmup_boost"] = {"spread_mode": mode, "catchup_overdrive_k": 1.0, **extra}
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 19.0, "temperature": 20, "heat_demand": 0.1},
    )
    entry, c, calls = await start(hass, config)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    await c.refresh()
    target = room.data.air_setpoint
    # The room is a little warm, its valve nearly shut, and the house is calling for heat.
    hass.states.async_set("sensor.air", round(target + 0.2, 2), {"unit_of_measurement": "°C"})
    return c, room, target


def calling(c):
    """Make the boiler side look as it does while Evohome is calling for heat."""
    from dataclasses import replace as _replace

    c.boiler.data = _replace(
        c.boiler.data, mode="heating", dhw_active=False, aggregate_heat_demand=25.0
    )


async def settle(c, room, freezer):
    """Two cycles: the first reads the room's new temperature, the second spreads on it."""
    for _ in range(2):
        calling(c)
        freezer.tick(timedelta(minutes=1))
        await room.async_refresh()


async def test_shadow_reports_the_lift_without_changing_the_target(  # noqa: F811
    hass, controlled, sources, freezer
):
    c, room, target = await spreading(hass, controlled, "shadow")
    await settle(c, room, freezer)
    report = c.report()["spread"]
    assert report["mode"] == "shadow" and report["engaged"] and report["calling"]
    assert room.data.spread_mode == "shadow" and room.data.spread_k == 0.4
    # The written target is still the overdrive's: taken off, because the room is warm.
    assert room.data.overdrive_k == -0.2
    assert "spreading" not in room.data.reason


async def test_active_lifts_a_warm_room_instead_of_taking_heat_off(  # noqa: F811
    hass, controlled, sources, freezer
):
    c, room, target = await spreading(hass, controlled, "active")
    await settle(c, room, freezer)
    assert room.data.spread_mode == "active" and room.data.spread_k == 0.4
    assert c.report()["spread"]["lifts"] == {room.room_name: 0.4}
    assert "spreading heat +0.4" in room.data.reason
    events = [e["data"]["event"] for e in c.heating.journal.events(kinds=["warmup_boost"])]
    assert "spread_engaged" in events


async def test_an_excluded_room_is_left_alone(hass, controlled, sources, freezer):  # noqa: F811
    c, room, target = await spreading(hass, controlled, "active", spread_exclude=["study"])
    await settle(c, room, freezer)
    assert c.report()["spread"]["engaged"] and room.data.spread_k == 0.0
    assert room.data.overdrive_k == -0.2
    assert "overdrive -0.2" in room.data.reason


async def test_nothing_without_a_call_for_heat(hass, controlled, sources, freezer):  # noqa: F811
    from dataclasses import replace as _replace

    c, room, target = await spreading(hass, controlled, "active")
    for _ in range(2):
        c.boiler.data = _replace(c.boiler.data, mode="heating", aggregate_heat_demand=0.0)
        freezer.tick(timedelta(minutes=1))
        await room.async_refresh()
    report = c.report()["spread"]
    assert not report["calling"] and not report["engaged"] and room.data.spread_k == 0.0


# -- scorecard ----------------------------------------------------------------


def test_scorecard_counts_spreading_hours():
    day = empty_day("2026-11-22")
    add_spread(day, 60, True, 4)
    add_spread(day, 30, True, 2)
    add_spread(day, 45, False, 0)
    row = finalise(day)
    assert row["spread_hours"] == 1.5 and row["spread_rooms"] == 3.3
    assert finalise(empty_day("2026-11-23"))["spread_hours"] == 0.0
