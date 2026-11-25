"""Planned pre-heat in the runtime: shadow records, active writes, hot water holds it back."""

import json
from dataclasses import asdict, replace
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.const import EntityCategory
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.optimum.preheat import (
    lead_error_minutes,
    learn_lead,
)
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


async def test_the_start_moves_with_what_earlier_arrivals_taught(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    assert plan.lead_bias_minutes == 0.0
    # The room was 0.6 short at the switchpoint: half of that, in minutes, is learnt.
    short = plan.goal - 0.6
    finished = replace(
        plan, started=True, switch_temp=short, capture={"method": "nearest", "value": short}
    )
    learnt = optimum._learn_lead("study", finished)
    error, bias = learnt["lead_error_minutes"], learnt["lead_bias_minutes"]
    assert error > 0 and bias == round(min(20.0, 0.5 * error), 1)
    assert optimum.lead_bias[optimum._lead_key("study", plan.switch_at)] == bias
    # The next plan for that time of day starts that much earlier, on a longer line.
    del optimum.plans["study"]
    await optimum.step()
    again = optimum.plans["study"]
    assert again.lead_bias_minutes == bias
    assert abs((plan.start_at - again.start_at).total_seconds() - bias * 60) < 1
    assert again.climb_minutes == plan.climb_minutes + bias
    # Switched off, the start is where it was.
    optimum.pcfg = {**optimum.pcfg, "arrival_gain": 0.0}
    del optimum.plans["study"]
    await optimum.step()
    assert optimum.plans["study"].start_at == plan.start_at
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
    await refresh(hass, controls)
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
    await refresh(hass, controls)
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


async def test_a_room_behind_its_plan_gets_the_preheat_push(hass, controlled, freezer):  # noqa: F811
    heating, controls, calls = await ready(
        hass, controlled, freezer, "active", warmup_boost={"catchup_overdrive_k": 0.5}
    )
    optimum, room = heating.optimum, controls.rooms["study"]
    plan = optimum.plans["study"]
    freezer.move_to(plan.start_at + timedelta(minutes=1))
    await refresh(hass, controls)
    goal = room.data.preheat_setpoint
    written = writes(calls)[-1]
    # Halfway through the plan the room has not moved: behind, so its zone target is pushed.
    freezer.move_to(plan.start_at + (SWITCH - plan.start_at) / 2)
    climate(hass, 17.0, written)
    cloud(hass, 16.0, 21.0, SWITCH)
    room._store.set("last_written_at", datetime.now(timezone.utc).isoformat())
    await refresh(hass, controls)
    assert room.data.preheating and room.data.preheat_push_k == 0.5
    assert writes(calls)[-1] == round(goal + 0.5, 1)
    assert "pre-heat push +0.5" in room.data.reason
    heating.analytics = None


# -- learning from arrivals: real reports only, once, and not across a restart -------------


async def at(hass, controls, optimum, freezer, when, air=None, zone=None, switch=SWITCH):
    """Move to ``when`` and run one cycle. Only a value given here is a new reading from the
    room's sensor: everything else reports again as it stood, and the sensor says nothing.
    """
    freezer.move_to(when)
    for state in hass.states.async_all():
        if state.domain in ("sensor", "climate", "number", "binary_sensor"):
            own = state.entity_id.startswith(("sensor.air", "binary_sensor.air"))
            if not own and state.entity_id not in ("climate.study", "climate.cloud"):
                hass.states.async_set(
                    state.entity_id, state.state, dict(state.attributes), force_update=True
                )
    past = when >= switch
    cloud(hass, 21.0 if past else 16.0, 16.0 if past else 21.0, switch + timedelta(hours=15 * past))
    study = hass.states.get("climate.study").attributes
    zone = study["current_temperature"] if zone is None else zone
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": zone, "temperature": study["temperature"]},
        force_update=True,
    )
    if air is not None:
        hass.states.async_set("sensor.air", air, {"unit_of_measurement": "°C"})
    await controls.refresh()
    await optimum.step()


async def warming(hass, controlled, freezer):  # noqa: F811
    """An active pre-heat under way: (heating, controls, optimum, the started plan)."""
    heating, controls, _ = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    # The sensor reports as the pre-heat falls due: a plan only begins on a current reading.
    await at(hass, controls, optimum, freezer, optimum.plans["study"].start_at + MIN, air=17.1)
    plan = optimum.plans["study"]
    assert plan.started and not plan.interrupted and plan.capture is None
    assert not plan.dhw_held and plan.dhw_wait_minutes <= 1.0
    assert controls.rooms["study"].data.air_quality == "ok"
    return heating, controls, optimum, plan


MIN = timedelta(minutes=1)


def bias(optimum):
    return optimum.lead_bias.get(optimum._lead_key("study", SWITCH), 0.0)


