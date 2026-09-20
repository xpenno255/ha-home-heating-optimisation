"""Metered energy: counter arithmetic, context, comparability, storage and isolation."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

from custom_components.home_heating_optimisation.advisor.evidence import build_evidence, encode
from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from custom_components.home_heating_optimisation.energy.analysis import (
    comparability,
    group_days,
    since_periods,
    summarise_period,
)
from custom_components.home_heating_optimisation.energy.const import BUCKET_SECONDS
from custom_components.home_heating_optimisation.energy.coordinator import (
    EnergyStore,
    configuration_era,
)
from custom_components.home_heating_optimisation.energy.meter import (
    allocation,
    bucket_context,
    meter_delta,
    meter_specs,
    to_kwh,
)
from tests.control.test_runtime import controlled as controlled  # noqa: PLC0414
from tests.control.test_runtime import start
from tests.test_integration import entity_id, setup

SPEC = {
    "slug": "fuel_input",
    "unit": None,
    "calorific_value_mj_m3": 39.5,
    "volume_correction": 1.02264,
}
BASE = datetime(2026, 9, 14, tzinfo=timezone.utc).timestamp()


def reading(value, minute, unit="kWh", entity="sensor.gas"):
    return {"value": value, "unit": unit, "time": BASE + minute * 60, "entity": entity}


def test_unit_conversions_and_unknown_units():
    assert to_kwh(1500, "Wh") == 1.5
    assert to_kwh(0.5, "MWh") == 500
    assert to_kwh(2, "kwh") == 2
    assert to_kwh(1, "m³") == pytest.approx(1.02264 * 39.5 / 3.6)
    assert to_kwh(1, "m3", calorific_value_mj_m3=36.0, volume_correction=1.0) == 10
    assert to_kwh(1, "ft³") is None
    assert to_kwh(True, "kWh") is None


def test_delta_ok_gap_reset_and_rollover():
    ok = meter_delta(reading(100.0, 0), reading(100.4, 5), SPEC)
    assert ok == {"kwh": pytest.approx(0.4), "quality": "ok", "source_changed": False}
    assert meter_delta(reading(100.0, 0), reading(101.0, 30), SPEC)["quality"] == "gap"
    reset = meter_delta(reading(100.0, 0), reading(0.3, 5), SPEC)
    assert reset["quality"] == "reset" and reset["kwh"] is None
    rollover = meter_delta(reading(99998.5, 0), reading(1.5, 5), SPEC)
    assert rollover["quality"] == "rollover" and rollover["kwh"] == pytest.approx(3.0)
    assert meter_delta(None, reading(5, 0), SPEC)["quality"] == "missing"
    assert meter_delta(reading(5, 0), None, SPEC)["quality"] == "missing"
    assert (
        meter_delta(reading(5, 0, unit="bananas"), reading(6, 5, unit="bananas"), SPEC)["quality"]
        == "unit_unknown"
    )
    wh = meter_delta(reading(5000, 0, unit="Wh"), reading(5500, 5, unit="Wh"), SPEC)
    assert wh["kwh"] == pytest.approx(0.5)


def test_source_or_unit_change_is_a_flagged_reset():
    changed = meter_delta(reading(5, 0), reading(6, 5, entity="sensor.other"), SPEC)
    assert changed == {"kwh": None, "quality": "reset", "source_changed": True}
    unit = meter_delta(reading(5, 0, unit="Wh"), reading(6, 5, unit="kWh"), SPEC)
    assert unit["source_changed"] and unit["kwh"] is None
    override = {**SPEC, "unit": "kWh"}
    assert meter_delta(reading(5, 0, unit=None), reading(6, 5, unit="Wh"), override)["kwh"] == 1


def test_context_shares_and_allocation_never_split():
    start, end = BASE, BASE + BUCKET_SECONDS
    samples = [(start - 60, True, False, 10.0), (start + 150, True, True, 12.0)]
    context = bucket_context(samples, start, end)
    assert context == {"heating": 1.0, "dhw": 0.5, "outdoor": 11.0}
    assert allocation(context["heating"], context["dhw"]) == "unknown"
    assert allocation(1.0, 0.0) == "heating"
    assert allocation(0.0, 0.3) == "dhw"
    assert allocation(0.0, 0.0) == "idle"
    assert allocation(None, 0.0) == "unknown"
    sparse = bucket_context([(start + 200, True, False, None)], start, end)
    assert sparse == {"heating": None, "dhw": None, "outdoor": None}


def test_meter_specs_bounds_and_slugs():
    config = {
        "energy": {
            "meters": [
                {"entity": "sensor.a", "kind": "fuel_input"},
                {"entity": "sensor.b", "kind": "fuel_input", "unit": "m3"},
                {"entity": "sensor.c", "kind": "bogus"},
                {"entity": "sensor.d", "kind": "electricity"},
                {"entity": "sensor.e", "kind": "electricity"},
            ]
        }
    }
    specs = meter_specs(config)
    assert [s["slug"] for s in specs] == ["fuel_input", "fuel_input_2"]
    assert specs[1]["unit"] == "m³"
    assert meter_specs({}) == [] and meter_specs({"energy": {}}) == []


def day_buckets(
    day, kwh=0.1, outdoor=5.0, dhw=0.0, era="observation", intervention=False, count=288
):
    start = BASE + day * 86400
    return [
        {
            "time": start + i * BUCKET_SECONDS,
            "meters": {"fuel_input": {"kwh": kwh, "quality": "ok", "source_changed": False}},
            "heating_share": 1.0,
            "dhw_share": dhw,
            "outdoor": outdoor,
            "allocation": "unknown" if dhw else "heating",
            "era": era,
            "intervention": intervention,
        }
        for i in range(count)
    ]


def test_group_days_and_clean_pair_is_comparable():
    a = group_days(day_buckets(0) + day_buckets(1) + day_buckets(2), "UTC", ["fuel_input"])
    b = group_days(
        day_buckets(3, kwh=0.12) + day_buckets(4, kwh=0.12) + day_buckets(5, kwh=0.12),
        "UTC",
        ["fuel_input"],
    )
    assert a[0]["kwh"]["fuel_input"] == pytest.approx(28.8)
    assert a[0]["coverage_percent"] == 100 and a[0]["degree_hours"] == pytest.approx(252)
    result = comparability(a, b, ["fuel_input"])
    assert result["conclusion"] == "comparable" and result["limits"] == []
    assert result["confidence"] == "medium"
    assert result["kwh_per_degree_hour"]["a"]["fuel_input"] == pytest.approx(28.8 / 252, rel=1e-3)
    assert result["note"] == "association, not causal evidence"
    assert "savings" not in encode(result).lower() or "No savings" in encode(result)


def test_comparability_rejects_era_mismatch_low_coverage_and_dhw_shift():
    a = group_days(day_buckets(0), "UTC", ["fuel_input"])
    era = group_days(day_buckets(1, era="abc123"), "UTC", ["fuel_input"])
    result = comparability(a, era, ["fuel_input"])
    assert result["conclusion"] == "insufficient"
    assert "configuration_era_mismatch" in result["limits"]
    assert result["kwh_per_degree_hour"] is None and result["confidence"] is None
    assert result["limitation"] == "No savings conclusion: metering/context insufficient"
    low = group_days(day_buckets(1, count=200), "UTC", ["fuel_input"])
    assert "period_b_coverage_below_80" in comparability(a, low, ["fuel_input"])["limits"]
    dhw = group_days(day_buckets(1, dhw=0.4), "UTC", ["fuel_input"])
    heavy = comparability(a, dhw, ["fuel_input"])
    assert "dhw_share_differs" in heavy["limits"]
    assert dhw[0]["allocation_unknown_share"] == 1.0
    cold = group_days(day_buckets(1, outdoor=-5.0), "UTC", ["fuel_input"])
    assert "degree_hour_ranges_do_not_overlap" in comparability(a, cold, ["fuel_input"])["limits"]
    trial = group_days(day_buckets(1, intervention=True), "UTC", ["fuel_input"])
    tried = comparability(a, trial, ["fuel_input"])
    assert tried["conclusion"] == "comparable" and tried["confidence"] == "low"
    assert "period_b_contains_interventions" in tried["limits"]
    assert comparability([], a, ["fuel_input"])["limits"] == ["period_a_no_data"]


def test_since_periods_split_equal_lengths():
    days = group_days(sum((day_buckets(d) for d in range(6)), []), "UTC", ["fuel_input"])
    since = datetime.fromtimestamp(BASE + 3 * 86400, timezone.utc)
    now = datetime.fromtimestamp(BASE + 5 * 86400 + 3600, timezone.utc)
    a, b, length = since_periods(days, since, now, "UTC")
    assert [d["date"] for d in a] == ["2026-09-17", "2026-09-18", "2026-09-19"]
    assert [d["date"] for d in b] == ["2026-09-14", "2026-09-15", "2026-09-16"]
    assert length == 3


def test_coverage_counts_wholly_missing_days_against_the_requested_span():
    """One observed day in an eight-day window is 12.5% coverage, not 100%."""
    days = group_days(day_buckets(0) + day_buckets(8), "UTC", ["fuel_input"])
    since = datetime.fromtimestamp(BASE + 1 * 86400, timezone.utc)
    now = datetime.fromtimestamp(BASE + 8 * 86400 + 3600, timezone.utc)
    a, b, length = since_periods(days, since, now, "UTC")
    assert length == 8 and [d["date"] for d in a] == ["2026-09-22"]
    assert a[0]["coverage_percent"] == 100
    summary = summarise_period(a, ["fuel_input"], expected_days=length)
    assert summary["days"] == 1 and summary["expected_days"] == 8
    assert summary["coverage_percent"] == pytest.approx(12.5)
    assert summary["context_coverage_percent"] == pytest.approx(12.5)
    result = comparability(a, b, ["fuel_input"], expected_days=length)
    assert result["conclusion"] == "insufficient"
    assert "period_a_coverage_below_80" in result["hard_limits"]
    assert result["periods"]["a"]["coverage_percent"] == pytest.approx(12.5)
    # Without an explicit span, gaps between the first and last observed day still count.
    sparse = summarise_period(days, ["fuel_input"])
    assert sparse["expected_days"] == 9 and sparse["coverage_percent"] == pytest.approx(22.2)
    full = summarise_period(group_days(day_buckets(0), "UTC", ["fuel_input"]), ["fuel_input"])
    assert full["coverage_percent"] == 100


def test_configuration_era_is_stable_and_observation_without_control():
    assert configuration_era({}) == "observation"
    control = {"rooms": {"study": {"config": {"primary_climate": "climate.study"}}}}
    era = configuration_era({"control": control})
    assert era == configuration_era({"control": deepcopy(control)}) and len(era) == 12
    other = deepcopy(control)
    other["rooms"]["study"]["config"]["primary_climate"] = "climate.other"
    assert configuration_era({"control": other}) != era


def metered(config):
    config = deepcopy(config)
    config["energy"] = {"meters": [{"entity": "sensor.gas", "kind": "fuel_input"}]}
    return config


def set_meter(hass, value, unit="kWh", device_class="energy", entity="sensor.gas"):
    hass.states.async_set(
        entity, value, {"unit_of_measurement": unit, "device_class": device_class}
    )


def freeze(hass, freezer, at="2026-09-14T10:00:00+00:00"):
    """Frozen clock with sources re-reported at that time so they are not future-dated."""
    freezer.move_to(at)
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": 20}, force_update=True
    )
    for entity, value in (
        ("sensor.study_demand", 50),
        ("sensor.outdoor", 5),
        ("sensor.flow", 50),
        ("sensor.return", 40),
        ("number.flow_setpoint", 55),
    ):
        hass.states.async_set(
            entity,
            value,
            {"unit_of_measurement": "%" if "demand" in entity else "°C"},
            force_update=True,
        )
    hass.states.async_set("binary_sensor.heating", "on", force_update=True)
    hass.states.async_set("binary_sensor.dhw", "off", force_update=True)


async def advance(hass, freezer, minutes):
    freezer.tick(timedelta(minutes=minutes))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()


async def test_no_meter_install_has_no_entities_or_store(hass, config, sources, hass_storage):
    entry = await setup(hass, config)
    assert entry.runtime_data.energy is None
    assert entity_id(hass, entry, "system:energy_status") is None
    assert not [k for k in hass_storage if k.endswith(".energy")]
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "get_energy_report", {}, blocking=True, return_response=True
        )
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["energy"] == {"enabled": False}


async def test_buckets_report_and_entities(hass, config, sources, freezer):
    freeze(hass, freezer)
    set_meter(hass, 1000.0)
    entry = await setup(hass, metered(config))
    energy = entry.runtime_data.energy
    assert energy is not None and energy.status == "insufficient_data"
    status = entity_id(hass, entry, "system:energy_status")
    daily = entity_id(hass, entry, "system:energy_fuel_input_daily_kwh")
    assert hass.states.get(status).state == "insufficient_data"
    assert hass.states.get(daily).state == "unknown"
    await advance(hass, freezer, 5)
    set_meter(hass, 1000.5)
    await advance(hass, freezer, 5)
    hass.states.async_set("binary_sensor.dhw", "on")
    set_meter(hass, 1001.0)
    await advance(hass, freezer, 5)
    buckets = energy.store.buckets
    assert [b["meters"]["fuel_input"]["quality"] for b in buckets] == ["missing", "ok", "ok"]
    assert buckets[1]["allocation"] == "heating" and buckets[2]["allocation"] == "unknown"
    assert buckets[1]["outdoor"] == 5.0 and buckets[1]["era"] == "observation"
    assert buckets[1]["intervention"] is False
    assert hass.states.get(status).state == "ready"
    assert float(hass.states.get(daily).state) == pytest.approx(1.0)
    attrs = hass.states.get(status).attributes
    assert attrs["meter_count"] == 1 and attrs["reset_count"] == 0
    assert "sensor." not in encode(attrs)
    report = await hass.services.async_call(
        DOMAIN, "get_energy_report", {"days": 2}, blocking=True, return_response=True
    )
    assert report["summary"]["kwh"]["fuel_input"] == pytest.approx(1.0)
    assert report["days"][0]["date"] == "2026-09-14"
    assert report["expected_days"] == 2 and report["observed_days"] == 1
    assert report["recent_vs_previous"]["conclusion"] == "insufficient"
    assert "period_b_no_data" in report["recent_vs_previous"]["hard_limits"]
    with pytest.raises(vol.Invalid):
        await hass.services.async_call(
            DOMAIN, "get_energy_report", {"days": 91}, blocking=True, return_response=True
        )
    result = energy.comparability(dt_util.utcnow() - timedelta(days=1))
    assert result["conclusion"] == "insufficient" and "limits" in result
    diagnostics = await async_get_config_entry_diagnostics(hass, entry)
    assert diagnostics["energy"]["meter_count"] == 1
    assert "sensor." not in str(diagnostics)


async def test_report_measures_coverage_over_the_requested_calendar_window(
    hass, config, sources, freezer
):
    """One observed day in an eight-day report window is 12.5% coverage, not 100%."""
    await hass.config.async_set_time_zone("UTC")
    freeze(hass, freezer, at="2026-09-22T10:00:00+00:00")
    set_meter(hass, 1000.0)
    entry = await setup(hass, metered(config))
    energy = entry.runtime_data.energy
    energy.store.buckets = day_buckets(1)  # the whole of 2026-09-15 only
    report = energy.report(8)
    assert report["expected_days"] == 8 and report["observed_days"] == 1
    assert report["previous_observed_days"] == 0
    assert [d["date"] for d in report["days"]] == ["2026-09-15"]
    assert report["summary"]["expected_days"] == 8
    assert report["summary"]["coverage_percent"] == pytest.approx(12.5)
    comparison = report["recent_vs_previous"]
    assert comparison["conclusion"] == "insufficient"
    assert "period_a_coverage_below_80" in comparison["hard_limits"]
    assert "period_b_no_data" in comparison["hard_limits"]
    assert comparison["periods"]["a"]["coverage_percent"] == pytest.approx(12.5)
    assert comparison["periods"]["b"]["expected_days"] == 8
    service = await hass.services.async_call(
        DOMAIN, "get_energy_report", {"days": 8}, blocking=True, return_response=True
    )
    assert service["expected_days"] == 8 and service["observed_days"] == 1


async def test_bucket_spanning_journal_command_is_flagged_as_intervention(
    hass, config, sources, freezer
):
    """The coordinator passes datetimes to journal.events; the flag must still be set."""
    freeze(hass, freezer)
    set_meter(hass, 1000.0)
    entry = await setup(hass, metered(config))
    energy, journal = entry.runtime_data.energy, entry.runtime_data.journal
    await advance(hass, freezer, 5)
    assert energy.store.buckets[-1]["intervention"] is False
    freezer.tick(timedelta(minutes=2))
    assert journal.record("command_sent", room_id="study", scope="study") is not None
    freezer.tick(timedelta(minutes=3))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    assert energy.store.buckets[-1]["intervention"] is True
    await advance(hass, freezer, 5)
    assert energy.store.buckets[-1]["intervention"] is False


async def test_daily_sensor_declares_last_reset_at_local_midnight(hass, config, sources, freezer):
    """TOTAL state class with a midnight reset needs last_reset or statistics corrupt."""
    freeze(hass, freezer)  # 10:00Z = 03:00 local (US/Pacific test default)
    set_meter(hass, 1000.0)
    entry = await setup(hass, metered(config))
    daily = entity_id(hass, entry, "system:energy_fuel_input_daily_kwh")
    await advance(hass, freezer, 5)
    set_meter(hass, 1000.5)
    await advance(hass, freezer, 5)
    state = hass.states.get(daily)
    assert state.attributes["state_class"] == "total"
    assert state.attributes["last_reset"] == "2026-09-14T00:00:00-07:00"
    assert float(state.state) == pytest.approx(0.5)
    freezer.move_to("2026-09-15T10:00:00+00:00")
    await advance(hass, freezer, 5)
    state = hass.states.get(daily)
    assert state.attributes["last_reset"] == "2026-09-15T00:00:00-07:00"
    assert dt_util.parse_datetime(state.attributes["last_reset"]) == dt_util.start_of_local_day()


async def test_reset_and_source_change_counted_and_persisted(
    hass, config, sources, freezer, hass_storage
):
    freeze(hass, freezer)
    set_meter(hass, 50.0)
    entry = await setup(hass, metered(config))
    energy = entry.runtime_data.energy
    await advance(hass, freezer, 5)
    set_meter(hass, 0.2)
    await advance(hass, freezer, 5)
    assert energy.store.buckets[-1]["meters"]["fuel_input"]["quality"] == "reset"
    assert energy.store.reset_count == 1
    set_meter(hass, 700.0, unit="Wh")
    await advance(hass, freezer, 5)
    assert energy.store.buckets[-1]["meters"]["fuel_input"]["source_changed"] is True
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert f"{DOMAIN}.{entry.entry_id}.energy" in hass_storage
    store = EnergyStore(hass, entry.entry_id)
    await store.load(energy.store.signature)
    assert store.status == "ready" and len(store.buckets) == 3 and store.reset_count == 2
    fresh = EnergyStore(hass, entry.entry_id)
    await fresh.load([{"slug": "other"}])
    assert fresh.status == "ready" and fresh.buckets == [] and fresh.reset_count == 0


async def test_corrupt_store_is_read_only_and_setup_survives(
    hass, config, sources, freezer, hass_storage
):
    freeze(hass, freezer)
    set_meter(hass, 50.0)
    entry = MockConfigEntry(
        domain=DOMAIN, title="Heating", unique_id=DOMAIN, data=metered(config), entry_id="nrg"
    )
    hass_storage[f"{DOMAIN}.nrg.energy"] = {"version": 1, "data": {"schema": 99, "buckets": "x"}}
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    energy = entry.runtime_data.energy
    assert energy.store.status == "storage_read_only" and energy.status == "storage_read_only"
    assert hass.states.get(entity_id(hass, entry, "system:energy_status")).state == (
        "storage_read_only"
    )
    await advance(hass, freezer, 5)
    assert len(energy.store.buckets) == 1  # memory-only collection continues
    await energy.store.save()
    assert hass_storage[f"{DOMAIN}.nrg.energy"]["data"]["schema"] == 99  # file preserved


async def test_energy_failures_never_raise_into_setup_or_control(hass, config, sources, freezer):
    freeze(hass, freezer)
    set_meter(hass, 50.0)
    with patch(
        "custom_components.home_heating_optimisation.energy.coordinator.EnergyStore.load",
        side_effect=RuntimeError("disk"),
    ):
        entry = await setup(hass, metered(config))
    assert entry.state.value == "loaded" and entry.runtime_data.energy is None
    assert hass.states.get(entity_id(hass, entry, "system:operating_state")).state == "heating"
    await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    entry = await setup(hass, metered(config))
    energy = entry.runtime_data.energy
    with patch.object(energy, "build_bucket", side_effect=RuntimeError("boom")):
        await advance(hass, freezer, 5)
    assert energy.store.buckets == []
    with patch.object(energy.store.backend, "async_save", side_effect=OSError("disk full")):
        await energy.store.save()
    assert energy.store.status == "save_failed"
    hass.states.async_set("binary_sensor.dhw", "on")
    await hass.async_block_till_done()
    assert hass.states.get(entity_id(hass, entry, "system:operating_state")).state == "mixed"


async def test_control_runtime_unaffected_by_energy(hass, controlled, sources, freezer):
    set_meter(hass, 50.0)
    entry, controls, calls = await start(hass, metered(controlled))
    energy = entry.runtime_data.energy
    assert energy is not None and energy.store.buckets == []
    assert configuration_era(entry.runtime_data.config) != "observation"
    await handover(controls)
    await controls.set_mode("boiler", "auto")
    await controls.set_mode("study", "active")
    with patch.object(energy.store.backend, "async_save", side_effect=OSError("disk full")):
        await energy.store.save()
    await advance(hass, freezer, 5)
    assert len(energy.store.buckets) == 1
    assert controls.settings.get("ownership") == "ready"
    assert {kind for kind, _ in calls} == {"number", "ramses"}


def test_evidence_energy_section_is_allowlisted():
    facts = build_evidence(
        {
            "quality": {},
            "definitions": {},
            "settings": {},
            "analysis": {"zone_stats": {}},
            "comparison": {},
            "rooms": [],
        },
        {"rooms": {}, "warnings": []},
        {},
        "weekly_review",
        energy={
            "quality": {
                "status": "ready",
                "meter_kinds": ["fuel_input"],
                "coverage_percent_7d": 50,
            },
            "days": [{"kwh": {"fuel_input": 12.3}, "date": "2026-09-14"}],
            "recent_vs_previous": {
                "limits": ["period_b_coverage_below_80"],
                "conclusion": "insufficient",
                "kwh_per_degree_hour": None,
                "confidence": None,
            },
            "meters": [{"entity": "sensor.private_gas"}],
        },
    )["facts"]
    energy = facts["energy"]
    assert energy["comparability_limits"] == ["period_b_coverage_below_80"]
    assert energy["limitations"] == ["No savings conclusion: metering/context insufficient"]
    assert "12.3" not in encode(energy) and "sensor.private_gas" not in encode(facts)
    assert (
        build_evidence(
            {
                "quality": {},
                "definitions": {},
                "settings": {},
                "analysis": {"zone_stats": {}},
                "comparison": {},
                "rooms": [],
            },
            {"rooms": {}, "warnings": []},
            {},
            "weekly_review",
        )["facts"]["energy"]["status"]
        == "no_meter"
    )


async def test_options_energy_step_validates_and_empty_disables(hass, config, sources):
    set_meter(hass, 12.0, unit="m³", device_class="gas")
    hass.states.async_set("sensor.temp", 20, {"device_class": "temperature"})
    entry = await setup(hass, config)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "energy" in flow["menu_options"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "energy"}
    )
    assert flow["step_id"] == "energy"
    bad = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"meter_1_entity": "sensor.temp", "calorific_value_mj_m3": 39.5, "volume_correction": 1.0},
    )
    assert bad["errors"] == {"base": "invalid_energy_meter"}
    duplicate = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "meter_1_entity": "sensor.gas",
            "meter_2_entity": "sensor.gas",
            "calorific_value_mj_m3": 39.5,
            "volume_correction": 1.0,
        },
    )
    assert duplicate["errors"] == {"base": "duplicate_energy_meter"}
    done = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            "meter_1_entity": "sensor.gas",
            "meter_1_kind": "fuel_input",
            "meter_1_unit": "m³",
            "calorific_value_mj_m3": 38.0,
            "volume_correction": 1.0,
        },
    )
    assert done["type"] == "create_entry"
    assert done["data"]["rooms"] == config["rooms"]
    assert done["data"]["energy"]["meters"][0] == {
        "entity": "sensor.gas",
        "kind": "fuel_input",
        "unit": "m³",
        "calorific_value_mj_m3": 38.0,
        "volume_correction": 1.0,
    }
    await hass.async_block_till_done()
    assert entry.runtime_data.energy is not None
    assert entry.runtime_data.energy.specs[0]["calorific_value_mj_m3"] == 38.0
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "energy"}
    )
    cleared = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"calorific_value_mj_m3": 39.5, "volume_correction": 1.02264}
    )
    assert cleared["type"] == "create_entry" and cleared["data"]["energy"] == {}
    await hass.async_block_till_done()
    assert entry.runtime_data.energy is None
    assert entity_id(hass, entry, "system:energy_status") is None
