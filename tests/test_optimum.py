"""Shadow optimum predictor in the runtime: learns, predicts, scores, never acts."""

import math
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from homeassistant.core import SupportsResponse

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.optimum.coordinator import Optimum
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start
from tests.test_optimum_model import warmup

SENSOR = f"sensor.{DOMAIN}_optimum_start"


def history(days=4):
    """Four mornings of a 3 K warm-up in about an hour (about 20 minutes per kelvin)."""
    return [p for d in range(days) for p in warmup(d * 1440 + 360, 18.0, 21.0, 60, zone="study")]


def with_optimum(config, **extra):
    config = deepcopy(config)
    config["analytics_enabled"] = False
    config["optimum"] = {"enabled": True, "forecast_entity": "weather.home", **extra}
    return config


async def test_disabled_by_default(hass, controlled):  # noqa: F811
    entry, _, calls = await start(hass, controlled)
    optimum = entry.runtime_data.optimum
    assert optimum.status == "disabled"
    assert hass.states.get(SENSOR).state == "disabled"
    assert hass.states.get(f"sensor.{DOMAIN}_living_room_optimum_start") is None
    assert entry.runtime_data.controls.report()["optimum"]["status"] == "disabled"
    assert calls == []


def climate(hass, temp, setpoint):
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": temp, "temperature": setpoint}
    )


def cloud(hass, this_sp, next_sp, at):
    hass.states.async_set(
        "climate.cloud",
        "auto",
        {
            "status": {
                "setpoints": {
                    "this_sp_temp": this_sp,
                    "next_sp_temp": next_sp,
                    "next_sp_from": at.isoformat(),
                }
            }
        },
    )


async def test_predicts_scores_and_never_acts(hass, controlled, freezer):  # noqa: F811
    freezer.move_to(datetime(2026, 9, 20, 5, 0, tzinfo=timezone.utc))
    switch = datetime(2026, 9, 20, 7, 0, tzinfo=timezone.utc)
    forecast_calls = []

    async def forecasts(call):
        forecast_calls.append(dict(call.data))
        return {
            "weather.home": {
                "forecast": [
                    {"datetime": (switch + timedelta(hours=h)).isoformat(), "temperature": 5.0}
                    for h in range(-4, 6)
                ]
            }
        }

    hass.services.async_register(
        "weather", "get_forecasts", forecasts, supports_response=SupportsResponse.ONLY
    )
    entry, controls, calls = await start(hass, with_optimum(controlled))
    hass.states.async_set("sensor.outdoor", 5, {"unit_of_measurement": "°C"})
    climate(hass, 17.0, 16.0)
    cloud(hass, 16.0, 21.0, switch)
    # Every other service is recorded by `start`; pre-fill analytics with simulated history.
    heating = entry.runtime_data
    heating.analytics = SimpleNamespace(store=SimpleNamespace(observations=history()))
    optimum = heating.optimum
    optimum._offset = lambda _c: 0.0  # the comfort model's offset is not under test here
    assert hass.states.get(f"sensor.{DOMAIN}_living_room_optimum_start") is not None
    await controls.refresh()
    await optimum.step()
    assert optimum.fits["study"].status == "ready" and optimum.fits["study"].events == 4
    assert optimum.status == "ready"
    assert forecast_calls == [{"entity_id": "weather.home", "type": "hourly"}]
    assert optimum.outdoor_source == "forecast"
    start_at, _, attrs = optimum.room_view("study")
    assert start_at is not None and start_at < switch
    assert attrs["boost_lead_minutes"] <= attrs["lead_minutes"]
    assert hass.states.get(f"sensor.{DOMAIN}_living_room_optimum_start").state != "unknown"

    # Evohome does nothing early: the room only warms from the switchpoint. Run past it.
    temp = 17.0
    while datetime.now(timezone.utc) < switch + timedelta(minutes=125):
        freezer.tick(timedelta(minutes=5))
        now = datetime.now(timezone.utc)
        started = now >= switch
        temp = min(21.0, temp + 0.5) if started else temp - 0.02
        climate(hass, temp, 21.0 if started else 16.0)
        cloud(
            hass,
            21.0 if now >= switch else 16.0,
            16.0 if now >= switch else 21.0,
            switch + timedelta(hours=15) if now >= switch else switch,
        )
        await hass.async_block_till_done()
        await controls.refresh()
        await optimum.step()

    assert optimum.scores, optimum.report()
    score = optimum.scores[-1]
    assert score["room_id"] == "study"
    assert score["evohome_start"] is not None and score["reached"] is not None
    assert score["outcome"] == "scored" and score["evohome_early"] is False
    assert score["evohome_lateness_min"] > 0 and score["schedule_target"] == 21.0
    assert score["outdoor_source"] == "forecast"
    kinds = {e["data"]["event"] for e in heating.journal.events(kinds=["optimum"])}
    assert {"start_predicted", "start_scored"} <= kinds
    assert all(e["origin"] == "controller" for e in heating.journal.events(kinds=["optimum"]))
    # Shadow only: no device service was ever called (the forecast read is the only call).
    assert calls == []
    heating.analytics = None