def scored(heating):
    events = heating.journal.events(kinds=["preheat"])
    return [e["data"] for e in events if e["data"]["event"] == "scored"]


async def test_the_switchpoint_reading_comes_from_the_reports_around_it_and_is_learnt_once(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    assert optimum.plans["study"].obs_before == (SWITCH - 8 * MIN, 20.0)
    # Cycles in between see the same report: it is not filed again under a later time.
    await at(hass, controls, optimum, freezer, SWITCH - 2 * MIN)
    await at(hass, controls, optimum, freezer, SWITCH + 3 * MIN)
    waiting = optimum.finishing["study"]
    assert waiting.obs_before == (SWITCH - 8 * MIN, 20.0) and waiting.obs_after is None
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    assert optimum.finishing["study"].capture is None  # the window is still open
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    done = optimum.finishing["study"]
    assert done.capture["method"] == "interpolated" and done.switch_temp == 20.4
    assert done.capture["gap_minutes"] == 19.0
    assert bias(optimum) == 0.0  # nothing is learnt before the pre-heat is scored
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    error = lead_error_minutes(done)
    learnt = learn_lead(0.0, error, 0.5)
    assert error > 0 and learnt > 0 and bias(optimum) == learnt
    last = optimum.preheat_last["study"]
    assert last["capture"]["method"] == "interpolated" and last["lead_error_minutes"] == error
    assert last["outcome"] != "interrupted" and not last["interrupted"]
    assert f"study|{plan.key}" in optimum.scored_keys and "study" not in optimum.finishing
    room = hass.states.get(SENSOR).attributes["rooms"]["study"]
    assert room["last_capture"]["method"] == "interpolated"
    assert room["last_lead_bias_minutes"] == learnt and room["reading_quality"] == "ok"
    # The same pre-heat turning up again (an older copy of the store, say) adds nothing.
    optimum.finishing["study"] = done
    await at(hass, controls, optimum, freezer, SWITCH + 66 * MIN)
    assert bias(optimum) == learnt and "study" not in optimum.finishing
    assert optimum.scored_keys.count(f"study|{plan.key}") == 1 and len(scored(heating)) == 1
    heating.analytics = None


async def test_a_value_written_again_is_not_a_new_report(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 12 * MIN, air=20.3)
    # The integration writes the same value again two minutes before the switchpoint:
    # the report time moves, the evidence does not.
    old = hass.states.get("sensor.air")
    freezer.move_to(SWITCH - 2 * MIN)
    hass.states.async_set("sensor.air", old.state, dict(old.attributes))
    assert hass.states.get("sensor.air").last_reported == SWITCH - 2 * MIN
    await at(hass, controls, optimum, freezer, SWITCH - 2 * MIN)
    assert optimum.plans["study"].obs_before == (SWITCH - 12 * MIN, 20.3)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    done = optimum.finishing["study"]
    # Twelve minutes before, alone, is too far from the switchpoint to stand for it.
    assert done.capture["method"] is None and done.switch_temp is None
    assert "no qualified report" in done.capture["reason"]
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN)
    assert bias(optimum) == 0.0 and optimum.lead_bias == {}
    last = scored(heating)[-1]
    assert last["lead_error_minutes"] is None and last["capture"]["method"] is None
    heating.analytics = None


async def test_one_report_nine_minutes_before_the_switchpoint_is_enough(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 9 * MIN, air=20.3)
    # The next report is three quarters of an hour after: far too late to count.
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    await at(hass, controls, optimum, freezer, SWITCH + 45 * MIN, air=21.4)
    done = optimum.finishing["study"]
    assert done.capture["method"] == "nearest" and done.switch_temp == 20.3
    assert done.obs_after is None
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN)
    assert bias(optimum) == learn_lead(0.0, lead_error_minutes(done), 0.5) > 0
    heating.analytics = None


async def test_a_late_report_alone_teaches_nothing(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    optimum.lead_bias[optimum._lead_key("study", SWITCH)] = 6.0
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    await at(hass, controls, optimum, freezer, SWITCH + 45 * MIN, air=21.4)
    assert optimum.finishing["study"].capture["method"] is None
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN)
    assert bias(optimum) == 6.0
    heating.analytics = None


async def test_a_stand_in_reading_teaches_nothing(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    room = controls.rooms["study"]
    # The room's sensor drops out; the thermostat's reading stands in for it.
    hass.states.async_set("sensor.air", "unavailable")
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, zone=20.0)
    assert room.data.air_temp == 20.0 and room.data.air_temp_source != "sensor.air"
    assert room.data.air_quality == "fallback" and not room.data.air_qualified
    assert optimum._observation("study")["qualified"] is False
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, zone=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    done = optimum.finishing["study"]
    assert done.obs_before is None and done.obs_after is None
    assert done.capture["method"] is None
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN)
    assert bias(optimum) == 0.0 and scored(heating)[-1]["lead_error_minutes"] is None
    heating.analytics = None


