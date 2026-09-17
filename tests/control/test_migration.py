"""Import provenance, independent copies and interrupted exclusive handover."""

import shutil
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.control.migration import (
    copy_survey,
    handover,
    preview,
)
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start


async def legacy_fixture(hass, c):
    rooms = []
    for domain, title, data, runtime, version in [
        ("ot_thermostat_control", "Hub", {"entry_type": "hub", "house_dir": "legacy"}, c.hub, 2),
        ("boiler_flow_control", "Boiler", c.boiler._config, c.boiler, 1),
        ("ot_thermostat_control", "Living", c.rooms["study"]._config, c.rooms["study"], 2),
    ]:
        e = MockConfigEntry(
            domain=domain,
            title=title,
            data=deepcopy(data),
            version=version,
            state=ConfigEntryState.LOADED,
        )
        e.runtime_data = runtime
        e.add_to_hass(hass)
        rooms.append(e)
    return rooms


async def test_preview_keeps_sensors_and_tunables_but_no_write_memory(hass, controlled, sources):
    entry, c, _ = await start(hass, controlled)
    legacy = await legacy_fixture(hass, c)
    old = deepcopy(controlled)
    old.pop("control")
    c.rooms["study"]._store.set("last_written_setpoint", 24)
    c.boiler.set_tunable("design_flow", 57)
    with patch(
        "custom_components.home_heating_optimisation.control.migration.effective_config",
        return_value=old,
    ):
        result = preview(hass, entry)
    imported = result["control"]
    assert imported["rooms"]["study"]["config"]["air_temp_sensor"] == "sensor.air"
    assert imported["boiler"]["seed"]["design_flow"] == 57
    assert "last_written_setpoint" not in imported["rooms"]["study"]["seed"]
    assert all(e.disabled_by is None for e in legacy)


async def test_preview_rejects_duplicate_and_unsupported_entries(hass, controlled, sources):
    entry, c, _ = await start(hass, controlled)
    legacy = await legacy_fixture(hass, c)
    old = deepcopy(controlled)
    old.pop("control")
    with patch(
        "custom_components.home_heating_optimisation.control.migration.effective_config",
        return_value=old,
    ):
        hass.config_entries.async_update_entry(legacy[1], version=9)
        with pytest.raises(ServiceValidationError, match="Unsupported"):
            preview(hass, entry)


async def test_interrupted_handover_is_never_ready(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    legacy = await legacy_fixture(hass, c)
    c.config["legacy_entries"] = [e.entry_id for e in legacy]

    async def fail(*args):
        raise RuntimeError("unload failed")

    with patch.object(hass.config_entries, "async_set_disabled_by", side_effect=fail):
        with pytest.raises(RuntimeError):
            await handover(c)
    assert c.settings.get("ownership") == "interrupted" and not c.can_write("boiler")
    assert calls == []


async def test_survey_copy_is_independent_idempotent_and_no_overwrite(hass, tmp_path, monkeypatch):
    monkeypatch.setattr(hass.config, "config_dir", str(tmp_path))
    source = Path(hass.config.path("legacy"))
    shutil.copytree(Path(__file__).parent / "fixtures", source, dirs_exist_ok=True)
    cfg = {"survey_directory": "legacy"}
    target = await hass.async_add_executor_job(copy_survey, hass, cfg)
    assert target == "home_heating_optimisation/house"
    assert await hass.async_add_executor_job(copy_survey, hass, cfg) == target
    original = (source / "house.yaml").read_bytes()
    (Path(hass.config.path(target)) / "house.yaml").write_text("changed")
    with pytest.raises(ServiceValidationError):
        await hass.async_add_executor_job(copy_survey, hass, cfg)
    assert (source / "house.yaml").read_bytes() == original


async def test_automation_writer_blocks_activation(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    hass.data["automation"] = SimpleNamespace(
        entities=[
            SimpleNamespace(
                entity_id="automation.other_writer",
                is_on=True,
                raw_config={
                    "actions": [
                        {
                            "action": "number.set_value",
                            "target": {"entity_id": "number.flow_setpoint"},
                            "data": {"value": 60},
                        }
                    ]
                },
            )
        ]
    )
    with pytest.raises(ServiceValidationError, match="automation"):
        await c.set_mode("boiler", "auto")
    assert calls == []
    hass.data.pop("automation")


async def test_idle_manual_boost_is_allowed_but_running_boost_blocks(hass, controlled, sources):
    _, c, calls = await start(hass, controlled)
    await handover(c)
    script = SimpleNamespace(
        entity_id="script.heating_boost",
        is_on=False,
        raw_config={
            "sequence": [
                {
                    "service": "climate.set_temperature",
                    "target": {"entity_id": "climate.study"},
                    "data": {"temperature": 22},
                }
            ]
        },
    )
    hass.data["script"] = SimpleNamespace(entities=[script])
    assert c.can_write("study")
    script.is_on = True
    assert not c.can_write("study")
    hass.data.pop("script")
