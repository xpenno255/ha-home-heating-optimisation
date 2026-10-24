"""Planned pre-heat in the runtime: shadow records, active writes, hot water holds it back."""

from dataclasses import replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start
from tests.test_optimum import climate, cloud, history, with_optimum

SWITCH = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
SENSOR = f"sensor.{DOMAIN}_preheat"


def preheat_config(config, mode, **extra):
    config = with_optimum(config, **extra.pop("optimum", {}))
    config["preheat"] = {"mode": mode}
    config.update(extra)
    return config


async def ready(hass, controlled, freezer, mode, room_mode="active", **extra):  # noqa: F811
    freezer.move_to(datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc))
    entry, controls, calls = await start(hass, preheat_config(controlled, mode, **extra))
    hass.states.async_set("sensor.outdoor", 5, {"unit_of_measurement": "°C"})
    climate(hass, 17.0, 16.0)
    cloud(hass, 16.0, 21.0, SWITCH)
    heating = entry.runtime_data
    heating.analytics = SimpleNamespace(store=SimpleNamespace(observations=history()))
    await handover(controls)
    if room_mode == "active":
        await controls.set_mode("study", "active")
    await refresh(hass, controls)
    await heating.optimum.step()
    calls.clear()
    return heating, controls, calls


def fresh(hass):
    """Re-report every source, as a live house does, so a time jump leaves none stale."""
    for state in hass.states.async_all():
        if state.domain in ("sensor", "climate", "number", "binary_sensor"):
            hass.states.async_set(
                state.entity_id, state.state, dict(state.attributes), force_update=True
            )


async def refresh(hass, controls):
    fresh(hass)
    await controls.refresh()


def writes(calls):
    return [d["setpoint"] for kind, d in calls if kind == "ramses"]


