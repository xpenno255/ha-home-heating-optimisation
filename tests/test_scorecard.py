"""Daily scorecard: pure day arithmetic and the collector wired into a running entry."""

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.data_entry_flow import InvalidData

from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.scorecard.core import (
    add_gas,
    add_globe,
    add_meter_gas,
    add_sample,
    add_write,
    burner_summary,
    day_part,
    empty_day,
    finalise,
    gas_delta,
    note_arrival,
    progress_arrivals,
    restore_day,
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
    assert restore_day({"date": "x"}) is None and restore_day(None) is None
    # A part-day stored by an older version gains whatever has been added since.
    day["rooms"]["study"].pop("held")
    day["rooms"]["study"].pop("arrivals")
    day["rooms"]["other"] = {"name": "Other"}
    day.pop("gas_meter")
    day.pop("meter_minutes")
    day.pop("globe")
    restored = restore_day(day)
    assert restored["rooms"]["study"]["held"] == 0.0 and restored["gas"] == 4.0
    assert restored["globe"] == {} and restored["rooms"]["study"]["arrivals"] == []
    # Each room gets its own list, not one shared between them.
    assert restored["rooms"]["study"]["arrivals"] is not restored["rooms"]["other"]["arrivals"]
    assert restored["gas_meter"] is None and restored["meter_minutes"] == 0.0
    add_sample(restored, 5, boiler={"outdoor": 15.0}, rooms={"study": room(19, 19)})
    assert finalise(restored)["gas_source"] == "boiler_counter"


def metered_day(live_samples, total_samples=288):
    day = empty_day("2026-11-12")
    for index in range(total_samples):
        add_sample(day, 5, boiler={"outdoor": 5.5}, rooms={}, meter_live=index < live_samples)
    add_gas(day, 80.0, dhw=False, split=True)
    add_gas(day, 20.0, dhw=True, split=True)
    add_meter_gas(day, 92.0)
    return day


def test_the_billing_meter_sets_the_amount_and_the_boiler_counter_the_split():
    row = finalise(metered_day(288))
    assert row["gas_source"] == "meter" and row["gas_kwh"] == 92.0
    assert row["gas_dhw_kwh"] == 18.4 and row["gas_heating_kwh"] == 73.6
    assert row["gas_counter_kwh"] == 100.0 and row["gas_meter_kwh"] == 92.0
    assert row["counter_to_meter"] == pytest.approx(1.087, abs=1e-3)
    assert row["heating_kwh_per_degree_day"] == pytest.approx(73.6 / 10, abs=0.01)


def test_a_meter_that_missed_part_of_the_day_is_not_used():
    row = finalise(metered_day(200))  # reporting for under 90 % of the day
    assert row["gas_source"] == "boiler_counter" and row["gas_kwh"] == 100.0
    assert row["gas_dhw_kwh"] == 20.0 and row["counter_to_meter"] is None
    assert row["gas_meter_kwh"] == 92.0 and row["gas_meter_coverage"] == pytest.approx(
        0.694, abs=1e-3
    )
    # A live meter on a day with no gas reads zero, not nothing.
    day = empty_day("2026-07-01")
    add_sample(day, 5, boiler={"outdoor": 20.0}, rooms={}, meter_live=True)
    row = finalise(day)
    assert row["gas_kwh"] == 0.0 and row["gas_source"] == "meter" and row["gas_heating_kwh"] is None


async def test_collector_samples_rolls_the_day_and_reports(hass, controlled, sources, freezer):  # noqa: F811
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(datetime(2026, 11, 10, 23, 50, tzinfo=timezone.utc))
    controlled["energy"] = {
        "meters": [{"entity": "sensor.gas", "kind": "fuel_input"}],
        "billing_meter": "sensor.supplier_gas",
    }
    hass.states.async_set("sensor.gas", 1000.0, {"unit_of_measurement": "kWh"})
    hass.states.async_set("sensor.supplier_gas", 500.0, {"unit_of_measurement": "kWh"})
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    card = entry.runtime_data.scorecard
    assert card is not None and card.status == "ready" and card.days == []
    await card.sample()
    for step in range(1, 6):
        freezer.tick(timedelta(minutes=1))
        hass.states.async_set("sensor.gas", 1000.0 + 0.3 * step, {"unit_of_measurement": "kWh"})
        if step == 4:  # the supplier's meter reports rarely
            hass.states.async_set("sensor.supplier_gas", 501.2, {"unit_of_measurement": "kWh"})
        await card.sample()
    today = card.today()
    assert today["date"] == "2026-11-10" and today["gas_counter_kwh"] == pytest.approx(1.5)
    assert today["gas_source"] == "meter" and today["gas_kwh"] == pytest.approx(1.2)
    assert today["counter_to_meter"] == pytest.approx(1.25)
    assert today["coverage"] == pytest.approx(6 / 1440, abs=1e-3)
    assert "study" in today["rooms"] and today["burner"]["starts"] == 0
    freezer.move_to(datetime(2026, 11, 11, 0, 1, tzinfo=timezone.utc))
    await card.sample()
    await hass.async_block_till_done()
    assert [d["date"] for d in card.days] == ["2026-11-10"]
    assert card.today()["date"] == "2026-11-11"
    state = hass.states.get(ENTITY_ID)
    assert state is not None and state.attributes["date"] == "2026-11-10"
    assert state.attributes["gas_kwh"] == pytest.approx(1.2)
    assert state.attributes["gas_source"] == "meter"
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
    assert again.current["date"] == "2026-11-11" and again.billing["kwh"] == 501.2
    # A reading after a long silence covers time nobody watched, so it is left out.
    freezer.tick(timedelta(hours=5))
    hass.states.async_set("sensor.supplier_gas", 560.0, {"unit_of_measurement": "kWh"})
    await again.sample()
    assert again.current["gas_meter"] in (None, 0.0) and again.billing["kwh"] == 560.0
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("sensor.supplier_gas", 560.4, {"unit_of_measurement": "kWh"})
    await again.sample()
    assert again.current["gas_meter"] == pytest.approx(0.4)


async def test_options_flow_stores_the_billing_meter(hass, config, sources):
    from custom_components.home_heating_optimisation.const import effective_config
    from tests.test_integration import setup

    entry = await setup(hass, config)
    hass.states.async_set(
        "sensor.gas", 10.0, {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    hass.states.async_set(
        "sensor.supplier_gas", 5.0, {"unit_of_measurement": "kWh", "device_class": "energy"}
    )
    hass.states.async_set("sensor.not_a_meter", 5.0, {"unit_of_measurement": "°C"})

    async def submit(values):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "energy"}
        )
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    base = {
        "meter_1_entity": "sensor.gas",
        "meter_1_kind": "fuel_input",
        "calorific_value_mj_m3": 39.5,
        "volume_correction": 1.02264,
    }
    bad = await submit({**base, "billing_meter": "sensor.not_a_meter"})
    assert bad["errors"] == {"base": "invalid_energy_meter"}
    same = await submit({**base, "billing_meter": "sensor.gas"})
    assert same["errors"] == {"base": "invalid_energy_meter"}
    assert (await submit({**base, "billing_meter": "sensor.supplier_gas"}))[
        "type"
    ] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["energy"]["billing_meter"] == "sensor.supplier_gas"
    assert entry.runtime_data.scorecard.billing_entity == "sensor.supplier_gas"
    assert (await submit(base))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert "billing_meter" not in effective_config(entry)["energy"]


def test_arrivals_record_how_far_under_and_how_long_to_get_there():
    day = empty_day("2026-11-12")
    t0 = 1_800_000_000.0
    note_arrival(day, "study", "Study", at="07:00", ts=t0, step_k=0.5, error_k=-1.04)
    note_arrival(day, "hall", "Hall", at="07:00", ts=t0, step_k=1.0, error_k=-0.2)  # already there
    progress_arrivals(day, "study", ts=t0 + 20 * 60, error_k=-0.6)
    assert day["rooms"]["study"]["arrivals"][0]["minutes"] is None
    progress_arrivals(day, "study", ts=t0 + 47 * 60, error_k=-0.3)
    progress_arrivals(day, "study", ts=t0 + 90 * 60, error_k=0.1)  # already closed
    # A second rise that the room never reaches within three hours stays open.
    note_arrival(day, "study", "Study", at="08:00", ts=t0 + 3600, step_k=1.5, error_k=-1.5)
    progress_arrivals(day, "study", ts=t0 + 3600 + 181 * 60, error_k=0.0)
    row = finalise(day)
    assert row["rooms"]["study"]["arrivals"] == [
        {"at": "07:00", "step_k": 0.5, "error_k": -1.04, "minutes_to_target": 47},
        {"at": "08:00", "step_k": 1.5, "error_k": -1.5, "minutes_to_target": None},
    ]
    assert row["rooms"]["hall"]["arrivals"][0]["minutes_to_target"] == 0
    for _ in range(6):
        note_arrival(day, "hall", "Hall", at="09:00", ts=t0, step_k=0.5, error_k=0.0)
    assert len(day["rooms"]["hall"]["arrivals"]) == 4


def test_globe_figures_by_part_of_day_and_applied_correction():
    assert [day_part(h) for h in (23, 0, 5, 6, 8, 9, 15, 16, 22)] == [
        "night", "night", "night", "morning", "morning", "daytime", "daytime", "evening", "evening",
    ]  # fmt: skip
    day = empty_day("2026-11-12")
    for _ in range(12):
        add_globe(day, "evening", 5, air=20.4, globe=19.4, applied=0.68, target=19.5, outdoor=2.3)
    add_globe(day, "evening", 5, air=None, globe=19.4, applied=0.68, target=19.5)  # ignored
    add_globe(day, "daytime", 5, air=19.6, globe=18.9, applied=None, target=None)
    add_sample(
        day,
        5,
        boiler=None,
        rooms={"living": {**room(20.4, 20.2), "offset": 0.68}},
    )
    row = finalise(day)
    evening = row["globe"]["evening"]
    assert evening["minutes"] == 60 and evening["outdoor_c"] == 2.3
    assert evening["air_minus_globe_k"] == 1.0 and evening["needed_k"] == 0.8
    assert evening["applied_k"] == 0.68 and evening["felt_vs_target_k"] == 0.1
    assert row["globe"]["daytime"]["applied_k"] is None
    assert row["globe"]["daytime"]["felt_vs_target_k"] is None
    assert row["rooms"]["living"]["applied_correction_k"] == 0.68
    assert finalise(empty_day("2026-11-13"))["globe"] is None


async def test_collector_records_the_globe_and_an_arrival(hass, controlled, sources, freezer):  # noqa: F811
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(datetime(2026, 11, 12, 18, 0, tzinfo=timezone.utc))
    controlled["scorecard"] = {"globe_entity": "sensor.globe", "globe_room": "study"}
    hass.states.async_set("sensor.globe", 17.2, {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18.0, "temperature": 20})
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    await c.refresh()
    card = entry.runtime_data.scorecard
    data = c.rooms["study"].data
    assert data.state == "active" and data.target_ot is not None
    await card.sample()
    # The scheduled target was a degree lower a minute ago.
    card._last_target["study"] = data.target_ot - 1.0
    freezer.tick(timedelta(minutes=1))
    hass.states.async_set("sensor.globe", 17.2, {"unit_of_measurement": "°C"})
    await card.sample()
    today = card.today()
    evening = today["globe"]["evening"]
    assert evening["air_minus_globe_k"] == pytest.approx(data.air_temp - 17.2, abs=0.01)
    assert evening["applied_k"] == pytest.approx(data.offset_final, abs=0.01)
    [arrival] = today["rooms"]["study"]["arrivals"]
    assert arrival["at"] == "18:01" and arrival["step_k"] == 1.0
    assert arrival["error_k"] == pytest.approx(data.air_temp - data.air_setpoint, abs=0.01)
    # A globe that has gone quiet is not counted.
    freezer.tick(timedelta(minutes=45))
    await card.sample()
    assert card.today()["globe"]["evening"]["minutes"] == evening["minutes"]


async def test_options_flow_stores_the_globe(hass, controlled, sources):  # noqa: F811
    from custom_components.home_heating_optimisation.const import effective_config

    entry, c, calls = await start(hass, controlled)
    hass.states.async_set(
        "sensor.globe", 19.0, {"unit_of_measurement": "°C", "device_class": "temperature"}
    )
    hass.states.async_set("sensor.gas", 10.0, {"device_class": "energy"})

    async def submit(values):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "scorecard" in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "scorecard"}
        )
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    for bad in (
        {"globe_entity": "sensor.gas", "globe_room": "study"},
        {"globe_entity": "sensor.globe"},
    ):
        assert (await submit(bad))["errors"] == {"base": "invalid_globe"}
    # A room that is not one of the controlled rooms never gets past the form.
    with pytest.raises(InvalidData):
        await submit({"globe_entity": "sensor.globe", "globe_room": "nowhere"})
    saved = await submit({"globe_entity": "sensor.globe", "globe_room": "study"})
    assert saved["type"] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["scorecard"] == {
        "globe_entity": "sensor.globe",
        "globe_room": "study",
    }
    assert entry.runtime_data.scorecard.globe_room == "study"
    assert (await submit({}))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["scorecard"] == {}
