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
    STEER_PERIOD_S,
    SpreadInputs,
    SpreadRoom,
    SpreadState,
    boiler_floor,
    choose_lead,
    lead_valve,
    open_capacity,
    spread_step,
    steer_step,
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
PERIOD = timedelta(seconds=STEER_PERIOD_S)


def house(living=0.45, kitchen=0.5, hall=0.1, study=0.2, warm=0.0):
    """Two big rooms on target with their valves half open, two small ones nearly shut."""
    return [
        SpreadRoom("living", 0.0 + warm, living, 7.5),
        SpreadRoom("kitchen", 0.1 + warm, kitchen, 4.4),
        SpreadRoom("hall", 0.2 + warm, hall, 2.1),
        SpreadRoom("study", 0.1 + warm, study, 2.5),
    ]


def inputs(calling=True, relay=18.0, outdoor=5.0, floor=35.0, capacity=8.0, max_k=0.2, **more):
    return SpreadInputs(calling, relay, outdoor, capacity, max_k, floor, **more)


def run(steps, state=None):
    """Step through ``[(time, rooms, inputs)]``; returns the last result."""
    state, result = state or SpreadState(), None
    for now, rooms, given in steps:
        result = spread_step(state, rooms, given, now)
        state = result[0]
    return result


# -- rules --------------------------------------------------------------------


def test_open_capacity_is_valve_times_rated_output():
    assert open_capacity(house()) == round(0.45 * 7.5 + 0.5 * 4.4 + 0.1 * 2.1 + 0.2 * 2.5, 2)
    assert open_capacity([SpreadRoom("a", 0.0, None, 3.0)]) == 0.0


def test_the_floor_follows_the_weather():
    assert boiler_floor(35, 5.0) == 35.0
    assert boiler_floor(35, 15.5) == 0.0 and boiler_floor(35, 20) == 0.0
    assert boiler_floor(35, 10.25) == 17.5
    assert boiler_floor(35, -10) == 60.0  # capped
    assert boiler_floor(35, None) == 35.0
    assert boiler_floor(0, 5.0) == 0.0


def test_the_lead_valve_is_the_opening_that_earns_the_boiler_time():
    assert lead_valve(10) == 50 and lead_valve(18) == 50
    assert lead_valve(37) == 70
    assert lead_valve(35) == 68
    assert lead_valve(52) == 80
    assert lead_valve(60) == 85 and lead_valve(95) == 85  # never driven fully open


def test_rooms_at_or_a_little_over_target_take_part():
    assert taking_part(SpreadRoom("a", 0.0, 0.4, 2.0), 0.2)
    assert taking_part(SpreadRoom("a", 0.2, 0.0, 2.0), 0.2)
    assert taking_part(SpreadRoom("a", -0.1, 0.6, 2.0), 0.2)
    # Too warm, short (overdrive has it), not eligible, no radiator, no data.
    assert not taking_part(SpreadRoom("a", 0.3, 0.0, 2.0), 0.2)
    assert not taking_part(SpreadRoom("a", -0.3, 0.8, 2.0), 0.2)
    assert not taking_part(SpreadRoom("a", 0.1, 0.1, 2.0, eligible=False), 0.2)
    assert not taking_part(SpreadRoom("a", 0.1, 0.1, 0.0), 0.2)
    assert not taking_part(SpreadRoom("a", None, 0.1, 2.0), 0.2)


def test_a_steer_moves_a_step_at_a_time_towards_the_opening_wanted():
    # Well under the opening wanted: one step up, at once the first time.
    assert steer_step(0.0, None, 0.2, 40, T0, 1.0) == (0.3, T0)
    # Then not again until a period has passed.
    assert steer_step(0.3, T0, 0.2, 40, T0 + PERIOD / 2, 1.0) == (0.3, T0)
    assert steer_step(0.3, T0, 0.2, 40, T0 + PERIOD, 1.0) == (0.6, T0 + PERIOD)
    # Never past its ceiling.
    assert steer_step(0.9, None, 0.2, 40, T0, 1.0) == (1.0, T0)
    assert steer_step(1.0, None, 0.2, 40, T0, 1.0) == (1.0, None)
    # Inside the dead band nothing moves; well over, it eases back, never below nothing.
    assert steer_step(0.6, None, 0.35, 40, T0, 1.0) == (0.6, None)
    assert steer_step(0.6, None, 0.5, 40, T0, 1.0) == (0.6, None)
    assert steer_step(0.6, None, 0.6, 40, T0, 1.0) == (0.3, T0)
    assert steer_step(0.0, None, 0.9, 40, T0, 1.0) == (0.0, None)
    # No valve reading, no move.
    assert steer_step(0.3, None, None, 40, T0, 1.0) == (0.3, None)


