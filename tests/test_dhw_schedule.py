"""DHW target schedule: one weekly higher target, restored, never re-raised."""

from copy import deepcopy
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import patch
from zoneinfo import ZoneInfo

import pytest
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.const import DOMAIN, effective_config
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.dhw.policy import (
    Evidence,
    dhw_config,
    next_window,
    session_due,
    validation_error,
)
from custom_components.home_heating_optimisation.source_identity import update_source_references
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start
from tests.test_integration import setup

WH = "water_heater.stored_hw"
SENSOR = f"sensor.{DOMAIN}_dhw_schedule"
LONDON = ZoneInfo("Europe/London")
# Monday 28 September 2026, 03:00 local (BST).
SUNDAY_NIGHT = datetime(2026, 9, 28, 2, 0, tzinfo=dt_util.UTC)


def with_dhw(config, **extra):
    config = deepcopy(config)
    config["dhw_schedule"] = {
        "enabled": True,
        "water_heater_entity": WH,
        "demand_entity": "sensor.hw",
        "cylinder_temp_entity": "sensor.cylinder",
        "weekdays": ["mon"],
        "revision": "r1",
        **extra,
    }
    return config


def heater(hass, setpoint=50.0, overrun=2, differential=7.5, active=True, mode="follow_schedule"):
    hass.states.async_set(
        WH,
        "auto",
        {
            "temperature": setpoint,
            "current_temperature": 49.5,
            "params": {
                "config": {"setpoint": setpoint, "overrun": overrun, "differential": differential}
            },
            "mode": {"mode": mode, "active": active, "until": None},
        },
    )


async def begin(hass, controlled, freezer, reply=True, **extra):  # noqa: F811
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(SUNDAY_NIGHT)
    heater(hass)
    entry, controls, calls = await start(hass, with_dhw(controlled, **extra))
    writes = []

    async def set_params(call):
        writes.append(dict(call.data))
        if reply:
            heater(hass, call.data["setpoint"], call.data["overrun"], call.data["differential"])

    hass.services.async_register("ramses_cc", "set_dhw_params", set_params)
    await handover(controls)
    schedule = entry.runtime_data.dhw
    schedule._mono = lambda: dt_util.utcnow().timestamp()
    return entry, schedule, writes


async def at(hass, freezer, schedule, when=None, **delta):
    freezer.move_to(when) if when else freezer.tick(timedelta(**delta))
    await hass.async_block_till_done()
    await schedule.step()
    await hass.async_block_till_done()


def monday(hour, minute=0):
    return datetime(2026, 9, 28, hour, minute, tzinfo=LONDON)


async def test_disabled_by_default_makes_no_writes(hass, controlled, sources, freezer):  # noqa: F811
    heater(hass)
    entry, controls, _ = await start(hass, controlled)
    writes = []
    hass.services.async_register("ramses_cc", "set_dhw_params", lambda c: writes.append(c))
    await handover(controls)
    schedule = entry.runtime_data.dhw
    await schedule.step()
    assert schedule.status == "disabled" and writes == []
    assert hass.states.get(SENSOR).state == "disabled"
    assert controls.report()["dhw_schedule"]["status"] == "disabled"


