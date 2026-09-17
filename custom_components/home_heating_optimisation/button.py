"""Reset diagnostic hot-water charge monitoring."""

from homeassistant.components.button import ButtonEntity

from .control.entities import ControlEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.controls
    if c.boiler:
        async_add_entities([ResetDHW(c, "boiler", "reset_dhw_diagnostics")])


class ResetDHW(ControlEntity, ButtonEntity):
    async def async_press(self):
        await self.coordinator.async_reset_dhw_cycling()
