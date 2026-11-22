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
    LEVEL_PERIOD_S,
    SpreadInputs,
    SpreadRoom,
    SpreadState,
    boiler_floor,
    open_capacity,
    spread_step,
    taking_part,
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
PERIOD = timedelta(seconds=LEVEL_PERIOD_S)


def house(living=0.4, kitchen=0.5, hall=0.05, study=0.1, warm=0.0):
    """Two big rooms at target, two small ones just warm with their valves nearly shut."""
    return [
        SpreadRoom("living", 0.0 + warm, living, 7.5),
        SpreadRoom("kitchen", 0.1 + warm, kitchen, 4.4),
        SpreadRoom("hall", 0.4 + warm, hall, 2.1),
        SpreadRoom("study", 0.3 + warm, study, 2.5),
    ]


def inputs(calling=True, relay=18.0, outdoor=5.0, floor=35.0, capacity=8.0, max_k=0.5):
    return SpreadInputs(calling, relay, outdoor, capacity, max_k, floor)


def run(steps, state=None):
    """Step through ``[(time, rooms, inputs)]``; returns the last result."""
    state, result = state or SpreadState(), None
    for now, rooms, given in steps:
        result = spread_step(state, rooms, given, now)
        state = result[0]
    return result


# -- rules --------------------------------------------------------------------


def test_open_capacity_is_valve_times_rated_output():
    assert open_capacity(house()) == round(0.4 * 7.5 + 0.5 * 4.4 + 0.05 * 2.1 + 0.1 * 2.5, 2)
    assert open_capacity([SpreadRoom("a", 0.0, None, 3.0)]) == 0.0


def test_the_floor_follows_the_weather():
    assert boiler_floor(35, 5.0) == 35.0
    assert boiler_floor(35, 15.5) == 0.0 and boiler_floor(35, 20) == 0.0
    assert boiler_floor(35, 10.25) == 17.5
    assert boiler_floor(35, -10) == 60.0  # capped
    assert boiler_floor(35, None) == 35.0
    assert boiler_floor(0, 5.0) == 0.0


def test_rooms_at_or_a_little_over_target_take_part():
    assert taking_part(SpreadRoom("a", 0.0, 0.4, 2.0), 0.5)
    assert taking_part(SpreadRoom("a", 0.5, 0.0, 2.0), 0.5)
    assert taking_part(SpreadRoom("a", -0.1, 0.6, 2.0), 0.5)
    # Too warm, short (overdrive has it), not eligible, no radiator, no data.
    assert not taking_part(SpreadRoom("a", 0.6, 0.0, 2.0), 0.5)
    assert not taking_part(SpreadRoom("a", -0.3, 0.8, 2.0), 0.5)
    assert not taking_part(SpreadRoom("a", 0.2, 0.1, 2.0, eligible=False), 0.5)
    assert not taking_part(SpreadRoom("a", 0.2, 0.1, 0.0), 0.5)
    assert not taking_part(SpreadRoom("a", None, 0.1, 2.0), 0.5)


def test_engaging_first_only_stops_heat_being_taken_off():
    state, lifts, info = spread_step(SpreadState(), house(), inputs(), T0)
    assert state.engaged and state.level == 0.0
    assert lifts == {"living": 0.0, "kitchen": 0.0, "hall": 0.0, "study": 0.0}
    assert info["open_capacity_kw"] < 8.0 and info["boiler_floor"] == 35.0


def test_the_level_climbs_while_the_boiler_call_is_under_the_floor():
    steps = [(T0 + PERIOD * n, house(), inputs(relay=18.0)) for n in range(4)]
    state, lifts, _ = run(steps)
    assert state.level == 0.3 and set(lifts.values()) == {0.3}
    # Never past its ceiling.
    state, _, _ = run([(T0 + PERIOD * n, house(), inputs(relay=18.0)) for n in range(12)])
    assert state.level == 0.5
    # At the floor it stays where it is; well above it, it comes back down.
    climbed = run(steps)[0]
    steady = run([(T0 + PERIOD * 4, house(), inputs(relay=36.0))], climbed)[0]
    assert steady.level == 0.3
    eased = run([(T0 + PERIOD * 4, house(), inputs(relay=55.0))], climbed)[0]
    assert eased.level == 0.2


def test_a_warm_house_stops_the_climb_then_eases_then_lets_go():
    climbed = run([(T0 + PERIOD * n, house(), inputs()) for n in range(3)])[0]
    assert climbed.level == 0.2
    # Running about 0.2 warm (weighted by radiator size): no more climbing.
    held = run([(T0 + PERIOD * 3, house(warm=0.1), inputs())], climbed)[0]
    assert held.engaged and held.level == 0.2
    # About 0.3 warm: the level falls, though the boiler call is still under the floor.
    eased = run([(T0 + PERIOD * 3, house(warm=0.3), inputs())], climbed)[0]
    assert eased.engaged and eased.level == 0.1
    # A warm house does not start spreading at all.
    assert not spread_step(SpreadState(), house(warm=0.1), inputs(), T0)[0].engaged
    # Rooms that have all gone past the ceiling leave nobody to lift: it lets go at once.
    gone = run([(T0 + timedelta(minutes=1), house(warm=0.6), inputs())], climbed)
    assert not gone[0].engaged and gone[1] == {} and gone[0].level == 0.0


