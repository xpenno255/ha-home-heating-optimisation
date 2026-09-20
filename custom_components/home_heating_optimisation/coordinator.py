"""Shared live observations with timed expiry and no device service calls."""

import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from .const import DOMAIN, effective_config
from .observations import make_snapshot, watched_entities
from .survey import async_load_survey, evidence
from .telemetry import Telemetry

LOGGER = logging.getLogger(__name__)


class HeatingCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, LOGGER, name=DOMAIN, config_entry=entry)
        self.controls = None
        self.analytics = None
        self.advisor = None
        self.gateways = None
        self.survey = {"status": "not_configured", "rooms": {}, "bindings": {}, "warnings": []}
        self.config = effective_config(entry)
        self.telemetry = Telemetry(hass, self.config.get("mqtt_sources", []))
        self.sources = watched_entities(self.config)

    async def load_house(self):
        self.survey = await async_load_survey(self.hass, self.config)
        if self.data is not None:
            self.async_set_updated_data(self.data)

    def house_report(self):
        return evidence(self.survey, self.config)

    def snapshot(self):
        return make_snapshot(
            {e: s for e in self.sources if (s := self.telemetry.get(e)) is not None},
            self.config,
            dt_util.utcnow(),
            self.hass.config.units.temperature_unit,
        )

    async def _async_update_data(self):
        return self.snapshot()

    @callback
    def start(self, entry):
        """Listen to source changes; expire silent sources even without new events."""
        entry.async_on_unload(async_track_state_change_event(self.hass, self.sources, self.changed))
        entry.async_on_unload(
            async_track_time_interval(self.hass, self.changed, timedelta(seconds=30))
        )

    @callback
    def changed(self, _event):
        self.async_set_updated_data(self.snapshot())
