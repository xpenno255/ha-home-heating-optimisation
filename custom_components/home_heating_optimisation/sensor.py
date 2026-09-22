"""Measured temperatures, commanded targets and explicitly labelled diagnostics."""

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.const import PERCENTAGE, EntityCategory, UnitOfTemperature
from homeassistant.util import dt as dt_util

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
    entities.append(AdvisorSensor(coordinator, entry))
    if coordinator.energy:
        from .energy.sensor import create_sensors as energy_sensors

        entities.extend(energy_sensors(coordinator.energy, entry))
    entities.append(AdvisorLatestReportSensor(coordinator, entry))
    entities.append(RecommendationsSensor(coordinator, entry))
    entities.append(TrialsSensor(coordinator, entry))
    entities.append(HouseModelSensor(coordinator, entry))
    entities.append(JournalStatusSensor(coordinator, entry))
    from .control.entities import sensors

    entities.extend(sensors(coordinator.controls))
    if coordinator.gateways is not None:
        from .gateway.entities import sensors as gateway_sensors

        entities.extend(gateway_sensors(coordinator.gateways))
    if coordinator.dhw is not None:
        from .dhw.sensor import DhwScheduleSensor

        entities.append(DhwScheduleSensor(coordinator.dhw))
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


class HouseModelSensor(HeatingEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "house_model")

    @property
    def native_value(self):
        return self.coordinator.house_report()["status"]

    @property
    def extra_state_attributes(self):
        report = self.coordinator.house_report()
        return {
            "survey_room_count": len(report["rooms"]),
            "mapped_room_count": report["mapped_room_count"],
            "unmapped_configured_room_count": report["unmapped_configured_room_count"],
            "warning_count": len(report["warnings"]),
            "advisory_count": len(report.get("advisories", [])),
            "error_code": report.get("error_code"),
        }


class AdvisorSensor(HeatingEntity, SensorEntity):
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "advisor")

    @property
    def native_value(self):
        return self.coordinator.advisor.status

    @property
    def extra_state_attributes(self):
        return self.coordinator.advisor.quality()


class JournalStatusSensor(HeatingEntity, SensorEntity):
    """Journal health and counts only; event content stays in private storage."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["ready", "storage_read_only", "save_failed", "disabled"]

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "journal_status")

    @property
    def native_value(self):
        journal = self.coordinator.journal
        return journal.status if journal else "disabled"

    @property
    def extra_state_attributes(self):
        journal = self.coordinator.journal
        if journal is None or not journal.enabled:
            return {"event_count": 0, "oldest_at": None, "newest_at": None, "counts_by_kind": {}}
        return journal.attributes()


class AdvisorLatestReportSensor(HeatingEntity, SensorEntity):
    """Creation time of the latest retained report; identifiers and counts only."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_device_class = SensorDeviceClass.TIMESTAMP

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "advisor_latest_report")

    @property
    def native_value(self):
        report = self.coordinator.advisor.latest()
        return dt_util.parse_datetime(report["created_at"]) if report else None

    @property
    def extra_state_attributes(self):
        return self.coordinator.advisor.latest_attributes()


class RecommendationsSensor(HeatingEntity, SensorEntity):
    """Counts only; recommendation text stays in the private store and read service."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "recommendations")

    @property
    def native_value(self):
        return self.coordinator.advisor.recommendations.quality()["counts"]["proposed"]

    @property
    def extra_state_attributes(self):
        quality = self.coordinator.advisor.recommendations.quality()
        return {
            "status": quality["status"],
            **{f"{state}_count": n for state, n in quality["counts"].items()},
            "latest_id": quality["latest_id"],
            "eligible_for_evaluation": quality["eligible_for_evaluation"],
            "evidence_type": "association",
        }


class TrialsSensor(HeatingEntity, SensorEntity):
    """Running-trial count; rationale and notes stay in the private store and read service."""

    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_state_class = SensorStateClass.MEASUREMENT

    def __init__(self, coordinator, entry):
        super().__init__(coordinator, entry, "trials")

    def _quality(self):
        trials = self.coordinator.trials
        return trials.quality() if trials else None

    @property
    def native_value(self):
        quality = self._quality()
        return quality["counts"]["running"] if quality else 0

    @property
    def extra_state_attributes(self):
        quality = self._quality()
        if quality is None:
            return {"status": "not_configured"}
        return {
            "status": quality["status"],
            **{f"{state}_count": n for state, n in quality["counts"].items()},
            "running_scope": quality["running_scope"],
            "running_parameter": quality["running_parameter"],
            "expires_at": quality["expires_at"],
            "evidence_type": "association",
        }
