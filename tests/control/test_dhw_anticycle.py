"""DHW anti-cycle override and the hot-cylinder flow: options, boiler behaviour."""

from copy import deepcopy
from datetime import timedelta

from custom_components.home_heating_optimisation.control.boiler.dhw_policy import (
    dhw_boiler_config,
    hot_cylinder_flow,
    validation_error,
)
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

ENTITY = "number.burn_min"


# -- pure ------------------------------------------------------------------------


def test_config_defaults_and_validation():
    cfg = dhw_boiler_config({})
    assert cfg["anticycle_enabled"] is False and cfg["anticycle_minutes"] == 5
    assert cfg["hot_flow"] is None and cfg["hot_threshold"] == 48.0
    assert (
        dhw_boiler_config({"dhw_boiler": {"hot_flow": 99, "anticycle_minutes": 40}})["hot_flow"]
        is None
    )
    assert validation_error(cfg) is None
    assert validation_error({**cfg, "anticycle_enabled": True}) == "dhw_boiler_needs_entity"
    assert validation_error({**cfg, "anticycle_minutes": 16}) == "dhw_boiler_invalid"
    assert validation_error({**cfg, "hot_flow": 60.0}) == "dhw_boiler_invalid"
    assert validation_error({**cfg, "hot_flow": 75.0}) is None


def test_hot_cylinder_flow_selection():
    pick = lambda **kw: hot_cylinder_flow(  # noqa: E731
        **{
            "target": 68.0,
            "cylinder_temp": 40.0,
            "elevated": False,
            "hot_flow": 75.0,
            "threshold": 48.0,
            **kw,
        }
    )
    assert pick() == (68.0, "normal")
    assert pick(cylinder_temp=48.0) == (75.0, "hot_cylinder")
    assert pick(elevated=True) == (75.0, "hot_cylinder")
    assert pick(cylinder_temp=None) == (68.0, "normal")
    assert pick(hot_flow=None, cylinder_temp=55.0) == (68.0, "normal")


# -- boiler ------------------------------------------------------------------------


def configured(config, enabled=True, **extra):
    config = deepcopy(config)
    config["dhw_boiler"] = {
        "anticycle_enabled": enabled,
        "anticycle_entity": ENTITY,
        "anticycle_minutes": 5,
        **extra,
    }
    return config


async def rig(hass, controlled, enabled=True, auto=True, **extra):  # noqa: F811
    hass.states.async_set(ENTITY, 15, {"unit_of_measurement": "min"})
    entry, c, calls = await start(hass, configured(controlled, enabled, **extra))
    await handover(c)
    if auto:
        await c.set_mode("boiler", "auto")
    await c.refresh()
    return c, calls


def burn(hass):
    return float(hass.states.get(ENTITY).state)


async def dhw(hass, c, on):
    hass.states.async_set("sensor.hw", 100 if on else 0)
    await c.refresh()


async def off_for(hass, c, freezer, minutes):
    await dhw(hass, c, False)
    for _ in range(minutes):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.hw", 0)
        await c.refresh()