def test_the_lead_is_the_coolest_room_and_is_not_swapped_lightly():
    rooms = house()
    assert choose_lead(None, rooms) == "living"
    # Kitchen is 0.1 warmer than the living room: it stays the lead if it already is.
    assert choose_lead("kitchen", rooms) == "kitchen"
    # Hall is 0.2 warmer: the living room takes over.
    assert choose_lead("hall", rooms) == "living"
    assert choose_lead("gone", rooms) == "living" and choose_lead(None, []) is None
    # On a tie the bigger radiator.
    tie = [SpreadRoom("small", 0.0, 0.1, 2.0), SpreadRoom("big", 0.0, 0.1, 7.0)]
    assert choose_lead(None, tie) == "big"


def test_engaged_every_room_is_steered_to_its_share_and_one_lead_further():
    state, lifts, info = spread_step(SpreadState(), house(), inputs(), T0)
    assert state.engaged and state.lead == "living"
    # Living room is the lead: 45 % open against 68 wanted. Kitchen is at its share
    # already. Hall and study are well under theirs.
    assert lifts == {"living": 0.3, "kitchen": 0.0, "hall": 0.3, "study": 0.3}
    assert info["valves"]["living"]["target"] == 68
    assert info["valves"]["hall"] == {"valve": 10, "target": 40.0, "steer_k": 0.3}
    # A period later the ones still short get another step.
    state, lifts, _ = spread_step(state, house(), inputs(), T0 + PERIOD)
    assert lifts == {"living": 0.6, "kitchen": 0.0, "hall": 0.6, "study": 0.6}
    # Valves that have reached their opening stop; one that overshoots is eased back.
    there = house(living=0.7, hall=0.4, study=0.6)
    state, lifts, _ = spread_step(state, there, inputs(), T0 + PERIOD * 2)
    assert lifts == {"living": 0.6, "kitchen": 0.0, "hall": 0.6, "study": 0.3}


def test_no_lead_while_the_boiler_call_is_at_the_floor_and_the_lead_is_dropped_well_above_it():
    state, lifts, _ = spread_step(SpreadState(), house(), inputs(relay=50.0), T0)
    assert state.engaged and state.lead is None  # engaged for capacity only
    assert lifts == {"living": 0.0, "kitchen": 0.0, "hall": 0.3, "study": 0.3}
    led = spread_step(SpreadState(), house(), inputs(relay=18.0), T0)[0]
    # Inside the dead band the lead stays; well above the floor it goes.
    assert spread_step(led, house(), inputs(relay=40.0), T0 + PERIOD)[0].lead == "living"
    assert spread_step(led, house(), inputs(relay=55.0), T0 + PERIOD)[0].lead is None


def test_a_room_that_stops_taking_part_loses_its_steer():
    state = run([(T0 + PERIOD * n, house(), inputs()) for n in range(3)])[0]
    assert state.steer["hall"][0] == 0.9
    rooms = house()
    rooms[2] = SpreadRoom("hall", 0.4, 0.3, 2.1)  # now 0.4 over: too warm
    state, lifts, _ = spread_step(state, rooms, inputs(), T0 + PERIOD * 3)
    assert "hall" not in lifts and "hall" not in state.steer
    # Back within range it starts again from nothing.
    state, lifts, _ = spread_step(state, house(), inputs(), T0 + PERIOD * 4)
    assert lifts["hall"] == 0.3


