"""Energy status and per-meter daily kWh; totals are not savings."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import EntityCategory, UnitOfEnergy
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN, NAME, VERSION


def create_sensors(energy, entry):
    return [EnergyStatusSensor(energy, entry)] + [
        EnergyDailySensor(energy, entry, spec) for spec in energy.specs
    ]


class EnergySensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, energy, entry, key, placeholders=None):
        super().__init__(energy)
        self._attr_unique_id = f"{entry.entry_id}:system:{key}"
        self._attr_translation_placeholders = placeholders or {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="Heating observations and analytics",
            sw_version=VERSION,
        )


class EnergyStatusSensor(EnergySensor):
    _attr_translation_key = "energy_status"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, energy, entry):
        super().__init__(energy, entry, "energy_status")

    @property
    def native_value(self):
        return self.coordinator.status

    @property
    def extra_state_attributes(self):
        quality = self.coordinator.quality()
        return {
            k: quality[k]
            for k in (
                "meter_count",
                "coverage_percent_7d",
                "last_bucket_at",
                "reset_count",
                "allocation_unknown_share",
            )
        }


class EnergyDailySensor(EnergySensor):
    _attr_translation_key = "energy_daily_kwh"
    _attr_device_class = SensorDeviceClass.ENERGY
    _attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR
    _attr_state_class = SensorStateClass.TOTAL
    _attr_suggested_display_precision = 2

    def __init__(self, energy, entry, spec):
        super().__init__(energy, entry, f"energy_{spec['slug']}_daily_kwh", {"meter": spec["slug"]})
        self.slug = spec["slug"]
        self.kind = spec["kind"]

    @property
    def native_value(self):
        return self.coordinator.daily_kwh(self.slug)

    @property
    def extra_state_attributes(self):
        return {
            "meter_kind": self.kind,
            "definition": "Metered kWh since local midnight from ok/rollover deltas; reset intervals are excluded, never estimated.",
        }