async def test_a_reading_from_before_the_start_teaches_nothing_until_a_new_one_arrives(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    room = controls.rooms["study"]
    # As after a restart: the sensor is back with whatever value was cached for it.
    room._obs_first, room._obs_new = None, False
    await at(hass, controls, optimum, freezer, SWITCH - 9 * MIN)
    assert room.data.air_quality == "not heard since the start" and not room.data.air_qualified
    # The cached value written again is still not a reading.
    old = hass.states.get("sensor.air")
    hass.states.async_set("sensor.air", old.state, dict(old.attributes), force_update=True)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN)
    assert room.data.air_quality == "not heard since the start"
    assert optimum.plans["study"].obs_before is None
    # The planner still plans from it, as the room's control does; it only will not learn.
    assert optimum._read("study")[0] == room.data.air_temp
    # A changed value is a new reading, and counts from then on.
    await at(hass, controls, optimum, freezer, SWITCH - 5 * MIN, air=20.3)
    assert room.data.air_quality == "ok" and room.data.air_qualified
    assert optimum.plans["study"].obs_before == (SWITCH - 5 * MIN, 20.3)
    # A thermostat's own sensor that came back wrong is replaced by the zone's reading
    # (see the overdrive tests); that stand-in teaches nothing either.
    room._obs_first, room._obs_new = None, False
    room._air_first, room._air_heard = None, False
    room._store.set("sensor_gap", 0.0)
    await at(hass, controls, optimum, freezer, SWITCH + 4 * MIN, zone=21.2)
    assert room.data.air_temp == 21.2 and "unchanged since" in room.data.air_temp_source
    assert room.data.air_quality == "unchanged since the start"
    assert optimum._observation("study")["qualified"] is False
    assert optimum.finishing["study"].obs_after is None
    # However long the cached value sits there and is written again, it is not a reading.
    room._obs_first, room._obs_new = None, False
    room._air_heard = True
    await at(hass, controls, optimum, freezer, SWITCH + 70 * MIN)
    assert room.data.air_quality == "not heard since the start"
    old = hass.states.get("sensor.air")
    freezer.move_to(SWITCH + 96 * MIN)
    hass.states.async_set("sensor.air", old.state, dict(old.attributes))
    await at(hass, controls, optimum, freezer, SWITCH + 96 * MIN)
    assert room.data.air_quality == "not heard since the start"
    await at(hass, controls, optimum, freezer, SWITCH + 97 * MIN, air=21.4)
    assert room.data.air_quality == "ok"
    assert optimum._observation("study")["changed_at"] == SWITCH + 97 * MIN
    # A sensor that drops out altogether has to show it is new again when it returns.
    hass.states.async_set("sensor.air", "unavailable")
    hass.states.async_set("climate.study", "unavailable")
    hass.states.async_set("climate.cloud", "unavailable")
    await controls.refresh()
    assert room.data.air_temp is None and room.data.air_quality == "no reading"
    assert optimum._observation("study") is None and optimum._read("study")[0] is None
    hass.states.async_set("sensor.air", 21.4, {"unit_of_measurement": "°C"})
    await controls.refresh()
    assert room.data.air_temp == 21.4 and room.data.air_quality == "not heard since the start"
    heating.analytics = None


