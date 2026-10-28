"""Catch-up: a room stuck below its target raises the flow outside a planned pre-heat."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.warmup import (
    CatchupTrack,
    catchup_need,
    catchup_step,
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
