"""Home Heating Optimisation: observation-only foundation."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er

from .analytics.coordinator import AnalyticsCoordinator
from .coordinator import HeatingCoordinator
from .services import async_register_services

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR]
type HeatingEntry = ConfigEntry[HeatingCoordinator]


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    async_register_services(hass)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: HeatingEntry) -> bool:
    coordinator = HeatingCoordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_config_entry_first_refresh()
    if coordinator.config.get("analytics_enabled", False):
        coordinator.analytics = AnalyticsCoordinator(hass, entry, coordinator.config)
        await coordinator.analytics.initialise()
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
    if coordinator.analytics:
        coordinator.analytics.start()
    entry.async_on_unload(entry.add_update_listener(async_reload))
    return True


async def async_reload(hass: HomeAssistant, entry: HeatingEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: HeatingEntry) -> bool:
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and entry.runtime_data.analytics:
        await entry.runtime_data.analytics.stop()
    return unloaded
