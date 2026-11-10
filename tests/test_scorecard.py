"""Daily scorecard: pure day arithmetic and the collector wired into a running entry."""

from datetime import datetime, timedelta, timezone

import pytest

from custom_components.home_heating_optimisation.scorecard.core import (
    add_gas,
    add_sample,
    add_write,
    burner_summary,
    empty_day,
    finalise,
    gas_delta,
    valid_day,
)
from custom_components.home_heating_optimisation.scorecard.sensor import ENTITY_ID
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start


def room(air, target, state="active", in_window=True):
    return {"name": "Study", "state": state, "in_window": in_window, "air": air, "target": target}


def test_comfort_counts_only_normal_control_against_the_air_target():
    day = empty_day("2026-11-11")
    boiler = {"outdoor": 5.5, "dhw": False, "power": 0, "return_c": 30}
    add_sample(day, 60, boiler=boiler, rooms={"study": room(19.0, 19.0)})  # capped to 5 min
    for _ in range(11):
        add_sample(day, 5, boiler=boiler, rooms={"study": room(19.0, 19.0)})
    for _ in range(6):
        add_sample(day, 5, boiler=boiler, rooms={"study": room(18.2, 19.0)})
    # Not the controller's result: pre-heat, a hold, an open window, outside the window.
    add_sample(day, 5, boiler=boiler, rooms={"study": room(20.0, 18.0, "preheat")})
    add_sample(day, 5, boiler=boiler, rooms={"study": room(17.0, 19.0, "manual")})
    add_sample(day, 5, boiler=boiler, rooms={"study": room(12.0, 19.0, "window_open")})
    add_sample(day, 5, boiler=boiler, rooms={"study": room(17.0, 19.0, "active", False)})
    add_write(day, "study")
    row = finalise(day)
    study = row["rooms"]["study"]
    assert study["minutes"] == 90 and study["held_minutes"] == 10 and study["writes"] == 1
    assert study["within"] == pytest.approx(60 / 90, abs=1e-3)
    assert study["short"] == pytest.approx(30 / 90, abs=1e-3)
    assert study["short_degree_hours"] == 0.4 and study["over_degree_hours"] == 0
    assert study["mean_error_k"] == pytest.approx(-0.27, abs=0.01)
    assert row["comfort_within"] == study["within"] and row["writes"] == 1
    assert row["outdoor_c"] == 5.5
    assert row["degree_days"] == pytest.approx(10 * 110 / 1440, abs=0.01)
    assert row["coverage"] == pytest.approx(110 / 1440, abs=1e-3)
    assert row["gas_kwh"] is None and row["heating_kwh_per_degree_day"] is None


def test_gas_split_condensing_and_burner_figures():
    day = empty_day("2026-11-11")
    for _ in range(288):
        add_sample(
            day, 5, boiler={"outdoor": 5.5, "dhw": False, "power": 40, "return_c": 35}, rooms={}
        )
    add_sample(day, 5, boiler={"outdoor": 5.5, "dhw": False, "power": 60, "return_c": 57}, rooms={})
    # A hot water burn is not judged for condensing.
    add_sample(day, 5, boiler={"outdoor": 5.5, "dhw": True, "power": 60, "return_c": 60}, rooms={})
    add_gas(day, 50.0, dhw=False, split=True)
    add_gas(day, 12.0, dhw=True, split=True)
    add_gas(day, 3.0, dhw=True, split=False)  # spans a gap: counted, not called hot water
    burns = [
        [100.0, 60.0, 30.0, 0],
        [200.0, 600.0, 40.0, 0],
        [900.0, 1800.0, 55.0, 1],
        [5000, 9, 1, 0],
    ]
    row = finalise(day, burner_summary(burns, 0, 1000))
    assert row["gas_kwh"] == 65.0 and row["gas_dhw_kwh"] == 12.0 and row["gas_heating_kwh"] == 53.0
    assert row["gas_unsplit_kwh"] == 3.0
    assert row["heating_kwh_per_degree_day"] == pytest.approx(53.0 / row["degree_days"], abs=0.01)
    assert row["condensing_share"] == pytest.approx(288 / 289, abs=1e-3)
    assert row["burner"] == {
        "starts": 3,
        "dhw_starts": 1,
        "short_burns": 1,
        "firing_minutes": 41.0,
        "mean_burn_minutes": 13.7,
    }


def test_gas_delta_rejects_resets_and_impossible_jumps():
    assert gas_delta(100.0, 100.4, 1) == pytest.approx(0.4)
    assert gas_delta(100.0, 99.0, 1) is None  # counter reset
    assert gas_delta(100.0, 110.0, 1) is None  # faster than any boiler
    assert gas_delta(100.0, 110.0, 60) == 10.0
    assert gas_delta(None, 110.0, 1) is None and gas_delta(100.0, 101.0, None) is None


def test_a_mild_day_gives_no_ratio_and_stored_days_are_checked():
    day = empty_day("2026-11-11")
    add_sample(day, 5, boiler={"outdoor": 15.0}, rooms={"study": room(19, 19)})
    add_gas(day, 4.0, dhw=False, split=True)
    assert finalise(day)["heating_kwh_per_degree_day"] is None
    assert valid_day(day) and not valid_day({"date": "x"}) and not valid_day(None)
    day["rooms"]["study"].pop("held")
    assert not valid_day(day)


async def test_collector_samples_rolls_the_day_and_reports(hass, controlled, sources, freezer):  # noqa: F811
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(datetime(2026, 11, 10, 23, 50, tzinfo=timezone.utc))
    controlled["energy"] = {"meters": [{"entity": "sensor.gas", "kind": "fuel_input"}]}
    hass.states.async_set("sensor.gas", 1000.0, {"unit_of_measurement": "kWh"})
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    card = entry.runtime_data.scorecard
    assert card is not None and card.status == "ready" and card.days == []
    await card.sample()
    for step in range(1, 6):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.gas", 1000.0 + 0.3 * step, {"unit_of_measurement": "kWh"})
        await card.sample()
    today = card.today()
    assert today["date"] == "2026-11-10" and today["gas_kwh"] == pytest.approx(1.5)
    assert today["coverage"] == pytest.approx(6 / 1440, abs=1e-3)
    assert "study" in today["rooms"] and today["burner"]["starts"] == 0
    freezer.move_to(datetime(2026, 11, 11, 0, 1, tzinfo=timezone.utc))
    await card.sample()
    await hass.async_block_till_done()
    assert [d["date"] for d in card.days] == ["2026-11-10"]
    assert card.today()["date"] == "2026-11-11"
    state = hass.states.get(ENTITY_ID)
    assert state is not None and state.attributes["date"] == "2026-11-10"
    assert state.attributes["gas_kwh"] == pytest.approx(1.5)
    assert "rooms" not in state.attributes["days"][0]
    response = await hass.services.async_call(
        "home_heating_optimisation",
        "get_scorecard",
        {"days": 5},
        blocking=True,
        return_response=True,
    )
    assert response["days"][0]["rooms"]["study"]["name"] == c.rooms["study"].room_name
    assert response["definitions"]["degree_day_base_c"] == 15.5
    # A reload keeps the finished day and the part-day.
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    again = entry.runtime_data.scorecard
    assert again is not card and [d["date"] for d in again.days] == ["2026-11-10"]
    assert again.current["date"] == "2026-11-11"
