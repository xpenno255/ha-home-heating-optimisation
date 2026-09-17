"""Independent comfort and boiler mode selection."""

from .control.entities import ControlMode

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    controls = entry.runtime_data.controls
    if controls.boiler:
        async_add_entities([ControlMode(controls, scope) for scope in ["boiler", *controls.rooms]])
