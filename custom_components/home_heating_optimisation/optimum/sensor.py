"""Shadow optimum start / stop sensors: predictions only, nothing is ever applied."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import slugify

from ..const import DOMAIN, NAME, VERSION
from .coordinator import STATES


def _device(entry_id):
    return DeviceInfo(
        identifiers={(DOMAIN, entry_id)},
        name=NAME,
        manufacturer=NAME,
        sw_version=VERSION,
        model="Heating observations and analytics",
    )


class OptimumSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, optimum):
        self.optimum = optimum

    async def async_added_to_hass(self):
        self.optimum.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.optimum.listeners.remove(self.async_write_ha_state))


class OptimumStartSensor(OptimumSensor):
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATES)
    _attr_translation_key = "optimum_start"

    def __init__(self, optimum):
        super().__init__(optimum)
        entry_id = optimum.entry.entry_id
        self.entity_id = f"sensor.{DOMAIN}_optimum_start"
        self._attr_unique_id = f"{entry_id}:system:optimum_start"
        self._attr_device_info = _device(entry_id)

    @property
    def native_value(self):
        return self.optimum.status

    @property
    def extra_state_attributes(self):
        report = self.optimum.report()
        report.pop("status", None)
        return report


class OptimumRoomSensor(OptimumSensor):
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, optimum, room_id, name, kind):
        super().__init__(optimum)
        self.room_id, self.kind = room_id, kind
        entry_id = optimum.entry.entry_id
        # Named like the observation sensors (…_kitchen_air_temperature); the id stays stable.
        self.entity_id = f"sensor.{DOMAIN}_{slugify(name)}_optimum_{kind}"
        self._attr_unique_id = f"{entry_id}:room:{room_id}:optimum_{kind}"
        self._attr_name = f"{name} optimum {kind}"
        self._attr_device_info = _device(entry_id)

    @property
    def native_value(self):
        start, stop, _ = self.optimum.room_view(self.room_id)
        return start if self.kind == "start" else stop

    @property
    def extra_state_attributes(self):
        return self.optimum.room_view(self.room_id)[2]
