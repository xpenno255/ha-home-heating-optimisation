"""Warm-up flow boost: pure rules and the boiler coordinator in off / shadow / active."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.control.boiler.core.warmup import (
    WarmupBoostParams,
    WarmupBoostState,
    boost_step,
    house_demand,
    return_scale,
    uplift_target,
)
from custom_components.home_heating_optimisation.control.boiler.warmup_policy import (
    validation_error,
    warmup_config,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

T0 = datetime(2026, 10, 14, 6, 40, tzinfo=timezone.utc)
PARAMS = WarmupBoostParams(start=0.15, full=0.40, boost_max=65.0, decay_k_per_min=1.0)


# -- pure rules -------------------------------------------------------------


def test_house_demand_weights_by_radiator_size():
    house = house_demand([(1.0, 6000.0), (0.0, 2000.0), (0.5, 2000.0)])
    assert house.fraction == pytest.approx((6000 + 1000) / 10000)
    assert house.room_equivalents == pytest.approx(1.5)
    assert (house.valid, house.total) == (3, 3)


def test_house_demand_invalid_rooms_and_missing_output():
    # Fewer than half valid: no answer.
    assert house_demand([(0.5, 1000.0), (None, 1000.0), (None, 1000.0)]) is None
    assert house_demand([]) is None
    # A room without a known output counts at the mean known output.
    house = house_demand([(1.0, 2000.0), (0.0, None), (0.0, 4000.0)])
    assert house.fraction == pytest.approx(2000 / (2000 + 3000 + 4000))
    # No outputs at all: rooms count equally; demand is clamped to 0-1.
    assert house_demand([(2.0, None), (0.0, None)]).fraction == pytest.approx(0.5)


def test_uplift_is_zero_below_start_full_at_full_and_linear_between():
    assert uplift_target(0.10, 50.0, PARAMS) == 0.0
    assert uplift_target(0.40, 50.0, PARAMS) == 15.0
    assert uplift_target(0.90, 50.0, PARAMS) == 15.0
    assert uplift_target(0.275, 50.0, PARAMS) == pytest.approx(7.5)
    # Already at the maximum: nothing to add.
    assert uplift_target(0.9, 65.0, PARAMS) == 0.0


def test_uplift_rises_at_once_and_falls_slowly():
    up = boost_step(WarmupBoostState(), 0.40, 50.0, T0, PARAMS)
    assert up.uplift == 15.0
    later = T0 + timedelta(minutes=5)
    down = boost_step(up, 0.0, 50.0, later, PARAMS)
    assert down.uplift == 10.0  # 1 K per minute
    assert boost_step(down, 0.0, 50.0, later + timedelta(minutes=30), PARAMS).uplift == 0.0
    # A stale or missing reading drops it straight to zero.
    assert boost_step(up, None, 50.0, later, PARAMS).uplift == 0.0


def test_return_guard_fades_the_uplift():
    assert return_scale(None, 50.0, PARAMS) == 1.0
    assert return_scale(35.0, 50.0, PARAMS) == 1.0
    assert return_scale(42.5, 50.0, PARAMS) == pytest.approx(0.5)
    assert return_scale(46.0, 50.0, PARAMS) == 0.0
    assert boost_step(WarmupBoostState(), 0.4, 50.0, T0, PARAMS, scale=0.5).uplift == 7.5


def test_config_defaults_and_validation():
    cfg = warmup_config({})
    assert cfg == {"mode": "off", "start": 0.15, "full": 0.40, "decay_k_per_min": 1.0}
    assert warmup_config({"warmup_boost": {"mode": "bogus", "start": 9}})["mode"] == "off"
    assert validation_error(cfg) is None
    assert validation_error({**cfg, "mode": "x"}) == "warmup_boost_invalid"
    assert validation_error({**cfg, "full": 0.1}) == "warmup_boost_invalid"
    assert validation_error({**cfg, "decay_k_per_min": 10}) == "warmup_boost_invalid"


# -- coordinator ------------------------------------------------------------


def with_mode(config, mode):
    config = deepcopy(config)
    config["warmup_boost"] = {"mode": mode}
    return config


async def run(hass, controlled, mode, demand=0.9):  # noqa: F811
    entry, c, calls = await start(hass, with_mode(controlled, mode))
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 18, "temperature": 20, "heat_demand": demand},
    )
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.refresh()
    writes = [data["value"] for kind, data in calls if kind == "number"]
    return entry, c, calls, writes


async def test_off_changes_nothing(hass, controlled, sources):  # noqa: F811
    _, c, calls, writes = await run(hass, controlled, "off")
    data = c.boiler.data
    assert (data.warmup_mode, data.house_demand, data.warmup_boost, data.boost_flow) == (
        "off",
        None,
        0.0,
        None,
    )
    assert writes and c.report()["warmup_boost"]["mode"] == "off"


async def test_shadow_publishes_the_boost_but_writes_the_base_target(
    hass,
    controlled,
    sources,  # noqa: F811
):
    _, c, calls, writes = await run(hass, controlled, "shadow")
    data = c.boiler.data
    assert data.warmup_mode == "shadow" and data.house_demand == pytest.approx(0.9)
    assert data.warmup_boost > 0 and data.boost_flow == 65.0
    base = data.requested_target
    assert base < data.boost_flow
    # Every number.set_value carries the base target (to the 1 K grid), never the boosted one.
    assert writes and all(abs(value - base) <= 1 for value in writes)
    assert max(writes) < data.boost_flow - 10
    summary = c.report()["warmup_boost"]
    assert summary["mode"] == "shadow" and summary["peak_uplift"] > 0
    assert calls and not any(kind == "ramses" for kind, _ in calls)


async def test_active_adds_the_uplift_capped_at_flow_max(hass, controlled, sources):  # noqa: F811
    _, c, _, writes = await run(hass, controlled, "active")
    data = c.boiler.data
    assert data.warmup_boost > 0
    assert writes[-1] == 65.0 and max(writes) <= 65.0
    assert data.requested_target == 65.0


async def test_active_is_idle_below_the_start_share(hass, controlled, sources):  # noqa: F811
    _, c, _, writes = await run(hass, controlled, "active", demand=0.05)
    assert c.boiler.data.warmup_boost == 0.0
    assert writes[-1] < 65.0


async def test_boost_is_not_applied_while_dhw_charges(hass, controlled, sources):  # noqa: F811
    entry, c, calls = await start(hass, with_mode(controlled, "active"))
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": 20, "heat_demand": 0.9}
    )
    hass.states.async_set("sensor.hw", 100)
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.refresh()
    assert c.boiler.data.dhw_active and c.boiler.data.warmup_boost == 0.0
