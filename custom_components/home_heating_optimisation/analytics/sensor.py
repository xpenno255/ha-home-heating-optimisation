"""Coverage-gated historical sensors with stable room identities."""

from homeassistant.components.sensor import SensorEntity
from homeassistant.const import EntityCategory
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from ..const import DOMAIN, NAME, VERSION
from .const import MIN_COVERAGE

# key: (unit, required coverage channel). Window totals are not lifetime counters.
METRICS = {
    "coverage": ("%", None),
    "demand_coverage": ("%", None),
    "within_band": ("%", "coverage"),
    "duty_cycle": ("%", "demand_coverage"),
    "deficit_degree_hours": ("K·h", "coverage"),
    "overshoot_degree_hours": ("K·h", "coverage"),
    "heating_rate_avg": ("K/h", None),
    "time_to_setpoint_avg": ("min", None),
    "setpoint_achievement": ("%", None),
    "completed_recoveries": (None, None),
    "response_ratio": (None, None),
}


def create_sensors(coordinator, entry):
    return [
        AnalyticsSensor(coordinator, entry, key, room)
        for room in coordinator.config["rooms"]
        for key in METRICS
    ] + [
        AnalyticsSensor(coordinator, entry, key) for key in ("status", "adjustments", "comparison")
    ]


class AnalyticsSensor(CoordinatorEntity, SensorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, room=None):
        super().__init__(coordinator)
        self.key = key
        self.room_id = room["id"] if room else None
        suffix = f"room:{self.room_id}:analytics_{key}" if room else f"system:analytics_{key}"
        self._attr_unique_id = f"{entry.entry_id}:{suffix}"
        self._attr_translation_key = f"analytics_{key}"
        self._attr_translation_placeholders = {"room": room["name"]} if room else {}
        self._attr_native_unit_of_measurement = METRICS[key][0] if room else None
        self._attr_entity_category = EntityCategory.DIAGNOSTIC
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer=NAME,
            model="Heating observations and analytics",
            sw_version=VERSION,
        )

    @property
    def native_value(self):
        if self.room_id:
            stats = self.coordinator.data["analysis"]["zone_stats"][self.room_id]
            coverage = METRICS[self.key][1]
            return None if coverage and stats[coverage] < MIN_COVERAGE else stats[self.key]
        if self.key == "adjustments":
            return len(self.coordinator.store.adjustments)
        if self.key == "comparison":
            return self.coordinator.data["comparison"]["summary"]
        return self.coordinator.data["analysis"]["system"]["status"]

    @property
    def extra_state_attributes(self):
        result = self.coordinator.data["analysis"]
        attrs = {
            "window_start": result["window_start"],
            "window_end": result["window_end"],
            "freshness_basis": "last_updated",
        }
        if self.room_id:
            stats = result["zone_stats"][self.room_id]
            attrs.update(
                {
                    k: stats[k]
                    for k in (
                        "coverage",
                        "demand_coverage",
                        "observed_hours",
                        "completed_recoveries",
                        "cancelled_recoveries",
                        "ongoing_recoveries",
                    )
                }
            )
            if self.key == "response_ratio":
                attrs.update(
                    {
                        k: stats[k]
                        for k in (
                            "matched_pairs",
                            "matched_days",
                            "response_status",
                            "response_interval",
                        )
                    }
                )
        else:
            attrs.update(self.coordinator.quality())
        return attrs