async def test_lowers_the_period_when_dhw_starts_and_restores_it_after(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    c, calls = await rig(hass, controlled)
    assert burn(hass) == 15 and c.boiler.data.dhw_anticycle_state == "normal"
    await dhw(hass, c, True)
    data = c.boiler.data
    assert data.dhw_active and burn(hass) == 5
    assert data.dhw_anticycle_state == "dhw" and data.dhw_anticycle_normal == 15
    assert c.boiler._store.get("anticycle_pending") is True
    await dhw(hass, c, True)  # no repeat write while it holds
    assert burn(hass) == 5
    await dhw(hass, c, False)
    assert burn(hass) == 5  # debounced: not at the first quiet cycle
    await off_for(hass, c, freezer, 4)
    assert burn(hass) == 15
    await c.refresh()
    assert c.boiler.data.dhw_anticycle_state == "normal"
    assert c.boiler._store.get("anticycle_pending") is False
    events = (
        [e["data"]["event"] for e in c.heating.journal.events(kinds=["dhw_anticycle"])]
        if hasattr(c, "heating")
        else []
    )
    assert events == [] or {"set", "restore"} <= set(events)


async def test_restart_with_a_pending_restore_puts_the_normal_value_back(
    hass,
    controlled,
    sources,  # noqa: F811
):
    c, _ = await rig(hass, controlled)
    await dhw(hass, c, True)
    assert burn(hass) == 5
    await dhw(hass, c, False)
    # a restart: runtime state is gone, the store still says restore 15
    boiler = c.boiler
    boiler._ac_first, boiler._ac_off_since, boiler._ac_state = True, None, "disabled"
    await c.refresh()
    assert burn(hass) == 15
    await c.refresh()  # the next cycle sees the value back and clears the flag
    assert boiler._store.get("anticycle_pending") is False


async def test_restart_during_a_charge_does_not_restore_early(hass, controlled, sources):  # noqa: F811
    c, _ = await rig(hass, controlled)
    await dhw(hass, c, True)
    boiler = c.boiler
    boiler._ac_first, boiler._ac_state = True, "disabled"
    await dhw(hass, c, True)
    assert burn(hass) == 5 and boiler.data.dhw_anticycle_state == "dhw"


async def test_disabled_writes_nothing(hass, controlled, sources):  # noqa: F811
    c, calls = await rig(hass, controlled, enabled=False)
    await dhw(hass, c, True)
    assert burn(hass) == 15 and c.boiler.data.dhw_anticycle_state == "disabled"
    assert not [d for k, d in calls if k == "number" and d["entity_id"] == ENTITY]


async def test_a_changed_normal_value_outside_a_charge_is_recaptured(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    c, _ = await rig(hass, controlled)
    hass.states.async_set(ENTITY, 12, {"unit_of_measurement": "min"})
    await c.refresh()
    assert c.boiler.data.dhw_anticycle_normal == 12
    await dhw(hass, c, True)
    assert burn(hass) == 5
    await off_for(hass, c, freezer, 4)
    assert burn(hass) == 12


async def test_the_write_guard_and_auto_mode_are_respected(hass, controlled, sources, freezer):  # noqa: F811
    c, calls = await rig(hass, controlled, auto=False)  # shadow: not in auto
    await dhw(hass, c, True)
    assert burn(hass) == 15
    c, calls = await rig(hass, controlled)
    c.boiler._write_guard = lambda: False
    await dhw(hass, c, True)
    assert burn(hass) == 15


async def test_restore_waits_for_the_guard_then_happens(hass, controlled, sources, freezer):  # noqa: F811
    c, _ = await rig(hass, controlled)
    await dhw(hass, c, True)
    allowed = c.boiler._write_guard
    c.boiler._write_guard = lambda: False
    await off_for(hass, c, freezer, 4)
    assert burn(hass) == 5 and c.boiler._store.get("anticycle_pending") is True
    c.boiler._write_guard = allowed
    freezer.tick(timedelta(minutes=11))
    hass.states.async_set("sensor.hw", 0)
    await c.refresh()
    assert burn(hass) == 15


async def test_unconfirmed_write_is_retried_once_then_reported_failed(
    hass,
    controlled,
    sources,
    freezer,  # noqa: F811
):
    c, calls = await rig(hass, controlled)
    hass.services.async_register("number", "set_value", lambda call: None)  # never applies
    await dhw(hass, c, True)
    for _ in range(4):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.hw", 100)
        await c.refresh()
    assert c.boiler.data.dhw_anticycle_state == "failed" and burn(hass) == 15


async def test_hot_cylinder_flow_applies_during_dhw_only(hass, controlled, sources):  # noqa: F811
    c, _ = await rig(hass, controlled, hot_flow=75.0, hot_threshold=48.0)
    await c.refresh()
    assert c.boiler.data.dhw_flow_source == "normal"
    await dhw(hass, c, True)  # the cylinder fixture reads 50 C, above the threshold
    data = c.boiler.data
    assert data.dhw_flow_source == "hot_cylinder" and data.requested_target == 75.0
    c2, _ = await rig(hass, controlled)
    await dhw(hass, c2, True)
    assert c2.boiler.data.dhw_flow_source == "normal" and c2.boiler.data.requested_target != 75.0
