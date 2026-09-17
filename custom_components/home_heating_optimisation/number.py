"""Imported comfort and boiler tuning controls."""

from homeassistant.components.number import NumberEntity, NumberMode
from homeassistant.const import EntityCategory

from .control.entities import ControlEntity

PARALLEL_UPDATES = 0
ROOM_NUMBERS = (
    ("trust_k", 0, 1, 0.05, None),
    ("cap_up", 0, 3, 0.5, "K"),
    ("cap_down", 0, 3, 0.5, "K"),
)
BOILER_NUMBERS = (
    ("design_flow", 30, 80, 1, "°C"),
    ("design_outdoor", -15, 10, 0.5, "°C"),
    ("return_ceiling", 30, 70, 1, "°C"),
    ("dhw_delta", 5, 40, 1, "K"),
)


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.controls
    if c.boiler:
        async_add_entities(
            [
                ControlNumber(c, scope, *spec)
                for scope in ["boiler", *c.rooms]
                for spec in (BOILER_NUMBERS if scope == "boiler" else ROOM_NUMBERS)
            ]
        )


class ControlNumber(ControlEntity, NumberEntity):
    _attr_mode = NumberMode.BOX
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, c, scope, key, minimum, maximum, step, unit):
        super().__init__(c, scope, key)
        self._attr_native_min_value = minimum
        self._attr_native_max_value = maximum
        self._attr_native_step = step
        self._attr_native_unit_of_measurement = unit

    @property
    def native_value(self):
        return self.coordinator.get_tunable(self.key)

    async def async_set_native_value(self, value):
        async with self.controls.lock:
            self.coordinator.set_tunable(self.key, value)
            await self.coordinator._store.async_save()
            await self.coordinator.async_refresh()
