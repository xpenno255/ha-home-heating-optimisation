"""Optional history and AI failures must not stop either actuator controller."""

from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError

from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled
from tests.control.test_runtime import start
from tests.test_advisor import MODULE, profile


async def test_failed_recorder_and_ai_do_not_stop_control(hass, controlled, sources):
    ai_entity = profile(hass)
    controlled["analytics_enabled"] = True
    controlled["advisor"] = {"enabled": True, "investigation": ai_entity}
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        side_effect=RuntimeError("Recorder unavailable"),
    ):
        entry, controls, calls = await start(hass, controlled)
    assert entry.runtime_data.analytics.backfill_status == "failed"
    with patch(f"{MODULE}.generate", side_effect=TimeoutError):
        with pytest.raises(HomeAssistantError, match="timed out"):
            await entry.runtime_data.advisor.run("investigation")
    assert entry.runtime_data.advisor.status == "timeout"
    await handover(controls)
    await controls.set_mode("boiler", "auto")
    await controls.set_mode("study", "active")
    assert {kind for kind, _ in calls} == {"number", "ramses"}


async def test_history_write_failure_does_not_change_control_ownership(hass, controlled, sources):
    controlled["analytics_enabled"] = True
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        return_value=([], False, "complete"),
    ):
        entry, controls, calls = await start(hass, controlled)
    await handover(controls)
    history = entry.runtime_data.analytics
    with patch.object(history.store.backend, "async_save", side_effect=OSError("disk full")):
        # History retains observations in memory, with control persistence unaffected.
        await history.refresh()
    assert history.store.status == "save_failed"
    assert controls.settings.get("ownership") == "ready"
    await controls.set_mode("boiler", "auto")
    await controls.set_mode("study", "active")
    assert {kind for kind, _ in calls} == {"number", "ramses"}
