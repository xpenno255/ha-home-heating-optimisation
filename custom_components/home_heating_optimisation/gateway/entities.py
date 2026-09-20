"""Diagnostic sensor per monitored gateway; state names are allowlisted."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.util import dt as dt_util

from ..const import DOMAIN, NAME, VERSION
from .monitor import STATES


def sensors(monitor):
    return [GatewaySensor(monitor, state) for state in monitor.states.values()]


class GatewaySensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = list(STATES)
    _attr_translation_key = "gateway"

    def __init__(self, monitor, state):
        self.monitor, self.gateway = monitor, state
        self.entity_id = f"sensor.{DOMAIN}_gateway_{state.slug}"
        self._attr_unique_id = f"{monitor.entry.entry_id}:gateway:{state.slug}"
        self._attr_translation_placeholders = {"gateway": state.slug}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, monitor.entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            sw_version=VERSION,
            model="Heating observations and analytics",
        )

    async def async_added_to_hass(self):
        self.monitor.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.monitor.listeners.remove(self.async_write_ha_state))

    @property
    def native_value(self):
        return self.gateway.status if self.monitor.enabled else "unconfigured"

    @property
    def extra_state_attributes(self):
        report = self.gateway.report(dt_util.utcnow())
        report.pop("status", None)
        report["threshold_minutes"] = int(self.monitor.threshold.total_seconds() // 60)
        report["meaning"] = "online entity state only; not proof of radio delivery"
        return report