async def test_a_sensor_gone_quiet_is_not_planned_from(hass, controlled, freezer):  # noqa: F811
    # The room's sensor and its humidity are one device, as a battery sensor is.
    registry, devices = er.async_get(hass), dr.async_get(hass)
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", "air")}
    )
    for unique, name in (("air_t", "air"), ("air_h", "air_humidity"), ("air_x", "air_count")):
        registry.async_get_or_create(
            "sensor", "test", unique, suggested_object_id=name, device_id=device.id
        )
    registry.async_get_or_create(
        "sensor",
        "test",
        "air_b",
        suggested_object_id="air_battery",
        device_id=device.id,
        entity_category=EntityCategory.DIAGNOSTIC,
    )
    heating, controls, _ = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    assert "study" in optimum.plans

    def humidity(value):
        hass.states.async_set("sensor.air_humidity", value, {"device_class": "humidity"})

    # Three hours on, with a warm-up still ahead, the sensor has said nothing more.
    later = SWITCH + timedelta(hours=3)

    async def then(minutes, air=None):
        when = SWITCH + timedelta(minutes=60 + minutes)
        await at(hass, controls, optimum, freezer, when, air, switch=later)

    await then(0)
    assert optimum._read("study")[0] is None and "study" not in optimum.plans
    assert optimum.preheat("study") is None
    room = hass.states.get(SENSOR).attributes["rooms"]["study"]
    assert room["temperature"] is None and room["planned_start"] is None
    assert room["reading"] == 17.0 and room["reading_evidence_minutes"] == 180.0
    assert room["reading_source"] == "sensor.air" and room["reading_evidence"] == "value change"
    # Its one report is still dated when it was made, three hours ago.
    assert optimum._observation("study")["changed_at"] == SWITCH - timedelta(hours=2)
    # The same value written again says nothing new: an integration can do that from a
    # cache at any time. The write is seen, and it changes nothing.
    old = hass.states.get("sensor.air")
    freezer.move_to(SWITCH + timedelta(minutes=62))
    hass.states.async_set("sensor.air", old.state, dict(old.attributes))
    await then(2)
    obs = optimum._observation("study")
    assert obs["reported_at"] == SWITCH + timedelta(minutes=62) and obs["evidence_minutes"] == 182.0
    assert optimum._read("study")[0] is None and "study" not in optimum.plans
    # Nor does anything else on the device that is not a measurement it makes: a battery
    # level (a diagnostic) or a counter moving is not the room being measured.
    hass.states.async_set("sensor.air_battery", 80, {"device_class": "humidity"})
    hass.states.async_set("sensor.air_count", 4)
    hass.states.async_set("sensor.air_count", 5, {"device_class": "enum"})
    await then(3)
    assert optimum._observation("study")["evidence"] == "value change"
    assert optimum._read("study")[0] is None
    # Nor does its humidity being written again unchanged.
    humidity(55)
    await then(5)
    assert optimum._read("study")[0] == 17.0  # a first humidity reading is a change
    freezer.move_to(SWITCH + timedelta(minutes=115))
    humidity(55)
    await then(55)
    assert optimum._observation("study")["evidence_minutes"] == 52.0  # first seen at +63
    assert optimum._read("study")[0] is None
    # A changed humidity is the device itself reporting: the temperature it last gave
    # still stands, as it does on a steady night when only the humidity moves.
    humidity(56)
    await then(56)
    obs = optimum._observation("study")
    assert obs["evidence"] == "device measurement change" and obs["evidence_minutes"] == 1.0
    assert optimum._read("study")[0] == 17.0 and "study" in optimum.plans
    # It stands behind the temperature; it is not a temperature. The reading is still
    # dated three hours ago, and nothing is filed as a new report of it.
    assert obs["changed_at"] == SWITCH - timedelta(hours=2) and obs["value"] == 17.0
    # A humidity that stops being a number says nothing, and no longer stands behind it.
    humidity("fault")
    await then(58)
    obs = optimum._observation("study")
    assert obs["evidence"] == "value change" and obs["evidence_minutes"] == 238.0
    assert optimum._read("study")[0] is None
    humidity(56)
    await then(59)
    assert optimum._read("study")[0] == 17.0
    # Quiet past the limit again and it is set aside again; a new reading brings it back.
    await then(105)
    assert optimum._read("study")[0] is None
    await then(108, air=17.2)
    assert optimum._observation("study")["evidence"] == "value change"
    assert optimum._read("study")[0] == 17.2 and "study" in optimum.plans
    heating.analytics = None