def test_a_warm_house_does_not_start_and_lets_go():
    assert not spread_step(SpreadState(), house(warm=0.2), inputs(max_k=0.5), T0)[0].engaged
    engaged = spread_step(SpreadState(), house(), inputs(max_k=0.5), T0)[0]
    # 0.3 warm overall: no lead any more, but still engaged.
    warm = spread_step(engaged, house(warm=0.25), inputs(max_k=0.5), T0 + HOLD)[0]
    assert warm.engaged and warm.lead is None
    # 0.5 warm: it lets go, and every steer with it.
    gone = spread_step(engaged, house(warm=0.4), inputs(max_k=0.6), T0 + HOLD)
    assert not gone[0].engaged and gone[1] == {} and gone[0].steer == {}
    # Nobody left to steer: it lets go at once, hold or not.
    gone = spread_step(engaged, house(warm=0.6), inputs(), T0 + timedelta(minutes=1))
    assert not gone[0].engaged


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


def test_it_does_not_let_go_because_of_the_capacity_it_opened_itself():
    state = spread_step(SpreadState(), house(), inputs(relay=50.0), T0)[0]
    wide = house(living=0.9, kitchen=0.9, hall=0.6, study=0.6)
    # Long after the hold, with plenty open and the boiler still being called: engaged.
    assert spread_step(state, wide, inputs(relay=50.0), T0 + HOLD * 3)[0].engaged
    # The call ends and the boiler call is not under the floor: it lets go ...
    quiet = inputs(calling=False, relay=0.0, floor=0.0)
    released = spread_step(state, wide, quiet, T0 + HOLD)[0]
    assert not released.engaged
    # ... but not inside the hold, and it waits out a hold before engaging again.
    assert spread_step(state, wide, quiet, T0 + timedelta(minutes=5))[0].engaged
    again = spread_step(released, house(), inputs(), T0 + HOLD + timedelta(minutes=2))[0]
    assert not again.engaged
    assert spread_step(released, house(), inputs(), T0 + HOLD * 2)[0].engaged


def test_settings_default_off_and_validated():
    cfg = warmup_config({})
    assert cfg["spread_mode"] == "off" and cfg["spread_exclude"] == []
    assert cfg["spread_floor_pct"] == 35.0 and cfg["spread_flow_c"] == 60.0
    assert cfg["spread_valve_pct"] == 40.0 and cfg["spread_steer_max_k"] == 1.0
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
    assert validation_error({**good, "spread_valve_pct": 5}) == "warmup_boost_invalid"
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
    assert report["lead"] == room.room_name and report["boiler_floor"] > 0
    assert report["rooms"][room.room_name] == {"valve": 10, "target": 68, "steer_k": 0.3}
    assert room.data.spread_mode == "shadow" and room.data.spread_k == 0.3
    # The written target is still the overdrive's: taken off, because the room is warm.
    assert room.data.overdrive_k == -0.2
    assert "spreading" not in room.data.reason


async def test_active_lifts_a_warm_room_instead_of_taking_heat_off(  # noqa: F811
    hass, controlled, sources, freezer
):
    c, room, target = await spreading(hass, controlled, "active")
    await settle(c, room, freezer)
    # The valve is 10 % open against the 68 % wanted of the lead: one step up, in place
    # of the take-off.
    assert room.data.spread_mode == "active" and room.data.spread_k == 0.3
    assert room.data.overdrive_k == -0.2 and "spreading heat +0.3" in room.data.reason
    events = [e["data"]["event"] for e in c.heating.journal.events(kinds=["warmup_boost"])]
    assert "spread" in events
    assert c.spread_flow_floor() == 60.0
    # The valve has not moved: another step each period.
    for _ in range(11):
        calling(c)
        freezer.tick(timedelta(minutes=1))
        await room.async_refresh()
    assert room.data.spread_k == 0.6 and "spreading heat +0.6" in room.data.reason


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
    assert report["engaged"] and report["lead"] == room.room_name and room.data.spread_k == 0.3


# -- scorecard ----------------------------------------------------------------


def test_scorecard_counts_spreading_hours():
    day = empty_day("2026-11-22")
    add_spread(day, 60, True, 4)
    add_spread(day, 30, True, 2)
    add_spread(day, 45, False, 0)
    row = finalise(day)
    assert row["spread_hours"] == 1.5 and row["spread_rooms"] == 3.3
    assert finalise(empty_day("2026-11-23"))["spread_hours"] == 0.0
