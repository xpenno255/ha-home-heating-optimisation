"""Control diagnostics and independent modes, owned by the consolidated entry."""

from dataclasses import asdict
from datetime import datetime

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.components.select import SelectEntity
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN, NAME, VERSION
from .runtime import BOILER_SENSORS, ROOM_SENSORS


def _state_attributes(data):
    """Make command timing available to Recorder as stable, scalar provenance."""
    attrs = asdict(data)
    for key, value in attrs.items():
        if isinstance(value, datetime):
            attrs[key] = value.isoformat()
    return attrs


class ControlEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, controls, scope, key):
        c = controls.boiler if scope == "boiler" else controls.rooms[scope]
        super().__init__(c)
        self.controls, self.scope, self.key = controls, scope, key
        self._attr_unique_id = f"{controls.entry.entry_id}:control:{scope}:{key}"
        room = "Boiler" if scope == "boiler" else c.room_name
        self._attr_name = f"{room} control {key.replace('_', ' ')}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controls.entry.entry_id)},
            name=NAME,
            sw_version=VERSION,
            manufacturer=NAME,
            model="Heating control and analytics",
        )


class ControlSensor(ControlEntity, SensorEntity):
    def __init__(self, controls, scope, key, unit):
        super().__init__(controls, scope, key)
        self.entity_id = controls.registry_id(scope, key)
        self._attr_native_unit_of_measurement = unit
        if unit == "°C":
            self._attr_device_class = SensorDeviceClass.TEMPERATURE

    @property
    def native_value(self):
        return getattr(self.coordinator.data, self.key, None) if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        data = self.coordinator.data
        if not data:
            return {}
        if self.key in ("state", "mode", "flow_setpoint"):
            attrs = _state_attributes(data)
            attrs["activation_block"] = self.controls.guard_reason(self.scope)
            if self.scope != "boiler":
                attrs["write_status"] = self.coordinator.write_status
            return attrs
        if self.key == "air_temp":
            return {
                "source_entity": data.air_temp_source,
                "meaning": "room air selected for comfort control",
            }
        if self.key == "target_ot":
            return {
                "schedule_setpoint": data.schedule_setpoint,
                "schedule_source": data.schedule_source,
                "occupancy_offset": data.occupancy_offset,
            }
        return {}


class ControlDHW(ControlEntity, BinarySensorEntity):
    def __init__(self, controls):
        super().__init__(controls, "boiler", "dhw")
        self.entity_id = controls.registry_id("boiler", "dhw", "binary_sensor")

    @property
    def is_on(self):
        return self.coordinator.data.dhw_active if self.coordinator.data else None

    @property
    def extra_state_attributes(self):
        return {"source": self.coordinator.data.dhw_source} if self.coordinator.data else {}


class ControlMode(ControlEntity, SelectEntity):
    def __init__(self, controls, scope):
        super().__init__(controls, scope, "mode_selection")
        self._attr_options = (
            ["shadow", "auto", "hold"] if scope == "boiler" else ["shadow", "active"]
        )

    @property
    def current_option(self):
        return self.coordinator.override if self.scope == "boiler" else self.coordinator.mode

    async def async_select_option(self, option):
        await self.controls.set_mode(self.scope, option)
        self.async_write_ha_state()


def sensors(controls):
    if not controls.boiler:
        return []
    return [
        ControlSensor(controls, "boiler", key, unit) for key, unit in BOILER_SENSORS.items()
    ] + [
        ControlSensor(controls, rid, key, unit)
        for rid in controls.rooms
        for key, unit in ROOM_SENSORS.items()
    ]
