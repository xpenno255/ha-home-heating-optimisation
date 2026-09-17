"""Persistent per-module enable and room occupancy settings."""

from homeassistant.components.switch import SwitchEntity

from .control.entities import ControlEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    c = entry.runtime_data.controls
    if c.boiler:
        async_add_entities([GlobalComfortSwitch(c, "boiler", "comfort_enabled")])
        async_add_entities(
            [
                ControlSwitch(c, scope, key)
                for scope in ["boiler", *c.rooms]
                for key in (["enabled"] if scope == "boiler" else ["enabled", "occupancy_enabled"])
            ]
        )


class ControlSwitch(ControlEntity, SwitchEntity):
    @property
    def is_on(self):
        return getattr(self.coordinator, self.key)

    async def change(self, value):
        async with self.controls.lock:
            flags = dict(self.controls.settings.get("flags", {}))
            flags[f"{self.scope}:{self.key}"] = value
            self.controls.settings.set("flags", flags)
            await self.controls.settings.async_save()
            setattr(self.coordinator, self.key, value)
            await self.coordinator.async_refresh()

    async def async_turn_on(self, **kwargs):
        await self.change(True)

    async def async_turn_off(self, **kwargs):
        await self.change(False)


class GlobalComfortSwitch(ControlEntity, SwitchEntity):
    @property
    def is_on(self):
        return self.controls.hub.global_enabled

    async def change(self, value):
        async with self.controls.lock:
            self.controls.settings.set("global_enabled", value)
            await self.controls.settings.async_save()
            self.controls.hub.global_enabled = value
            await self.controls.refresh()
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs):
        await self.change(True)

    async def async_turn_off(self, **kwargs):
        await self.change(False)
