"""Diagnostic status of the DHW target schedule; state names are allowlisted."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import DOMAIN, NAME, VERSION
from .coordinator import STATES
from .cutoff import STATES as CUTOFF_STATES


def _device(entry_id):
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=NAME,
        manufacturer=NAME,
        sw_version=VERSION,
        model="Heating observations and analytics",
    )


class DhwScheduleSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATES)
    _attr_translation_key = "dhw_schedule"

    def __init__(self, schedule):
        self.schedule = schedule
        entry_id = schedule.entry.entry_id
        self.entity_id = f"sensor.{DOMAIN}_dhw_schedule"
        self._attr_unique_id = f"{entry_id}:system:dhw_schedule"
        self._attr_device_info = _device(entry_id)

    async def async_added_to_hass(self):
        self.schedule.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.schedule.listeners.remove(self.async_write_ha_state))

    @property
    def native_value(self):
        return self.schedule.status

    @property
    def extra_state_attributes(self):
        report = self.schedule.report()
        report.pop("status", None)
        return report


class DhwCutoffSensor(DhwScheduleSensor):
    _attr_options = list(CUTOFF_STATES)
    _attr_translation_key = "dhw_cutoff"

    def __init__(self, cutoff):
        self.schedule = cutoff
        entry_id = cutoff.entry.entry_id
        self.entity_id = f"sensor.{DOMAIN}_dhw_cutoff"
        self._attr_unique_id = f"{entry_id}:system:dhw_cutoff"
        self._attr_device_info = _device(entry_id)
