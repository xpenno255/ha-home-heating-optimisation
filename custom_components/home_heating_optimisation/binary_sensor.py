"""Observed heating and resolved DHW activity, with unknown preserved."""

from homeassistant.components.binary_sensor import BinarySensorEntity

from .entity import HeatingEntity

PARALLEL_UPDATES = 0


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(
        [
            HeatingBinarySensor(entry.runtime_data, entry, key)
            for key in ("heating_active", "dhw_active")
        ]
    )


class HeatingBinarySensor(HeatingEntity, BinarySensorEntity):
    @property
    def is_on(self):
        return self.reading.value
