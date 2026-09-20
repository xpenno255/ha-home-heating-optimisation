"""Consolidated comfort and boiler control, observations and optional advice."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers import entity_registry as er

from .advisor.coordinator import Advisor
from .analytics.coordinator import AnalyticsCoordinator
from .const import DOMAIN
from .control.runtime import Controls
from .coordinator import HeatingCoordinator
from .observations import watched_entities
from .services import async_register_services
from .source_identity import async_register_source_identity
from .trials.coordinator import Trials

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)
PLATFORMS = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SELECT,
    Platform.NUMBER,
    Platform.SWITCH,
    Platform.BUTTON,
]
type HeatingEntry = ConfigEntry[HeatingCoordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_register_services(hass)
    async_register_source_identity(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HeatingEntry) -> bool:
    coordinator = HeatingCoordinator(hass, entry)
    entry.runtime_data = coordinator
    coordinator.controls = Controls(hass, entry, coordinator)
    coordinator.controls.wire_config()
    entry.async_on_unload(coordinator.controls.stop)
    coordinator.sources = watched_entities(coordinator.config)
    await coordinator.telemetry.start()
    entry.async_on_unload(coordinator.telemetry.stop)
    await coordinator.controls.initialise()
    # Any trial persisted as running is rolled back here; a restart never resumes one.
    coordinator.trials = Trials(hass, entry, coordinator)
    await coordinator.trials.initialise()
    await coordinator.async_config_entry_first_refresh()
    await coordinator.load_house()
    if coordinator.config.get("analytics_enabled", False):
        coordinator.analytics = AnalyticsCoordinator(
            hass, entry, coordinator.config, coordinator.telemetry.get
        )
        await coordinator.analytics.initialise()
    coordinator.advisor = Advisor(hass, entry, coordinator)
    await coordinator.advisor.initialise()
    # Remove only our entities for explicitly removed rooms, retaining all others' IDs.
    registry = er.async_get(hass)
    valid_rooms = {room["id"] for room in coordinator.config["rooms"]}
    prefix = f"{entry.entry_id}:room:"
    for entity in er.async_entries_for_config_entry(registry, entry.entry_id):
        if entity.unique_id.startswith(prefix):
            room_id = entity.unique_id[len(prefix) :].split(":", 1)[0]
            if room_id not in valid_rooms:
                registry.async_remove(entity.entity_id)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    coordinator.start(entry)
    coordinator.telemetry.listeners.append(lambda: coordinator.changed(None))
    await coordinator.controls.start()
    coordinator.trials.start()
    coordinator.advisor.start()
    if coordinator.analytics:
        coordinator.analytics.start()
    entry.async_on_unload(entry.add_update_listener(async_reload))
    return True


async def async_reload(hass: HomeAssistant, entry: HeatingEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HeatingEntry) -> bool:
    # Roll running trials back to baseline while the controllers can still be written.
    if entry.runtime_data.trials:
        await entry.runtime_data.trials.stop()
    await entry.runtime_data.controls.stop()
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.advisor:
        await entry.runtime_data.advisor.stop()
    if unloaded and entry.runtime_data.analytics:
        await entry.runtime_data.analytics.stop()
    return unloaded
