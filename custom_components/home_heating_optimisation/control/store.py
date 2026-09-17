"""Fail-closed private persistence; never overwrite unreadable controller state."""

from copy import deepcopy

from homeassistant.helpers.storage import Store


class ControlStore:
    def __init__(self, hass, key, seed=None):
        self.backend = Store(hass, 1, f"home_heating_optimisation.control.{key}")
        self._data = deepcopy(seed or {})
        self.ready = False

    async def async_load(self):
        data = await self.backend.async_load()
        if data is not None:
            if not isinstance(data, dict):
                raise ValueError("Invalid control store")
            self._data = data
        self.ready = True
        return self._data

    async def async_save(self):
        if not self.ready:
            raise ValueError("Control storage is not writable")
        try:
            await self.backend.async_save(deepcopy(self._data))
        except Exception:
            self.ready = False
            raise

    def get(self, key, default=None):
        return self._data.get(key, default)

    def set(self, key, value):
        self._data[key] = value