def test_it_engages_for_the_floor_alone_and_for_capacity_alone():
    wide = house(living=0.9, kitchen=0.9, hall=0.6, study=0.6)
    # Plenty open but the boiler call has collapsed: the floor engages it.
    assert spread_step(SpreadState(), wide, inputs(relay=18.0), T0)[0].engaged
    # Boiler call fine but too little open: capacity engages it.
    assert spread_step(SpreadState(), house(), inputs(relay=50.0), T0)[0].engaged
    # Both fine: nothing. Nor with the floor off and no call.
    assert not spread_step(SpreadState(), wide, inputs(relay=50.0), T0)[0].engaged
    quiet = inputs(calling=False, relay=0.0, floor=0.0)
    assert not spread_step(SpreadState(), house(), quiet, T0)[0].engaged
    # Mild weather: the floor is below the call, so a low call alone does not engage it.
    mild = inputs(relay=18.0, outdoor=12.0)
    assert not spread_step(SpreadState(), wide, mild, T0)[0].engaged


def test_holds_then_lets_go_and_waits_before_engaging_again():
    state = spread_step(SpreadState(), house(), inputs(), T0)[0]
    wide = house(living=0.9, kitchen=0.9, hall=0.6, study=0.6)
    fine = inputs(relay=55.0)
    # Inside the hold it stays engaged even with plenty open and the call well up.
    assert spread_step(state, wide, fine, T0 + timedelta(minutes=5))[0].engaged
    released = spread_step(state, wide, fine, T0 + HOLD)[0]
    assert not released.engaged and released.level == 0.0
    again = spread_step(released, house(), inputs(), T0 + HOLD + timedelta(minutes=2))[0]
    assert not again.engaged
    assert spread_step(released, house(), inputs(), T0 + HOLD * 2)[0].engaged


def test_settings_default_off_and_validated():
    cfg = warmup_config({})
    assert cfg["spread_mode"] == "off" and cfg["spread_exclude"] == []
    assert cfg["spread_floor_pct"] == 35.0 and cfg["spread_flow_c"] == 60.0
    bad = {"spread_mode": "x", "spread_capacity_kw": 99, "spread_exclude": "study"}
    cfg = warmup_config({"warmup_boost": bad})
    assert cfg["spread_mode"] == "off" and cfg["spread_capacity_kw"] == 8.0
    assert cfg["spread_exclude"] == []
    good = warmup_config({"warmup_boost": {"spread_mode": "shadow", "spread_exclude": ["study"]}})
    assert good["spread_mode"] == "shadow" and good["spread_exclude"] == ["study"]
    assert validation_error(good) is None
    assert validation_error({**good, "spread_mode": "x"}) == "warmup_boost_invalid"
    assert validation_error({**good, "spread_max_k": 3}) == "warmup_boost_invalid"
    assert validation_error({**good, "spread_floor_pct": 90}) == "warmup_boost_invalid"
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
    assert report["rooms"] == [room.room_name] and report["boiler_floor"] > 0
    assert room.data.spread_mode == "shadow" and room.data.spread_k == 0.0
    # The written target is still the overdrive's: taken off, because the room is warm.
    assert room.data.overdrive_k == -0.2
    assert "spreading" not in room.data.reason


async def test_active_lifts_a_warm_room_instead_of_taking_heat_off(  # noqa: F811
    hass, controlled, sources, freezer
):
    c, room, target = await spreading(hass, controlled, "active")
    await settle(c, room, freezer)
    # Level 0: the take-off is dropped, so the valve is no longer being shut.
    assert room.data.spread_mode == "active" and room.data.spread_k == 0.0
    assert room.data.overdrive_k == -0.2 and "spreading heat +0.0" in room.data.reason
    events = [e["data"]["event"] for e in c.heating.journal.events(kinds=["warmup_boost"])]
    assert "spread" in events
    assert c.spread_flow_floor() == 60.0
    # With the boiler call under the floor the level climbs a step each period.
    for _ in range(11):
        calling(c)
        freezer.tick(timedelta(minutes=1))
        await room.async_refresh()
    assert room.data.spread_k == 0.1 and "spreading heat +0.1" in room.data.reason


async def test_an_excluded_room_is_left_alone(hass, controlled, sources, freezer):  # noqa: F811
    c, room, target = await spreading(hass, controlled, "active", spread_exclude=["study"])
    await settle(c, room, freezer)
    # Nobody left to lift, so nothing engages and the room keeps its overdrive.
    assert not c.report()["spread"]["engaged"] and room.data.spread_k is None
    assert room.data.overdrive_k == -0.2
    assert "overdrive -0.2" in room.data.reason
    assert c.spread_flow_floor() is None


async def test_nothing_without_a_call_for_heat(hass, controlled, sources, freezer):  # noqa: F811
    from dataclasses import replace as _replace

    c, room, target = await spreading(hass, controlled, "active")
    for _ in range(2):
        c.boiler.data = _replace(c.boiler.data, mode="heating", aggregate_heat_demand=0.0)
        freezer.tick(timedelta(minutes=1))
        await room.async_refresh()
    report = c.report()["spread"]
    assert not report["calling"] and room.data.spread_mode == "active"
    # The floor still engages it: the boiler call has collapsed while the room is on target.
    assert report["engaged"] and room.data.spread_k == 0.0


# -- scorecard ----------------------------------------------------------------


def test_scorecard_counts_spreading_hours():
    day = empty_day("2026-11-22")
    add_spread(day, 60, True, 4)
    add_spread(day, 30, True, 2)
    add_spread(day, 45, False, 0)
    row = finalise(day)
    assert row["spread_hours"] == 1.5 and row["spread_rooms"] == 3.3
    assert finalise(empty_day("2026-11-23"))["spread_hours"] == 0.0
