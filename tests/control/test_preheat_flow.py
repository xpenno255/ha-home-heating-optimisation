"""Flow during a planned pre-heat: fullness, cap, need, ramps (pure) and the boiler."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.warmup import (
    FullnessState,
    WarmupBoostParams,
    WarmupBoostState,
    fullness,
    fullness_cap,
    fullness_step,
    need_uplift,
    preheat_step,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

T0 = datetime(2026, 10, 23, 5, 30, tzinfo=timezone.utc)
PARAMS = WarmupBoostParams(boost_max=65.0)


def room(behind, done=False):
    return SimpleNamespace(room_id="r", name="R", behind_k=behind, done=done)


# -- fullness ------------------------------------------------------------------


def test_fullness_maths_and_unknown_cases():
    assert fullness(55.0, 40.0, 20.0) == pytest.approx(20 / 35)
    assert fullness(None, 40.0, 20.0) is None
    assert fullness(55.0, None, 20.0) is None
    assert fullness(55.0, 40.0, None) is None
    assert fullness(22.0, 21.0, 20.0) is None  # under 3 K of span
    assert fullness(23.0, 21.0, 20.0) == pytest.approx(1 / 3)


def test_filter_moves_only_while_firing_and_holds_otherwise():
    state = fullness_step(FullnessState(), 0.3, True, T0)
    assert state.value == 0.3
    held = fullness_step(state, 0.9, False, T0 + timedelta(minutes=5))
    assert held == state
    assert fullness_step(state, None, True, T0 + timedelta(minutes=5)) == state
    later = fullness_step(state, 0.9, True, T0 + timedelta(seconds=60))
    assert 0.3 < later.value < 0.9
    # three-minute time constant: a minute moves it about a quarter of the way
    assert later.value == pytest.approx(0.3 + 0.6 * 60 / 240)
    # a long pause does not make the first firing reading jump the filter
    after_pause = fullness_step(later, 0.9, True, T0 + timedelta(hours=1))
    assert after_pause.value < 0.9


def test_cap_is_linear_between_low_and_high_and_full_when_unknown():
    cap = lambda f: fullness_cap(f, 45.0, 65.0, 0.45, 0.65)  # noqa: E731
    assert cap(0.3) == 20.0 and cap(0.45) == 20.0
    assert cap(0.55) == pytest.approx(10.0)
    assert cap(0.65) == 0.0 and cap(0.9) == 0.0
    assert cap(None) == 20.0


# -- need and ramp ---------------------------------------------------------------


def test_need_follows_the_worst_room_behind_plan_and_ignores_done_rooms():
    need = lambda rooms: need_uplift(rooms, 45.0, 65.0, PARAMS)  # noqa: E731
    assert need([]) == 0.0
    assert need([room(0.0), room(-0.4)]) == 0.0  # on or ahead of plan
    assert need([room(0.25)]) == pytest.approx(10.0)
    assert need([room(0.25), room(0.5)]) == 20.0
    assert need([room(2.0)]) == 20.0
    assert need([room(0.5, done=True), room(0.1)]) == pytest.approx(4.0)
    assert need([room(None)]) == 0.0


def test_ramp_rises_at_most_1k_a_minute_and_falls_at_most_the_decay():
    state = WarmupBoostState(0.0, T0)
    up = preheat_step(state, 15.0, 45.0, T0 + timedelta(minutes=2), PARAMS)
    assert up.uplift == pytest.approx(2.0)
    down = preheat_step(WarmupBoostState(10.0, T0), 0.0, 45.0, T0 + timedelta(minutes=3), PARAMS)
    assert down.uplift == pytest.approx(7.0)
    assert (
        preheat_step(
            WarmupBoostState(10.0, T0), 9.5, 45.0, T0 + timedelta(minutes=3), PARAMS
        ).uplift
        == 9.5
    )
    # never above the headroom to the maximum flow
    assert (
        preheat_step(
            WarmupBoostState(19.5, T0), 30.0, 45.0, T0 + timedelta(minutes=5), PARAMS
        ).uplift
        == 20.0
    )


# -- boiler ------------------------------------------------------------------------


def configured(config, warm="off"):
    config = deepcopy(config)
    boiler = config["control"]["boiler"]["config"]
    boiler["current_flow_entity"] = "sensor.flow"
    boiler["return_temp_entity"] = "sensor.return"
    boiler["heating_active_entity"] = "binary_sensor.heating"
    config["warmup_boost"] = {"mode": warm}
    return config


class Rig:
    def __init__(self, hass, c, freezer):
        self.hass, self.c, self.freezer = hass, c, freezer
        self.air, self.behind, self.flow, self.ret, self.done = 18.0, 0.5, 50.0, 28.0, False

    def states(self):
        h = self.hass
        h.states.async_set(
            "climate.study",
            "auto",
            {"current_temperature": 18, "temperature": 20, "heat_demand": 0.9},
        )
        h.states.async_set("sensor.flow", self.flow, {"unit_of_measurement": "°C"})
        h.states.async_set("sensor.return", self.ret, {"unit_of_measurement": "°C"})
        h.states.async_set("binary_sensor.heating", "on")
        h.states.async_set("sensor.outdoor", 5, {"unit_of_measurement": "°C"})
        h.states.async_set("sensor.study_demand", 50, {"unit_of_measurement": "%"})
        h.states.async_set("sensor.cylinder", 50, {"unit_of_measurement": "°C"})

    async def tick(self, minutes=1):
        self.freezer.tick(timedelta(minutes=minutes))
        self.states()
        await self.c.refresh()
        return self.c.boiler.data


async def rig(hass, controlled, freezer, pre, warm="off", rooms=True):  # noqa: F811
    entry, c, calls = await start(hass, configured(controlled, warm))
    b = c.boiler
    r = Rig(hass, c, freezer)
    b._preheat_config = lambda: {"mode": pre}
    b._warmup_rooms = lambda: [("climate.study", None, True, "Study", r.air, 19.0)]
    b._preheat_rooms = lambda _now: (
        [SimpleNamespace(room_id="study", name="Study", behind_k=r.behind, done=r.done)]
        if rooms
        else []
    )
    r.states()
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.refresh()
    return r, c, calls


async def test_shadow_publishes_the_plan_uplift_but_writes_nothing_extra(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    r, c, calls = await rig(hass, controlled, freezer, "shadow")
    quiet = c.boiler.data.requested_target
    for _ in range(4):
        data = await r.tick()
    assert data.preheat_mode == "shadow" and data.preheat_rooms == 1
    assert data.preheat_uplift > 0 and data.preheat_need == pytest.approx(data.fullness_cap, abs=20)
    assert data.preheat_flow > quiet and data.warmup_boost == 0.0
    assert data.requested_target == quiet
    assert data.radiator_fullness is not None


async def test_active_uplift_is_min_of_need_and_cap_and_falls_as_return_rises(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    r, c, calls = await rig(hass, controlled, freezer, "active")
    base = c.boiler.data.requested_target
    last = 0.0
    for _ in range(6):
        data = await r.tick()
        assert 0 < data.warmup_boost <= last + 1.0 + 1e-6 or last == 0.0
        last = data.warmup_boost
    assert data.warmup_boost >= 4.0 and data.requested_target > base
    assert data.warmup_boost == data.preheat_uplift
    # The radiators fill (return climbs, F passes the high mark) while the room is still
    # behind plan: the uplift falls, at most a kelvin a minute.
    r.ret = 45.0
    falling = []
    for _ in range(12):
        data = await r.tick()
        falling.append(data.warmup_boost)
    assert data.radiator_fullness > 0.6
    assert falling[-1] < last
    deltas = [b - a for a, b in zip([last, *falling], falling)]
    assert max(deltas) <= 1.0 + 1e-6 and min(deltas) >= -1.0 - 1e-6
    assert falling[-1] < max(falling) and falling[-1] < falling[3]
    assert data.fullness_cap < 5 and data.preheat_need > data.fullness_cap


async def test_on_plan_rooms_need_no_uplift_in_active(hass, controlled, sources, freezer):  # noqa: F811
    r, c, _ = await rig(hass, controlled, freezer, "active")
    r.behind = -0.2
    for _ in range(3):
        data = await r.tick()
    assert data.warmup_boost == 0.0 and data.preheat_need == 0.0


async def test_without_pre_heating_rooms_the_house_demand_boost_is_unchanged(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    r, c, _ = await rig(hass, controlled, freezer, "active", warm="active", rooms=False)
    data = await r.tick()
    assert data.preheat_rooms == 0 and data.preheat_uplift == 0.0
    assert data.warmup_boost > 5 and data.house_demand == pytest.approx(0.9)
    assert data.radiator_fullness is not None


async def test_plan_replaces_the_house_demand_boost_while_rooms_pre_heat(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    r, c, _ = await rig(hass, controlled, freezer, "active", warm="active")
    r.behind = 0.0
    data = await r.tick()
    assert data.preheat_rooms == 1 and data.warmup_boost <= 1.0  # ramp, not the demand target


async def test_no_uplift_while_hot_water_charges(hass, controlled, sources, freezer):  # noqa: F811
    r, c, _ = await rig(hass, controlled, freezer, "active")
    r.states()
    hass.states.async_set("sensor.hw", 100)
    data = await r.tick()
    hass.states.async_set("sensor.hw", 100)
    assert c.boiler.data.dhw_active
    assert data.warmup_boost == 0.0 and data.preheat_rooms == 0