async def test_a_radio_that_says_when_it_last_heard_the_sensor_is_believed(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    """A wall thermostat reads the same for hours on a quiet night but still transmits."""
    registry, devices = er.async_get(hass), dr.async_get(hass)
    owner = MockConfigEntry(domain="test")
    owner.add_to_hass(hass)
    device = devices.async_get_or_create(
        config_entry_id=owner.entry_id, identifiers={("test", "air")}
    )
    registry.async_get_or_create(
        "sensor", "test", "air_t", suggested_object_id="air", device_id=device.id
    )
    registry.async_get_or_create(
        "binary_sensor", "test", "air_s", suggested_object_id="air_status", device_id=device.id
    )
    heating, controls, _ = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    later = SWITCH + timedelta(hours=3)

    def status(seen, state="on"):
        # Written afresh every minute, as that integration does; only ``last_seen`` matters.
        hass.states.async_set(
            "binary_sensor.air_status",
            state,
            {"last_seen": seen.isoformat(), "device_class": "connectivity", "poll": seen.second},
        )

    status(datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc))
    # Two hours with the reading unchanged, heard from eighteen minutes ago.
    when = SWITCH
    freezer.move_to(when)
    status(when - timedelta(minutes=18))
    await at(hass, controls, optimum, freezer, when, switch=later)
    obs = optimum._observation("study")
    assert obs["evidence"] == "radio last seen" and obs["evidence_minutes"] == 18.0
    assert optimum._read("study")[0] == 17.0 and "study" in optimum.plans
    # The status is written again and again; the thermostat itself has gone silent.
    for minutes in (20, 40):
        freezer.move_to(when + timedelta(minutes=minutes))
        status(when - timedelta(minutes=18))
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=40), switch=later)
    assert optimum._observation("study")["evidence_minutes"] == 58.0
    assert optimum._read("study")[0] is None
    # Then it is declared lost. That change of state is the opposite of hearing from it.
    status(when - timedelta(minutes=18), "off")
    assert hass.states.get("binary_sensor.air_status").last_changed == when + timedelta(minutes=40)
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=40), switch=later)
    obs = optimum._observation("study")
    assert obs["evidence"] == "radio last seen" and obs["evidence_minutes"] == 58.0
    assert optimum._read("study")[0] is None
    # A time even half a minute ahead of the clock is not evidence, and no age is negative.
    status(when + timedelta(minutes=40, seconds=30))
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=40), switch=later)
    obs = optimum._observation("study")
    assert obs["evidence"] == "value change" and obs["evidence_minutes"] > 45
    assert optimum._read("study")[0] is None
    status(when + timedelta(minutes=40))  # exactly now is the latest there can be
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=40), switch=later)
    assert optimum._observation("study")["evidence_minutes"] == 0.0
    assert optimum._observation("study", when + timedelta(minutes=39))["evidence_minutes"] == 0.0
    status(when - timedelta(minutes=18))
    # A time from the future, or one that is no time at all, is not evidence.
    hass.states.async_set("binary_sensor.air_status", "on", {"last_seen": "soon"})
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=41), switch=later)
    assert optimum._observation("study")["evidence"] == "value change"
    status(when + timedelta(hours=5))
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=42), switch=later)
    assert optimum._read("study")[0] is None
    # After a restart of Home Assistant everything on the bus was put back from a cache:
    # nothing stamped before it counts, however recent it claims to be.
    room = controls.rooms["study"]
    room._evidence_restart, room._evidence_floor = True, None
    status(when + timedelta(minutes=42))
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=43), switch=later)
    assert room.data.air_evidence == "none" and room.data.air_evidence_at is None
    assert optimum._observation("study")["evidence_minutes"] is None
    assert optimum._read("study")[0] is None
    status(when + timedelta(minutes=44))
    await at(hass, controls, optimum, freezer, when + timedelta(minutes=45), switch=later)
    assert optimum._read("study")[0] == 17.0
    heating.analytics = None


async def test_a_pre_heat_does_not_begin_on_a_reading_that_has_gone_quiet(
    hass, controlled, freezer
):  # noqa: F811
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    assert plan.start_at - datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc) > timedelta(minutes=45)
    # Planned at 05:00 from a reading the sensor has not added to since.
    await at(hass, controls, optimum, freezer, plan.start_at + MIN)
    assert optimum._read("study")[0] is None
    waiting = optimum.preheat("study")
    assert waiting is not None and not waiting.started
    assert not controls.rooms["study"].data.preheating
    await at(hass, controls, optimum, freezer, plan.start_at + 3 * MIN)
    events = [e["data"] for e in heating.journal.events(kinds=["preheat"])]
    assert [e["event"] for e in events].count("waiting") == 1  # said once, not every cycle
    assert "no current reading" in next(e for e in events if e["event"] == "waiting")["reason"]
    assert "no current reading" in hass.states.get(SENSOR).attributes["rooms"]["study"]["waiting"]
    # The sensor is heard from again before the switchpoint: the pre-heat begins, late,
    # and a late start is no test of the plan.
    await at(hass, controls, optimum, freezer, plan.start_at + 6 * MIN, air=17.1)
    begun = optimum.plans["study"]
    assert begun.started and begun.dhw_wait_minutes > 2 and not begun.dhw_held
    assert hass.states.get(SENSOR).attributes["rooms"]["study"]["waiting"] is None
    short = begun.goal - 0.6
    late = replace(begun, switch_temp=short, capture={"method": "nearest", "value": short})
    assert lead_error_minutes(late) is None
    heating.analytics = None


