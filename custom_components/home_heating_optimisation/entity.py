"""Stable identities and common observation attributes."""

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NAME, VERSION


class HeatingEntity(CoordinatorEntity):
    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key, room=None):
        super().__init__(coordinator)
        self.key = key
        self.room_id = room["id"] if room else None
        suffix = f"room:{self.room_id}:{key}" if room else f"system:{key}"
        self._attr_unique_id = f"{entry.entry_id}:{suffix}"
        self._attr_translation_key = key
        self._attr_translation_placeholders = {"room": room["name"]} if room else {}
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=NAME,
            manufacturer="Home Heating Optimisation",
            model="Heating observations and analytics",
            sw_version=VERSION,
        )

    @property
    def room(self):
        return next(r for r in self.coordinator.data.rooms if r.id == self.room_id)

    @property
    def reading(self):
        if self.room_id:
            return getattr(self.room, self.key) if self.key != "deficit" else None
        return self.coordinator.data.system.get(self.key)

    @property
    def extra_state_attributes(self):
        reading = self.reading
        if reading is not None:
            return {
                "quality": reading.quality,
                "source_entity": reading.source,
                "source_reported_at": reading.reported_at.isoformat()
                if reading.reported_at
                else None,
            }
        if self.key == "deficit":
            return {
                "definition": "positive commanded air target minus measured air, when enabled",
                "air_quality": self.room.air.quality,
                "target_quality": self.room.target.quality,
                "room_enabled": self.room.enabled,
            }
        if self.key == "operating_state":
            return {
                k: self.coordinator.data.system[k].quality for k in ("heating_active", "dhw_active")
            }
        return {"definition": "currently valid inputs / configured inputs; not time coverage"}