async def test_completed_session_raises_once_and_restores_preserving_params(
    hass,
    controlled,  # noqa: F811
    sources,
    freezer,
):
    _, schedule, writes = await begin(hass, controlled, freezer)
    await schedule.step()
    assert writes == [] and schedule.status == "armed"  # baseline already normal
    await at(hass, freezer, schedule, monday(3, 59))
    assert writes == []
    await at(hass, freezer, schedule, monday(4))
    assert writes == [{"entity_id": WH, "setpoint": 60.0, "overrun": 2, "differential": 7.5}]
    assert schedule.status == "elevated"
    assert hass.states.get(SENSOR).attributes["transaction"]["day"] == "2026-09-28"

    # Demand off at the start is not completion.
    await at(hass, freezer, schedule, minutes=15)
    assert len(writes) == 1
    hass.states.async_set("sensor.hw", 100)
    await at(hass, freezer, schedule, minutes=20)
    hass.states.async_set("sensor.cylinder", 60.5, {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.hw", 0)
    await at(hass, freezer, schedule, minutes=5)
    # A thermostat cycle (demand back on) resets the off dwell.
    hass.states.async_set("sensor.hw", 100)
    await at(hass, freezer, schedule, minutes=1)
    hass.states.async_set("sensor.hw", 0)
    await at(hass, freezer, schedule, minutes=9)
    assert len(writes) == 1
    await at(hass, freezer, schedule, minutes=1)
    assert writes[-1] == {"entity_id": WH, "setpoint": 50.0, "overrun": 2, "differential": 7.5}
    assert schedule.status == "restoring"
    await at(hass, freezer, schedule, seconds=90)
    assert schedule.status == "armed"
    assert schedule.store.data["last"]["outcome"] == "complete"
    await at(hass, freezer, schedule, monday(5, 30))
    assert len(writes) == 2  # never a second session on the same date


async def test_deadline_restores_and_records_no_charge(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    await at(hass, freezer, schedule, monday(4))
    await at(hass, freezer, schedule, monday(5, 59))
    assert len(writes) == 1
    await at(hass, freezer, schedule, monday(6))
    assert writes[-1]["setpoint"] == 50.0
    await at(hass, freezer, schedule, minutes=2)
    assert schedule.store.data["last"]["outcome"] == "no_charge"


async def test_deadline_distinguishes_target_not_reached(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    await at(hass, freezer, schedule, monday(4))
    hass.states.async_set("sensor.hw", 100)
    await at(hass, freezer, schedule, monday(6))
    await at(hass, freezer, schedule, minutes=2)
    assert schedule.store.data["last"]["outcome"] == "target_not_reached"


async def test_schedule_off_after_charging_ends_early_as_incomplete(
    hass,
    controlled,  # noqa: F811
    sources,
    freezer,
):
    _, schedule, writes = await begin(hass, controlled, freezer)
    await at(hass, freezer, schedule, monday(4))
    hass.states.async_set("sensor.hw", 100)
    await at(hass, freezer, schedule, minutes=30)
    hass.states.async_set("sensor.hw", 0)
    heater(hass, 60.0, active=False)
    await at(hass, freezer, schedule, minutes=1)
    assert writes[-1]["setpoint"] == 50.0
    await at(hass, freezer, schedule, minutes=2)
    assert schedule.store.data["last"]["outcome"] == "incomplete"


async def test_external_change_pauses_without_overwriting(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    await at(hass, freezer, schedule, monday(4))
    await at(hass, freezer, schedule, minutes=5)
    heater(hass, 55.0)  # someone else set a different target
    await at(hass, freezer, schedule, minutes=1)
    assert len(writes) == 1
    assert schedule.status == "paused"
    assert schedule.store.data["last"]["outcome"] == "external_change"
    await at(hass, freezer, schedule, monday(6, 30))
    assert len(writes) == 1


async def test_unexpected_target_at_opening_pauses(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    heater(hass, 52.0)
    await at(hass, freezer, schedule, monday(4))
    assert writes == [] and schedule.status == "paused"


async def test_save_failure_means_no_write(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    with patch.object(schedule.store.backend, "async_save", side_effect=OSError("disk")):
        await at(hass, freezer, schedule, monday(4))
    assert writes == []
    assert schedule.store.data["consumed"] == "2026-09-28"
    await at(hass, freezer, schedule, minutes=5)
    assert writes == []


async def test_start_mid_window_skips_the_session(hass, controlled, sources, freezer):  # noqa: F811
    await hass.config.async_set_time_zone("Europe/London")
    freezer.move_to(monday(4, 30))
    heater(hass)
    entry, controls, _ = await start(hass, with_dhw(controlled))
    writes = []
    hass.services.async_register("ramses_cc", "set_dhw_params", lambda c: writes.append(c))
    await handover(controls)
    await entry.runtime_data.dhw.step()
    assert writes == []


async def test_unload_restores_and_restart_never_reraises(hass, controlled, sources, freezer):  # noqa: F811
    entry, schedule, writes = await begin(hass, controlled, freezer)
    await at(hass, freezer, schedule, monday(4))
    await at(hass, freezer, schedule, minutes=10)
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Restored before control shut down, with parameters preserved.
    assert writes[-1] == {"entity_id": WH, "setpoint": 50.0, "overrun": 2, "differential": 7.5}
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    schedule = entry.runtime_data.dhw
    schedule._mono = lambda: dt_util.utcnow().timestamp()
    assert schedule.status == "restoring"
    await at(hass, freezer, schedule, minutes=3)
    assert schedule.status == "armed"
    assert schedule.store.data["last"]["outcome"] == "interrupted"
    await at(hass, freezer, schedule, monday(5))
    assert len(writes) == 2


async def test_restart_with_elevation_restores_even_when_disabled(
    hass,
    controlled,  # noqa: F811
    sources,
    freezer,
):
    entry, schedule, writes = await begin(hass, controlled, freezer, reply=False)
    await at(hass, freezer, schedule, monday(4))
    heater(hass, 60.0)  # the controller's reply arrives later
    # Simulate a crash: no unload restore, just a reload with the feature removed.
    schedule.stop = lambda: _noop()
    config = deepcopy(effective_config(entry))
    config.pop("dhw_schedule")
    hass.config_entries.async_update_entry(entry, data=config)
    await hass.async_block_till_done()
    schedule = entry.runtime_data.dhw
    schedule._mono = lambda: dt_util.utcnow().timestamp()
    assert schedule.status == "restoring"
    await at(hass, freezer, schedule, minutes=2)
    assert writes[-1]["setpoint"] == 50.0 and writes[-1]["entity_id"] == WH
    assert len([w for w in writes if w["setpoint"] == 60.0]) == 1


async def _noop():
    return None


async def test_unconfirmed_restore_raises_repair_and_clears(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer, reply=False)
    await at(hass, freezer, schedule, monday(4))
    heater(hass, 60.0)  # the raise is observed; later restores never are
    await at(hass, freezer, schedule, monday(6))
    for _ in range(4):
        await at(hass, freezer, schedule, minutes=6)
    assert 2 <= len([w for w in writes if w["setpoint"] == 50.0]) <= 4
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "dhw_schedule_restore")
    assert issue is not None and schedule.status == "recovery_pending"
    heater(hass, 50.0)
    await at(hass, freezer, schedule, minutes=2)
    assert ir.async_get(hass).async_get_issue(DOMAIN, "dhw_schedule_restore") is None
    assert schedule.status == "armed"


async def test_conflicting_automation_blocks_raise(hass, controlled, sources, freezer):  # noqa: F811
    registered = er.async_get(hass).async_get_or_create(
        "water_heater", "ramses_cc", "hw", suggested_object_id="stored_hw"
    )
    assert registered.entity_id == WH
    entry, schedule, writes = await begin(hass, controlled, freezer)
    automation = SimpleNamespace(
        is_on=True,
        entity_id="automation.boiler_temperature",
        raw_config={
            "actions": [
                {
                    "device_id": "d1",
                    "domain": "water_heater",
                    "type": "set_temperature",
                    "entity_id": registered.id,
                }
            ]
        },
    )
    hass.data["automation"] = SimpleNamespace(entities=[automation])
    controls = entry.runtime_data.controls
    assert controls.dhw_automation_conflicts(WH) == ["automation.boiler_temperature"]
    await at(hass, freezer, schedule, monday(4))
    assert writes == []
    assert "automation" in schedule.report()["blocked"]
    for service in ("ramses_cc.set_dhw_params", "ramses_cc.reset_dhw_params"):
        automation.raw_config = {"actions": [{"action": service}]}
        assert controls.dhw_automation_conflicts(WH)
    # On/off, mode and boost writers (e.g. a voice boost) leave the target alone.
    for action in (
        {"action": "light.turn_on", "target": {"entity_id": WH}},
        {"action": "water_heater.set_operation_mode", "target": {"entity_id": WH}},
        {"action": "water_heater.turn_on", "target": {"entity_id": WH}},
        {"action": "ramses_cc.set_dhw_mode", "data": {"mode": "permanent_override"}},
        {"action": "ramses_cc.set_dhw_boost", "target": {"entity_id": WH}},
        {"action": "ramses_cc.reset_dhw_mode"},
        {"domain": "water_heater", "type": "turn_off", "entity_id": registered.id},
    ):
        automation.raw_config = {"actions": [action]}
        assert controls.dhw_automation_conflicts(WH) == [], action
    # Room and boiler guards are unaffected by DHW-only writers.
    automation.raw_config = {"actions": [{"action": "ramses_cc.set_dhw_params"}]}
    assert controls.automation_conflicts("boiler") == []


async def test_boost_at_window_start_delays_raise(hass, controlled, sources, freezer):  # noqa: F811
    _, schedule, writes = await begin(hass, controlled, freezer)
    heater(hass, mode="temporary_override")
    await at(hass, freezer, schedule, monday(4))
    assert writes == [] and schedule.status == "armed"
    heater(hass)
    await at(hass, freezer, schedule, monday(5))
    assert writes == [{"entity_id": WH, "setpoint": 60.0, "overrun": 2, "differential": 7.5}]
    assert schedule.status == "elevated"


async def test_boiler_uses_scheduled_water_heater_target(hass, controlled, sources, freezer):  # noqa: F811
    entry, schedule, _ = await begin(hass, controlled, freezer)
    boiler = entry.runtime_data.controls.boiler
    assert boiler._config["cylinder_target_entity"] == WH
    await boiler.async_refresh()
    assert boiler.data.cylinder_target == 50.0
    assert boiler.override == "shadow"
    await at(hass, freezer, schedule, monday(4))
    await boiler.async_refresh()
    assert any("not yet confirmed" in f for f in boiler.data.disabled_features)


async def test_options_flow_validates_and_rearms(hass, config, sources):
    heater(hass)
    entry = await setup(hass, config)

    async def submit(values):
        flow = await hass.config_entries.options.async_init(entry.entry_id)
        assert "dhw_schedule" in flow["menu_options"]
        flow = await hass.config_entries.options.async_configure(
            flow["flow_id"], {"next_step_id": "dhw_schedule"}
        )
        return await hass.config_entries.options.async_configure(flow["flow_id"], values)

    base = {
        "enabled": True,
        "water_heater_entity": WH,
        "demand_entity": "sensor.hw",
        "cylinder_temp_entity": "sensor.cylinder",
        "normal_target": 50,
        "high_target": 60,
        "weekdays": ["mon"],
        "window_start": "04:00:00",
        "window_end": "06:00:00",
    }
    assert (await submit({**base, "high_target": 50}))["errors"] == {"base": "dhw_invalid_targets"}
    assert (await submit({**base, "window_end": "03:00:00"}))["errors"] == {
        "base": "dhw_invalid_window"
    }
    hass.states.async_set("water_heater.other", "on", {})
    assert (await submit({**base, "water_heater_entity": "water_heater.other"}))["errors"] == {
        "base": "dhw_params_unavailable"
    }
    assert (await submit(base))["type"] == "create_entry"
    await hass.async_block_till_done()
    stored = effective_config(entry)
    first = stored["dhw_schedule"]["revision"]
    assert first and stored["rooms"] == config["rooms"]
    # An unrelated change keeps the revision; a new normal target re-arms.
    assert (await submit({**base, "weekdays": ["mon", "thu"]}))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["dhw_schedule"]["revision"] == first
    assert effective_config(entry)["dhw_schedule"]["weekdays"] == ["mon", "thu"]
    assert (await submit({**base, "normal_target": 48}))["type"] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["dhw_schedule"]["revision"] != first
    # Disabled with no weekdays and no entities is accepted.
    assert (await submit({"enabled": False, "normal_target": 50, "high_target": 60}))[
        "type"
    ] == "create_entry"


def test_policy_window_consumption_and_dst():
    cfg = dhw_config({"dhw_schedule": {"enabled": True, "weekdays": ["sun"]}})
    armed = datetime(2026, 10, 24, 12, tzinfo=dt_util.UTC)
    # 25 October 2026: clocks go back at 02:00 BST; 04:00 is GMT.
    opening = datetime(2026, 10, 25, 4, 0, tzinfo=LONDON)
    due = session_due(opening, cfg, LONDON, armed, None)
    assert due is not None and due[0].isoformat() == "2026-10-25"
    assert session_due(opening, cfg, LONDON, armed, "2026-10-25") is None
    assert (
        session_due(opening + timedelta(minutes=30), cfg, LONDON, opening + timedelta(1), None)
        is None
    )
    assert next_window(armed, cfg, LONDON) == opening
    assert next_window(armed, dhw_config({}), LONDON) is None
    assert validation_error({**cfg, "normal_target": 50.2}) == "dhw_invalid_targets"
    assert validation_error({**cfg, "water_heater_entity": None}) == "dhw_missing_entity"


def test_evidence_needs_charge_target_and_continuous_off():
    ev = Evidence()
    ev.observe(0, False, None, True, 60)
    assert ev.result(10_000) is None and ev.deadline_outcome() == "no_charge"
    ev.observe(10, True, 61, True, 60)
    ev.observe(20, False, None, True, 60)
    ev.observe(300, None, None, True, 60)  # stale demand resets the dwell
    ev.observe(310, False, None, True, 60)
    assert ev.result(900) is None
    assert ev.result(910) == "complete"


def test_rename_updates_dhw_section():
    config = with_dhw({"rooms": []})
    updated = update_source_references(config, WH, "water_heater.cylinder")
    assert updated["dhw_schedule"]["water_heater_entity"] == "water_heater.cylinder"
    assert config["dhw_schedule"]["water_heater_entity"] == WH


@pytest.fixture(autouse=True)
def _no_real_ramses():
    yield