async def test_shadow_plans_and_records_but_never_writes(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    assert hass.states.get(SENSOR).state == "shadow"
    plan = optimum.plans["study"]
    assert plan.start_at < SWITCH and not plan.started and plan.goal > 20.5
    freezer.move_to(plan.start_at + timedelta(minutes=2))
    climate(hass, 17.0, 16.0)
    await refresh(hass, controls)
    assert optimum.plans["study"].started
    room = controls.rooms["study"]
    assert room.data.preheat_status == "shadow" and not room.data.preheating
    assert "pre-heat (shadow)" in room.data.reason
    # only the ordinary model write: nothing near the pre-heat target went out
    assert all(w < room.data.preheat_setpoint - 1 for w in writes(calls))
    kinds = {e["data"]["event"] for e in heating.journal.events(kinds=["preheat"])}
    assert {"planned", "started"} <= kinds
    assert controls.preheat_rooms()[0].commanded is None
    assert controls.warmup_rooms()[0][5] == room.data.air_setpoint
    await optimum.step()
    attrs = hass.states.get(SENSOR).attributes["rooms"]["study"]
    assert attrs["planned_start"] and attrs["warm_by"] and attrs["started"]
    heating.analytics = None


async def test_active_writes_at_planned_start_and_hands_over_without_a_second_write(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum, room = heating.optimum, controls.rooms["study"]
    plan = optimum.plans["study"]
    freezer.move_to(plan.start_at - timedelta(minutes=3))
    await refresh(hass, controls)
    assert not optimum.plans["study"].started
    assert all(w < 18 for w in writes(calls))
    calls.clear()

    freezer.move_to(plan.start_at + timedelta(minutes=1))
    await refresh(hass, controls)
    target = room.data.preheat_setpoint
    assert target is not None and target > room.data.air_setpoint
    assert writes(calls) == [round(target, 1)] or writes(calls) == [target]
    assert room.data.preheating and room.data.state == "preheat"
    assert controls.warmup_rooms()[0][5] == target
    [pre] = controls.preheat_rooms()
    assert pre.commanded == target and pre.goal == optimum.plans["study"].goal

    # Zone echoes the override and the room warms; it is not taken for a manual change.
    written = writes(calls)[-1]
    freezer.move_to(SWITCH - timedelta(minutes=1))
    climate(hass, 20.9, written)
    cloud(hass, 16.0, 21.0, SWITCH)
    # keep the override fresh as the cycle would have, so only the hand-over is under test
    room._store.set("last_written_at", datetime.now(timezone.utc).isoformat())
    await refresh(hass, controls)
    assert room.data.state != "manual"
    before = len(writes(calls))
    freezer.move_to(SWITCH + timedelta(seconds=30))
    cloud(hass, 21.0, 16.0, SWITCH + timedelta(hours=15))
    climate(hass, 21.0, written)
    await refresh(hass, controls)
    assert room.data.state != "manual" and not room.data.preheating
    assert len(writes(calls)) == before
    assert hass.states.get(SENSOR).state == "active"
    heating.analytics = None


async def test_hot_water_charge_delays_the_start(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    freezer.move_to(plan.start_at + timedelta(minutes=1))
    hass.states.async_set("sensor.hw", 100)
    await refresh(hass, controls)
    assert controls.boiler.data.dhw_active
    assert not optimum.plans["study"].started
    assert all(w < 18 for w in writes(calls))
    hass.states.async_set("sensor.hw", 0)
    freezer.tick(timedelta(minutes=2))
    await refresh(hass, controls)
    await refresh(hass, controls)
    assert optimum.plans["study"].started
    assert optimum.plans["study"].begin > plan.start_at
    heating.analytics = None


async def test_planned_start_moves_past_a_scheduled_charge(hass, controlled, freezer):  # noqa: F811
    hass.states.async_set(
        "water_heater.cloud",
        "heat",
        {
            "status": {
                "setpoints": {
                    "next_sp_state": "On",
                    "next_sp_from": datetime(2026, 9, 20, 5, 30, tzinfo=timezone.utc).isoformat(),
                }
            }
        },
    )
    heating, controls, _ = await ready(
        hass, controlled, freezer, "shadow", dhw_cutoff={"cloud_entity": "water_heater.cloud"}
    )
    plan = heating.optimum.plans["study"]
    assert plan.start_at >= datetime(2026, 9, 20, 6, 10, tzinfo=timezone.utc)
    heating.analytics = None


async def test_plan_survives_a_restart_in_the_store(hass, controlled, freezer):  # noqa: F811
    heating, controls, _ = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    freezer.move_to(plan.start_at + timedelta(minutes=1))
    assert optimum.preheat("study").started
    await optimum.step()
    stored = {}

    async def save(data):
        stored.update(data)

    optimum._store.async_save = save
    await optimum._save()
    fresh = type(optimum)(hass, optimum.entry, heating)
    fresh._load_preheat(stored)
    assert fresh.plans["study"].started and fresh.plans["study"].start_at == plan.start_at
    heating.analytics = None


async def test_pre_heat_off_changes_nothing(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "off")
    assert heating.optimum.plans == {} and heating.optimum.preheat("study") is None
    assert hass.states.get(SENSOR).state == "off"
    assert controls.preheat_rooms() == [] and writes(calls) == []
    heating.analytics = None


async def hold_for_hot_water(hass, controls, optimum, plan, freezer):
    """A charge running when the planned start arrives; returns the minute it was first seen."""
    freezer.move_to(plan.start_at + timedelta(minutes=1))
    hass.states.async_set("sensor.hw", 100)
    await refresh(hass, controls)
    await optimum.step()
    assert controls.boiler.data.dhw_active and not optimum.preheat("study").started
    return plan.start_at + timedelta(minutes=1)


async def test_charge_running_past_its_expected_end_only_delays_by_the_grace(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, calls = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    seen = await hold_for_hot_water(hass, controls, optimum, optimum.plans["study"], freezer)
    plan = replace(optimum.plans["study"], latest_start=SWITCH)  # plenty of slack
    optimum.plans["study"] = plan
    # default expected charge 40 minutes, plus a 10 minute grace
    freezer.move_to(seen + timedelta(minutes=49))
    assert not optimum.preheat("study").started
    freezer.move_to(seen + timedelta(minutes=51))
    started = optimum.preheat("study")
    assert started.started and controls.boiler.data.dhw_active  # DHW still on
    assert started.start_reason == "dhw_grace"
    assert started.dhw_wait_minutes == 52.0
    assert started.late_risk_minutes >= 0
    events = [
        e for e in heating.journal.events(kinds=["preheat"]) if e["data"]["event"] == "started"
    ]
    assert events[-1]["data"]["start_reason"] == "dhw_grace"
    heating.analytics = None


async def test_a_room_that_would_be_late_starts_at_its_latest_start(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    await hold_for_hot_water(hass, controls, optimum, optimum.plans["study"], freezer)
    plan = replace(
        optimum.plans["study"], latest_start=optimum.plans["study"].start_at + timedelta(minutes=8)
    )
    optimum.plans["study"] = plan
    freezer.move_to(plan.start_at + timedelta(minutes=7))
    assert not optimum.preheat("study").started
    freezer.move_to(plan.start_at + timedelta(minutes=9))
    started = optimum.preheat("study")
    assert started.started and started.start_reason == "latest_start"
    assert started.dhw_wait_minutes == 9.0
    heating.analytics = None


async def test_short_charge_is_waited_out_then_the_pre_heat_starts(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    seen = await hold_for_hot_water(hass, controls, optimum, optimum.plans["study"], freezer)
    plan = replace(optimum.plans["study"], latest_start=SWITCH)
    optimum.plans["study"] = plan
    freezer.move_to(seen + timedelta(minutes=15))
    hass.states.async_set("sensor.hw", 0)
    await refresh(hass, controls)
    assert not controls.boiler.data.dhw_active
    started = optimum.preheat("study")
    assert started.started and started.start_reason == "planned"
    assert started.dhw_wait_minutes == 16.0
    heating.analytics = None
