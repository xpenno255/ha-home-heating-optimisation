"""Catch-up: a room stuck below its target raises the flow outside a planned pre-heat."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.warmup import (
    CatchupTrack,
    catchup_need,
    catchup_step,
    overdrive_k,
)
from custom_components.home_heating_optimisation.control.boiler.dhw_policy import (
    dhw_boiler_config,
)
from custom_components.home_heating_optimisation.control.boiler.dhw_policy import (
    validation_error as dhw_validation_error,
)
from custom_components.home_heating_optimisation.control.boiler.warmup_policy import (
    validation_error,
    warmup_config,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

T0 = datetime(2026, 10, 28, 19, 30, tzinfo=timezone.utc)


def room(air, target=19.7, demand=0.25, window=True, name="Living Room"):
    return [(name, air, target, demand, window)]


# -- pure rules -------------------------------------------------------------


def test_sustained_shortfall_without_recovery_is_pushed():
    tracks = catchup_step({}, room(19.4), T0)
    assert tracks == {"Living Room": CatchupTrack(T0, 19.4)}
    tracks = catchup_step(tracks, room(19.3), T0 + timedelta(minutes=19))
    assert not tracks["Living Room"].pushing
    tracks = catchup_step(tracks, room(19.3), T0 + timedelta(minutes=20))
    assert tracks["Living Room"].pushing


def test_a_room_recovering_on_its_own_is_not_pushed():
    tracks = catchup_step({}, room(19.0), T0)
    # +0.1 K in 20 minutes is 0.3 K an hour: already catching up.
    tracks = catchup_step(tracks, room(19.1), T0 + timedelta(minutes=20))
    assert not tracks["Living Room"].pushing


def test_a_pushed_room_stays_pushed_until_close():
    tracks = {"Living Room": CatchupTrack(T0, 19.3, pushing=True)}
    # Recovering fast because of the push, still 0.15 K short: carry on.
    tracks = catchup_step(tracks, room(19.55), T0 + timedelta(minutes=40))
    assert tracks["Living Room"].pushing
    assert catchup_step(tracks, room(19.65), T0 + timedelta(minutes=50)) == {}


def test_only_rooms_calling_inside_their_window_count():
    assert catchup_step({}, room(18.0, demand=0.0), T0) == {}
    assert catchup_step({}, room(18.0, demand=None), T0) == {}
    assert catchup_step({}, room(18.0, window=False), T0) == {}
    assert catchup_step({}, room(19.5), T0) == {}
    pushed = {"Living Room": CatchupTrack(T0, 19.3, pushing=True)}
    assert catchup_step(pushed, room(19.3, window=False), T0) == {}


def test_need_follows_the_worst_pushed_room():
    rooms = room(19.3) + room(18.9, target=19.0, name="Hall") + room(17.0, name="Study")
    tracks = {
        "Living Room": CatchupTrack(T0, 19.3, pushing=True),
        "Hall": CatchupTrack(T0, 18.9, pushing=True),
        "Study": CatchupTrack(T0, 17.0),  # far behind but not (yet) pushed
    }
    need, name, short = catchup_need(tracks, rooms, 50.0, 60.0)
    assert name == "Living Room" and short == pytest.approx(0.4)
    assert need == pytest.approx(10.0 * 0.3 / 0.7)
    full, _, _ = catchup_need(tracks, room(18.5), 50.0, 60.0)
    assert full == pytest.approx(10.0)
    assert catchup_need({}, rooms, 50.0, 60.0) == (0.0, None, None)


def test_config_default_off_and_validated():
    assert warmup_config({})["catchup_mode"] == "off"
    assert warmup_config({"warmup_boost": {"catchup_mode": "active"}})["catchup_mode"] == "active"
    assert warmup_config({"warmup_boost": {"catchup_mode": "bogus"}})["catchup_mode"] == "off"
    good = {"mode": "active", "start": 0.15, "full": 0.6, "decay_k_per_min": 1.0}
    assert validation_error({**good, "catchup_mode": "shadow"}) is None
    assert validation_error({**good, "catchup_mode": "bogus"}) == "warmup_boost_invalid"
    assert validation_error(good) is None


# -- boiler coordinator ------------------------------------------------------


def with_catchup(config, catchup, mode="off"):
    config = deepcopy(config)
    config["warmup_boost"] = {"mode": mode, "catchup_mode": catchup}
    return config


async def run(hass, controlled, freezer, catchup, mode="off"):  # noqa: F811
    entry, c, calls = await start(hass, with_catchup(controlled, catchup, mode))

    def demand():
        hass.states.async_set(
            "climate.study",
            "auto",
            {"current_temperature": 19.3, "temperature": 19.7, "heat_demand": 0.25},
        )

    c.boiler._warmup_rooms = lambda: [
        ("climate.study", None, True, "Living Room", 19.3, 19.7, True)
    ]
    demand()
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.refresh()
    assert c.boiler.data.catchup_uplift == 0.0 and c.boiler.data.catchup_room is None
    for _ in range(25):
        freezer.tick(timedelta(minutes=1))
        demand()
        await c.refresh()
    return entry, c


async def test_active_catchup_raises_the_flow(hass, controlled, sources, freezer):  # noqa: F811
    entry, c = await run(hass, controlled, freezer, "active")
    data = c.boiler.data
    assert data.catchup_room == "Living Room" and data.catchup_shortfall_k == pytest.approx(0.4)
    assert data.catchup_uplift > 0 and data.warmup_boost == data.catchup_uplift
    assert data.boost_flow == data.catchup_flow
    assert data.requested_target == pytest.approx(data.catchup_flow, abs=0.6)
    events = [e["data"]["event"] for e in entry.runtime_data.journal.events(kinds=["warmup_boost"])]
    assert "catchup_started" in events
    assert c.report()["warmup_boost"]["catchup_room"] == "Living Room"


async def test_shadow_catchup_only_publishes(hass, controlled, sources, freezer):  # noqa: F811
    _, c = await run(hass, controlled, freezer, "shadow")
    data = c.boiler.data
    assert data.catchup_uplift > 0 and data.catchup_room == "Living Room"
    assert data.warmup_boost == 0.0
    assert data.requested_target < data.catchup_flow - 1


# -- burn period during a catch-up -----------------------------------------------

BURN = "number.burn_min"


def test_catchup_minutes_default_off_and_need_the_entity():
    cfg = dhw_boiler_config({})
    assert cfg["catchup_minutes"] is None
    assert dhw_boiler_config({"dhw_boiler": {"catchup_minutes": 10}})["catchup_minutes"] == 10
    assert dhw_boiler_config({"dhw_boiler": {"catchup_minutes": 20}})["catchup_minutes"] is None
    assert dhw_validation_error({**cfg, "catchup_minutes": 10}) == "dhw_boiler_needs_entity"
    assert dhw_validation_error({**cfg, "catchup_minutes": 10, "anticycle_entity": BURN}) is None
    assert dhw_validation_error({**cfg, "catchup_minutes": 16, "anticycle_entity": BURN}) == (
        "dhw_boiler_invalid"
    )


async def burn_rig(hass, controlled, freezer, catchup="active", normal=15, minutes=10):  # noqa: F811
    hass.states.async_set(BURN, normal, {"unit_of_measurement": "min"})
    config = with_catchup(controlled, catchup)
    config["dhw_boiler"] = {"anticycle_entity": BURN, "catchup_minutes": minutes}
    entry, c, calls = await start(hass, config)
    air = {"value": 19.3}

    def demand():
        hass.states.async_set(
            "climate.study",
            "auto",
            {"current_temperature": air["value"], "temperature": 19.7, "heat_demand": 0.25},
        )

    c.boiler._warmup_rooms = lambda: [
        ("climate.study", None, True, "Living Room", air["value"], 19.7, True)
    ]

    async def tick(n=1):
        for _ in range(n):
            freezer.tick(timedelta(minutes=1))
            demand()
            await c.refresh()
        return c.boiler.data

    demand()
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.refresh()
    return entry, c, air, tick


def burn(hass):
    return float(hass.states.get(BURN).state)


async def test_active_catchup_lowers_the_burn_period_then_restores_it(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    entry, c, air, tick = await burn_rig(hass, controlled, freezer)
    data = await tick(5)
    assert data.catchup_uplift == 0.0 and burn(hass) == 15
    assert data.dhw_anticycle_state == "normal"
    data = await tick(20)
    assert data.catchup_uplift > 0 and burn(hass) == 10
    assert data.dhw_anticycle_state == "catchup" and data.dhw_anticycle_normal == 15
    assert c.boiler._store.get("anticycle_pending") is True
    # The room recovers: the uplift falls away, then the period goes back after 2 minutes.
    air["value"] = 19.7
    for _ in range(15):
        data = await tick()
        if burn(hass) == 15:
            break
    assert burn(hass) == 15 and data.catchup_uplift == 0.0
    data = await tick()
    assert data.dhw_anticycle_state == "normal"
    assert c.boiler._store.get("anticycle_pending") is False
    events = entry.runtime_data.journal.events(kinds=["dhw_anticycle"])
    sets = [e["data"] for e in events if e["data"]["event"] == "set"]
    assert sets and sets[0]["reason"] == "catchup" and sets[0]["value"] == 10


async def test_shadow_catchup_leaves_the_burn_period_alone(hass, controlled, sources, freezer):  # noqa: F811
    _, c, _, tick = await burn_rig(hass, controlled, freezer, catchup="shadow")
    data = await tick(25)
    assert data.catchup_uplift > 0 and burn(hass) == 15
    assert data.dhw_anticycle_state == "normal"


async def test_catchup_never_raises_a_lower_normal_period(hass, controlled, sources, freezer):  # noqa: F811
    _, c, _, tick = await burn_rig(hass, controlled, freezer, normal=8)
    data = await tick(25)
    assert data.catchup_uplift > 0 and burn(hass) == 8
    assert c.boiler._store.get("anticycle_pending") is not True


# -- overdrive ------------------------------------------------------------------


def test_overdrive_default_off_and_validated():
    assert warmup_config({})["catchup_overdrive_k"] is None
    assert (
        warmup_config({"warmup_boost": {"catchup_overdrive_k": 1.0}})["catchup_overdrive_k"] == 1.0
    )
    assert (
        warmup_config({"warmup_boost": {"catchup_overdrive_k": 3}})["catchup_overdrive_k"] is None
    )
    base = {"mode": "off", "catchup_mode": "active", "start": 0.15, "full": 0.4}
    base["decay_k_per_min"] = 1.0
    assert validation_error({**base, "catchup_overdrive_k": 1.0}) is None
    assert validation_error({**base, "catchup_overdrive_k": 2.5}) == "warmup_boost_invalid"


def test_overdrive_follows_the_shortfall_and_stops_early():
    assert overdrive_k(1.0, 1.4) == 1.0
    assert overdrive_k(1.0, 0.6) == 0.6
    assert overdrive_k(0.5, 0.6) == 0.5
    assert overdrive_k(1.0, 19.6 - 19.3) == 0.3
    assert overdrive_k(1.0, 0.29) == 0.0
    assert overdrive_k(None, 0.6) == 0.0
    assert overdrive_k(1.0, None) == 0.0


async def test_overdrive_only_for_a_pushed_room_in_active(hass, controlled, sources, freezer):  # noqa: F811
    entry, c = await run(hass, controlled, freezer, "active")
    boiler = c.boiler
    assert boiler.catchup_overdrive("Living Room") == 0.0  # no overdrive configured
    boiler._warmup_config = lambda: {
        "mode": "off",
        "catchup_mode": "active",
        "catchup_overdrive_k": 1.0,
    }
    # Living Room is 0.4 K short: the overdrive is its shortfall, under the 1.0 K limit.
    assert boiler.catchup_overdrive("Living Room") == 0.4
    assert boiler.catchup_overdrive("Kitchen") == 0.0
    boiler._warmup_config = lambda: {
        "mode": "off",
        "catchup_mode": "shadow",
        "catchup_overdrive_k": 1.0,
    }
    assert boiler.catchup_overdrive("Living Room") == 0.0


async def test_overdrive_raises_the_written_target_not_the_judged_one(hass, controlled, sources):  # noqa: F811
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    plain = [d for kind, d in calls if kind == "ramses"][-1]["setpoint"]
    assert plain == pytest.approx(room.data.air_setpoint) and room.data.catchup_overdrive_k == 0.0
    room._catchup_hint = lambda: 1.0
    await c.refresh()
    pushed = [d for kind, d in calls if kind == "ramses"][-1]["setpoint"]
    assert room.data.catchup_overdrive_k == 1.0
    assert pushed == pytest.approx(room.data.air_setpoint + 1.0, abs=0.06)
    assert "catch-up overdrive +1.0" in room.data.reason
    # Catch-up still judges the room against its normal air target.
    assert c.warmup_rooms()[0][5] == room.data.air_setpoint
    room._catchup_hint = lambda: 0.0
    await c.refresh()
    back = [d for kind, d in calls if kind == "ramses"][-1]["setpoint"]
    assert back == pytest.approx(room.data.air_setpoint, abs=0.06)


def calling(hass):
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 19.3, "temperature": 19.7, "heat_demand": 0.25},
    )
    hass.states.async_set("sensor.study_demand", 50, {"unit_of_measurement": "%"})
    hass.states.async_set("binary_sensor.heating", "on")


async def test_hot_water_pauses_catchup_without_resetting_it(hass, controlled, sources, freezer):  # noqa: F811
    entry, c = await run(hass, controlled, freezer, "active")
    boiler = c.boiler
    cfg = warmup_config({"warmup_boost": {"catchup_mode": "active", "catchup_overdrive_k": 1.0}})
    boiler._warmup_config = lambda: cfg
    assert boiler._catchup_tracks["Living Room"].pushing
    hass.states.async_set("sensor.hw", 100)
    for _ in range(5):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.hw", 100)
        calling(hass)
        await c.refresh()
    assert boiler.data.dhw_active
    assert boiler._catchup_tracks["Living Room"].pushing
    assert boiler.catchup_overdrive("Living Room") == 0.4
    # Straight back to pushing once the charge ends, without a new 20-minute wait.
    hass.states.async_set("sensor.hw", 0)
    for _ in range(3):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.hw", 0)
        calling(hass)
        await c.refresh()
    assert not boiler.data.dhw_active and boiler.data.catchup_uplift > 0
    assert boiler.data.catchup_room == "Living Room"
