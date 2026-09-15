"""Measured temperatures, commanded targets and explicitly labelled diagnostics."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature

from .const import SYSTEM_SOURCES
from .entity import HeatingEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = entry.runtime_data
    entities = [
        HeatingSensor(coordinator, entry, k)
        for k in (
            "operating_state",
            "input_availability",
            *(k for k, spec in SYSTEM_SOURCES.items() if spec.kind == "temperature"),
        )
    ]
    for room in coordinator.config["rooms"]:
        entities.extend(
            HeatingSensor(coordinator, entry, k, room)
            for k in ("air", "target", "demand", "deficit")
        )
    if coordinator.analytics:
        from .analytics.sensor import create_sensors

        entities.extend(create_sensors(coordinator.analytics, entry))
    async_add_entities(entities)


class HeatingSensor(HeatingEntity, SensorEntity):
    def __init__(self, coordinator, entry, key, room=None):
        super().__init__(coordinator, entry, key, room)
        if key == "operating_state":
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = ["heating", "dhw", "mixed", "idle"]
        elif key in ("input_availability", "demand"):
            self._attr_native_unit_of_measurement = PERCENTAGE
            self._attr_state_class = SensorStateClass.MEASUREMENT
        elif key == "deficit":
            self._attr_native_unit_of_measurement = UnitOfTemperature.KELVIN
            self._attr_state_class = SensorStateClass.MEASUREMENT
        else:
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS
            self._attr_state_class = SensorStateClass.MEASUREMENT
        if key == "input_availability":
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @property
    def native_value(self):
        if self.key == "operating_state":
            value = self.coordinator.data.operating_state
            return None if value == "unknown" else value
        if self.key == "input_availability":
            return self.coordinator.data.input_availability
        if self.key == "deficit":
            return self.room.deficit
        value = self.reading.value
        return round(value * 100, 2) if value is not None and self.key == "demand" else value
