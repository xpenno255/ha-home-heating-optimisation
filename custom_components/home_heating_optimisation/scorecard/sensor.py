"""Yesterday's heating gas per degree-day; the recent days ride along unrecorded."""

from homeassistant.components.sensor import SensorEntity, SensorStateClass
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import DOMAIN, NAME, VERSION

ENTITY_ID = f"sensor.{DOMAIN}_daily_scorecard"
RECORDED = (
    "date",
    "coverage",
    "outdoor_c",
    "indoor_c",
    "degree_days",
    "gas_kwh",
    "gas_heating_kwh",
    "gas_dhw_kwh",
    "comfort_within",
    "short_degree_hours",
    "writes",
)


class ScorecardSensor(SensorEntity):
    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:clipboard-text-clock"
    _attr_name = "Daily scorecard"
    _attr_native_unit_of_measurement = "kWh/°C·d"
    _attr_state_class = SensorStateClass.MEASUREMENT
    # The day lists are kept in the scorecard's own store; no need to record them again.
    _unrecorded_attributes = frozenset({"today", "days"})

    def __init__(self, scorecard):
        self.scorecard = scorecard
        entry_id = scorecard.entry.entry_id
        self.entity_id = ENTITY_ID
        self._attr_unique_id = f"{entry_id}:system:daily_scorecard"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry_id)}, name=NAME, sw_version=VERSION
        )

    async def async_added_to_hass(self):
        self.scorecard.listeners.append(self.async_write_ha_state)
        self.async_on_remove(lambda: self.scorecard.listeners.remove(self.async_write_ha_state))

    @property
    def native_value(self):
        latest = self.scorecard.latest()
        return latest.get("heating_kwh_per_degree_day") if latest else None

    @property
    def extra_state_attributes(self):
        latest = self.scorecard.latest() or {}
        burner = latest.get("burner") or {}
        return {
            **{key: latest.get(key) for key in RECORDED},
            "burner_starts": burner.get("starts"),
            **self.scorecard.sensor_view(),
        }
