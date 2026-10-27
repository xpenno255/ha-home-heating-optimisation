"""Visitor mode controls: hold the higher stored hot water target until ended."""

from homeassistant.components.datetime import DateTimeEntity
from homeassistant.components.switch import SwitchEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .policy import usable
from .sensor import _device


class VisitorEntity:
    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, schedule, key):
        self.schedule = schedule
        entry_id = schedule.entry.entry_id
        self._attr_unique_id = f"{entry_id}:system:{key}"
        self._attr_device_info = _device(entry_id)

    async def async_added_to_hass(self):
        self.schedule.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.schedule.listeners.remove(self.async_write_ha_state))


class VisitorModeSwitch(VisitorEntity, SwitchEntity):
    _attr_translation_key = "visitor_mode"

    def __init__(self, schedule):
        super().__init__(schedule, "visitor_mode")
        self.entity_id = f"switch.{DOMAIN}_visitor_mode"

    @property
    def available(self):
        return usable(self.schedule.cfg)

    @property
    def is_on(self):
        return self.schedule.store.data["visitor"]["on"]

    async def async_turn_on(self, **kwargs):
        if not usable(self.schedule.cfg):
            raise HomeAssistantError(
                "Visitor mode needs the DHW target schedule enabled with its entities configured"
            )
        await self.schedule.set_visitor(True)

    async def async_turn_off(self, **kwargs):
        await self.schedule.set_visitor(False)


class VisitorUntilDateTime(VisitorEntity, DateTimeEntity):
    _attr_translation_key = "visitor_mode_until"

    def __init__(self, schedule):
        super().__init__(schedule, "visitor_mode_until")
        self.entity_id = f"datetime.{DOMAIN}_visitor_mode_until"

    @property
    def native_value(self):
        return self.schedule.visitor_until()

    async def async_set_value(self, value):
        if value <= dt_util.utcnow():
            raise HomeAssistantError("The visitor mode end time must be in the future")
        await self.schedule.set_visitor_until(value)