async def test_needs_analytics_without_history(hass, controlled):  # noqa: F811
    entry, _, calls = await start(hass, with_optimum(controlled))
    optimum = entry.runtime_data.optimum
    await optimum.step()
    assert optimum.status == "needs_analytics"
    assert hass.states.get(SENSOR).state == "needs_analytics"
    assert calls == []


async def test_forecast_failure_falls_back_to_current_outdoor(hass, controlled):  # noqa: F811
    async def broken(call):
        raise RuntimeError("down")

    hass.services.async_register(
        "weather", "get_forecasts", broken, supports_response=SupportsResponse.ONLY
    )
    entry, controls, _ = await start(hass, with_optimum(controlled))
    entry.runtime_data.analytics = SimpleNamespace(store=SimpleNamespace(observations=[]))
    await controls.refresh()
    await entry.runtime_data.optimum.step()
    assert entry.runtime_data.optimum.outdoor_source == "current"
    assert entry.runtime_data.optimum.last_error is None
    entry.runtime_data.analytics = None


async def test_learning_waits_for_history_and_hints_the_next_warmup(
    hass,
    controlled,  # noqa: F811
    freezer,
):
    # Live 12 Oct: the first pass ran 20 s before the history finished loading and every
    # room stayed unfitted for 10 minutes.
    freezer.move_to(datetime(2026, 9, 19, 20, 0, tzinfo=timezone.utc))
    switch = datetime(2026, 9, 20, 6, 0, tzinfo=timezone.utc)
    entry, controls, calls = await start(hass, with_optimum(controlled, forecast_entity=None))
    hass.states.async_set("sensor.outdoor", 5, {"unit_of_measurement": "°C"})
    climate(hass, 19.0, 16.0)
    cloud(hass, 16.0, 21.0, switch)
    heating = entry.runtime_data
    store = SimpleNamespace(observations=[])
    heating.analytics = SimpleNamespace(store=store, backfill_status="running")
    optimum = heating.optimum
    await controls.refresh()
    await optimum.step()
    assert optimum.fits == {} and optimum.learned_at is None
    assert optimum.report()["waiting"] == "history loading"

    heating.analytics.backfill_status = "complete"
    store.observations = history()
    await optimum.step()
    assert optimum.fits["study"].status == "ready" and optimum.report()["waiting"] is None

    # Ten hours ahead: no prediction yet, but the sensor says which warm-up is next.
    start_at, _, attrs = optimum.room_view("study")
    assert start_at is None
    assert attrs["next_warmup"] == switch.isoformat()
    assert (attrs["next_warmup_from"], attrs["next_warmup_to"]) == (16.0, 21.0)
    assert attrs["predicting_from"] == (switch - timedelta(minutes=180)).isoformat()
    assert calls == []
    heating.analytics = None


def test_goal_offset_is_commanded_target_minus_schedule_and_clamped():
    def room(air, schedule):
        return SimpleNamespace(data=SimpleNamespace(air_setpoint=air, schedule_setpoint=schedule))

    offset = Optimum._offset
    assert math.isclose(offset(room(19.3, 19.0)), 0.3)
    assert offset(room(25.0, 19.0)) == 2.0
    assert offset(room(10.0, 19.0)) == -1.0
    assert offset(room(None, 19.0)) == 0.0
    assert offset(room(19.3, None)) == 0.0
