"""Freshness from actual MQTT payloads, independent of HA value deduplication."""

import json
import math

from homeassistant.core import State, callback
from homeassistant.util import dt as dt_util


class Telemetry:
    def __init__(self, hass, bindings):
        self.hass = hass
        self.bindings = {b["entity_id"]: b for b in bindings}
        self.received = {}
        self.unsubscribers = []
        self.listeners = []

    async def start(self):
        if not self.bindings:
            return
        from homeassistant.components import mqtt

        if not await mqtt.async_wait_for_mqtt_client(self.hass):
            raise ValueError("MQTT is unavailable")
        for topic in sorted({b["topic"] for b in self.bindings.values()}):
            self.unsubscribers.append(await mqtt.async_subscribe(self.hass, topic, self.message))

    @callback
    def message(self, message):
        # A retained replay is not evidence of a live measurement after restart.
        if message.retain:
            return
        try:
            payload = json.loads(message.payload)
        except ValueError, TypeError:
            return
        if not isinstance(payload, dict):
            return
        now = dt_util.utcnow()
        updated = False
        for entity, binding in self.bindings.items():
            if binding["topic"] != message.topic or binding["field"] not in payload:
                continue
            value = payload[binding["field"]]
            if binding["kind"] == "binary":
                if value not in ("on", "off"):
                    self.received.pop(entity, None)
                    continue
            else:
                try:
                    if isinstance(value, bool) or not math.isfinite(float(value)):
                        raise ValueError
                    value = str(float(value))
                except ValueError, TypeError:
                    self.received.pop(entity, None)
                    continue
            self.received[entity] = (value, now)
            updated = True
        if updated:
            for listener in self.listeners:
                listener()

    def get(self, entity_id):
        source = self.hass.states.get(entity_id)
        if source is None or entity_id not in self.bindings:
            return source
        if source.state in ("unavailable", "unknown"):
            return source
        if entity_id not in self.received:
            return State(entity_id, "unavailable", source.attributes)
        value, received = self.received[entity_id]
        return State(
            entity_id,
            str(value),
            source.attributes,
            last_changed=source.last_changed,
            last_updated=received,
            last_reported=received,
        )

    def stop(self):
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        self.listeners.clear()