async def test_a_pre_heat_never_heard_from_in_time_is_reported_as_not_started(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    await at(hass, controls, optimum, freezer, plan.start_at + MIN)
    await at(hass, controls, optimum, freezer, SWITCH - 5 * MIN)
    assert not optimum.plans["study"].started
    assert all(w < 18 for w in writes(calls))  # nothing near a pre-heat target went out
    await at(hass, controls, optimum, freezer, SWITCH + MIN)
    assert "study" not in optimum.plans and "study" not in optimum.finishing
    last = optimum.preheat_last["study"]
    assert last["outcome"] == "not_started" and "no current reading" in last["reason"]
    events = [e["data"] for e in heating.journal.events(kinds=["preheat"])]
    assert events[-1]["event"] == "not_started" and events[-1]["switchpoint"] == SWITCH.isoformat()
    assert optimum.lead_bias == {} and optimum.scored_keys == []
    heating.analytics = None


async def test_a_charge_running_when_the_pre_heat_fell_due_teaches_nothing_however_short(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    hass.states.async_set("sensor.hw", 100)
    await at(hass, controls, optimum, freezer, plan.start_at, air=17.1)
    held = optimum.plans["study"]
    assert controls.boiler.data.dhw_active and not held.started and held.dhw_held
    # It stays marked as the plan is worked out again, and through the store.
    await optimum.step()
    assert optimum.plans["study"].dhw_held
    assert type(held).from_dict(json.loads(json.dumps(held.to_dict()))).dhw_held
    # The charge ends within the minute and the pre-heat begins as planned, a minute late.
    hass.states.async_set("sensor.hw", 0)
    due = max(optimum.plans["study"].start_at, plan.start_at)
    await at(hass, controls, optimum, freezer, due + MIN, air=17.2)
    await at(hass, controls, optimum, freezer, due + MIN + timedelta(seconds=30))
    begun = optimum.plans["study"]
    assert begun.started and begun.start_reason == "planned" and begun.dhw_wait_minutes <= 1.5
    assert begun.dhw_held
    short = begun.goal - 0.6
    done = replace(begun, switch_temp=short, capture={"method": "nearest", "value": short})
    assert lead_error_minutes(done) is None
    assert lead_error_minutes(replace(done, dhw_held=False)) > 0  # the hold is what rules it out
    assert (
        optimum._learn_lead("study", done)["lead_bias_minutes"] == 0.0 and optimum.lead_bias == {}
    )
    events = [e["data"] for e in heating.journal.events(kinds=["preheat"])]
    assert next(e for e in events if e["event"] == "started")["dhw_held"] is True
    heating.analytics = None


async def test_a_restart_just_after_a_start_not_yet_saved_still_costs_the_lesson(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, calls = await ready(hass, controlled, freezer, "active")
    optimum = heating.optimum
    plan = optimum.plans["study"]
    saves = []

    async def save(data):
        saves.append(json.loads(json.dumps(data)))

    optimum._store.async_save = save
    await optimum._save()
    before = saves[-1]
    assert before["plans"]["study"]["started"] is False
    # The pre-heat begins twenty seconds into its minute, and that goes to the store at
    # once, without waiting for the planner's next five-minute step.
    freezer.move_to(plan.start_at + timedelta(seconds=20))
    hass.states.async_set("sensor.air", 17.1, {"unit_of_measurement": "°C"})
    await controls.refresh()
    assert optimum.plans["study"].started
    await hass.async_block_till_done()
    assert saves[-1]["plans"]["study"]["started"] is True and len(saves) >= 2
    # Had Home Assistant gone down before that save landed, the store would still say
    # "not started". Coming back after the planned start, that cannot be trusted: the
    # pre-heat is taken to have been interrupted.
    freezer.move_to(plan.start_at + timedelta(seconds=40))
    again = type(optimum)(hass, optimum.entry, heating)
    again._load_preheat(before)
    resumed = again.plans["study"]
    assert not resumed.started and resumed.interrupted
    began = again.preheat("study")
    assert (
        began.started
        and began.interrupted
        and lead_error_minutes(
            replace(began, switch_temp=20.0, capture={"method": "nearest", "value": 20.0})
        )
        is None
    )
    # The mark survives the plan being worked out again before it begins.
    again2 = type(optimum)(hass, optimum.entry, heating)
    again2._load_preheat(before)
    optimum.plans = again2.plans
    await optimum.step()
    assert optimum.plans["study"].interrupted
    # A plan whose start is still ahead when the store is read is as it was left.
    freezer.move_to(plan.start_at - timedelta(minutes=5))
    early = type(optimum)(hass, optimum.entry, heating)
    early._load_preheat(before)
    assert not early.plans["study"].interrupted
    heating.analytics = None


async def restart(optimum):
    """Save, then load into the same planner as a restart or an options save would."""
    stored = {}

    async def save(data):
        stored.clear()
        stored.update(json.loads(json.dumps(data)))

    optimum._store.async_save = save
    await optimum._save()
    optimum.plans, optimum.finishing, optimum.earlier, optimum.lead_bias = {}, {}, {}, {}
    optimum.scored_keys, optimum.preheat_last = [], {}
    optimum._load_preheat(stored)
    return stored


async def test_a_restart_before_the_capture_costs_that_morning_its_lesson(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    stored = await restart(optimum)
    assert stored["plans"]["study"]["obs_before"] == [(SWITCH - 8 * MIN).timestamp(), 20.0]
    cut = optimum.plans["study"]
    assert cut.started and cut.interrupted and cut.capture is None
    # The pre-heat itself carries on.
    assert optimum.preheat("study") is cut
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    done = optimum.finishing["study"]
    assert done.capture["method"] is None and "restart" in done.capture["reason"]
    assert done.obs_after is None
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    assert bias(optimum) == 0.0 and optimum.lead_bias == {}
    last = scored(heating)[-1]
    assert last["outcome"] == "interrupted" and last["interrupted"]
    assert last["lead_error_minutes"] is None
    # It is still a reported outcome: the arrival and the peak are there.
    assert last["switchpoint"] == SWITCH.isoformat() and "overshoot_k" in last
    assert hass.states.get(SENSOR).attributes["rooms"]["study"]["last_outcome"] == "interrupted"
    heating.analytics = None


async def test_a_capture_made_before_a_restart_is_kept_and_learnt_from(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    stored = await restart(optimum)
    assert stored["finishing"]["study"]["capture"]["method"] == "interpolated"
    kept = optimum.finishing["study"]
    assert not kept.interrupted and kept.switch_temp == 20.4
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    learnt = learn_lead(0.0, lead_error_minutes(kept), 0.5)
    assert learnt > 0 and bias(optimum) == learnt
    assert scored(heating)[-1]["outcome"] != "interrupted"
    heating.analytics = None


async def test_a_restart_after_the_score_does_not_learn_it_again(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    done = optimum.finishing["study"]
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    learnt = bias(optimum)
    assert learnt > 0
    stored = await restart(optimum)
    # What was learnt and which pre-heat it came from are in the one saved document.
    assert stored["lead_bias"] == {optimum._lead_key("study", SWITCH): learnt}
    assert stored["scored_keys"] == [f"study|{plan.key}"] and stored["finishing"] == {}
    assert bias(optimum) == learnt
    # Even handed the finished pre-heat again, it is not applied a second time.
    optimum.finishing["study"] = done
    await at(hass, controls, optimum, freezer, SWITCH + 66 * MIN)
    assert bias(optimum) == learnt and "study" not in optimum.finishing
    # Nor is it scored or journalled a second time: one pre-heat, one score.
    assert len(scored(heating)) == 1 and scored(heating)[-1]["lead_bias_minutes"] == learnt
    assert optimum.preheat_last["study"]["lead_bias_minutes"] == learnt
    heating.analytics = None


def after(plan, minutes, **changes):
    """Another started pre-heat of the room, to a switchpoint ``minutes`` after ``plan``'s."""
    switch = plan.switch_at + timedelta(minutes=minutes)
    begin = switch - timedelta(minutes=20)
    blank = dict(obs_before=None, obs_after=None, capture=None, switch_temp=None, arrived_at=None)
    fields = dict(key=int(switch.timestamp()), switch_at=switch, start_at=begin, started_at=begin)
    return replace(plan, **blank, **fields, **changes)


async def test_warmups_an_hour_apart_are_each_scored_once(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    first = optimum.finishing["study"]
    assert first.capture["method"] == "interpolated"
    # A second warm-up, to 08:00, is under way when the first is still waiting for its score.
    second = after(first, 60)
    optimum.plans["study"] = second
    await at(hass, controls, optimum, freezer, SWITCH + 59 * MIN)
    assert optimum.finishing["study"].key == first.key and scored(heating) == []
    # 08:01: the second passes its switchpoint in the very cycle the first falls due.
    # The first is scored, not pushed out.
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    assert f"study|{first.key}" in optimum.scored_keys and len(scored(heating)) == 1
    assert scored(heating)[0]["switchpoint"] == SWITCH.isoformat()
    learnt = learn_lead(0.0, lead_error_minutes(first), 0.5)
    assert bias(optimum) == learnt > 0
    assert optimum.finishing["study"].key == second.key and "study" not in optimum.earlier
    # The second is finalised a quarter of an hour after its own switchpoint and scored an
    # hour after it, no sooner.
    await at(hass, controls, optimum, freezer, SWITCH + 76 * MIN)
    assert optimum.finishing["study"].capture is not None and len(scored(heating)) == 1
    await at(hass, controls, optimum, freezer, SWITCH + 119 * MIN)
    assert len(scored(heating)) == 1
    await at(hass, controls, optimum, freezer, SWITCH + 121 * MIN)
    assert len(scored(heating)) == 2 and "study" not in optimum.finishing
    assert optimum.scored_keys == [f"study|{first.key}", f"study|{second.key}"]
    assert bias(optimum) == learnt
    heating.analytics = None


async def test_warmups_half_an_hour_apart_both_wait_their_hour_and_survive_a_restart(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    await at(hass, controls, optimum, freezer, SWITCH - 8 * MIN, air=20.0)
    await at(hass, controls, optimum, freezer, SWITCH + 3 * MIN)
    first = optimum.finishing["study"]
    second = after(first, 30)
    optimum.plans["study"] = second
    await at(hass, controls, optimum, freezer, SWITCH + 11 * MIN, air=20.95)
    await at(hass, controls, optimum, freezer, SWITCH + 16 * MIN)
    assert optimum.finishing["study"].capture["method"] == "interpolated"
    assert optimum.plans["study"].obs_before is None  # 07:11 is outside the 07:30 window
    await at(hass, controls, optimum, freezer, SWITCH + 24 * MIN, air=21.0)
    assert optimum.plans["study"].obs_before == (SWITCH + 24 * MIN, 21.0)
    # 07:31: the second joins the first, which still has half an hour to wait.
    await at(hass, controls, optimum, freezer, SWITCH + 31 * MIN)
    assert [p.key for p in optimum.earlier["study"]] == [first.key]
    assert optimum.finishing["study"].key == second.key and scored(heating) == []
    await at(hass, controls, optimum, freezer, SWITCH + 46 * MIN)
    assert optimum.finishing["study"].capture["method"] == "nearest"
    assert optimum.finishing["study"].switch_temp == 21.0
    assert optimum.earlier["study"][0].switch_temp == 20.4  # each keeps its own capture
    # A restart with both waiting: both come back, captured, neither interrupted.
    stored = await restart(optimum)
    assert [p["key"] for p in stored["earlier"]["study"]] == [first.key]
    assert stored["finishing"]["study"]["key"] == second.key
    back = [*optimum.earlier["study"], optimum.finishing["study"]]
    assert [p.key for p in back] == [first.key, second.key]
    assert all(p.capture["method"] and not p.interrupted for p in back)
    await at(hass, controls, optimum, freezer, SWITCH + 59 * MIN)
    assert scored(heating) == []
    await at(hass, controls, optimum, freezer, SWITCH + 61 * MIN, air=21.3)
    assert [e["switchpoint"] for e in scored(heating)] == [SWITCH.isoformat()]
    assert "study" not in optimum.earlier and optimum.finishing["study"].key == second.key
    assert bias(optimum) == learn_lead(0.0, lead_error_minutes(back[0]), 0.5) > 0
    await at(hass, controls, optimum, freezer, SWITCH + 89 * MIN)
    assert len(scored(heating)) == 1
    await at(hass, controls, optimum, freezer, SWITCH + 91 * MIN)
    assert [e["switchpoint"] for e in scored(heating)] == [
        SWITCH.isoformat(),
        (SWITCH + 30 * MIN).isoformat(),
    ]
    assert optimum.scored_keys == [f"study|{first.key}", f"study|{second.key}"]
    assert optimum.finishing == {} and optimum.earlier == {}
    heating.analytics = None


async def test_a_start_held_back_by_hot_water_teaches_nothing(hass, controlled, freezer):  # noqa: F811
    heating, controls, optimum, plan = await warming(hass, controlled, freezer)
    short = plan.goal - 0.6
    held = replace(
        plan,
        dhw_wait_minutes=12.0,
        switch_temp=short,
        capture={"method": "nearest", "value": short},
    )
    assert optimum._learn_lead("study", held)["lead_error_minutes"] is None
    assert optimum.lead_bias == {}
    # The same morning without the wait would have moved the start, within its limits.
    free = replace(held, dhw_wait_minutes=0.0, key=plan.key + 1, switch_temp=plan.goal - 5.0)
    assert optimum._learn_lead("study", free)["lead_bias_minutes"] == 20.0
    heating.analytics = None


async def test_duplicate_warmups_in_the_store_are_removed_and_the_fit_rebuilt(
    hass,
    controlled,
    freezer,  # noqa: F811
):
    heating, controls, _ = await ready(hass, controlled, freezer, "shadow")
    optimum = heating.optimum
    distinct = optimum.events["study"]
    assert len(distinct) == 4 and optimum.fits["study"].events == 4
    doubled = [r for e in distinct for r in (e, {**e, "start": e["start"] + 0.003})]
    stale = replace(optimum.fits["study"], events=8)

    async def load():
        return {
            "events": {"study": doubled},
            "fits": {"study": json.loads(json.dumps(asdict(stale)))},
        }

    optimum._store.async_load = load
    await optimum.initialise()
    assert optimum.events["study"] == distinct
    # The stored fit is kept for now, and says so.
    assert optimum.refit_pending and optimum.fits["study"].events == 8
    assert optimum.report()["refit_pending"] and optimum.room_view("study")[2]["refit_pending"]
    await optimum.step()
    assert not optimum.refit_pending and optimum.fits["study"].events == 4
    report = optimum.report()
    assert report["refit_pending"] is False and report["learned_at"] is not None
    assert report["rooms"]["study"]["events"] == 4
    # A clean store raises no flag.
    doubled[:] = distinct
    await optimum.initialise()
    assert not optimum.refit_pending
    # More warm-ups than are kept: the newest sixty distinct ones, as when merging.
    many = [{**distinct[0], "start": distinct[0]["start"] + day * 86400} for day in range(70)]
    doubled[:] = [r for e in many for r in (e, {**e, "start": e["start"] + 0.003})]
    await optimum.initialise()
    assert optimum.events["study"] == many[-60:] and optimum.refit_pending
    heating.analytics = None
