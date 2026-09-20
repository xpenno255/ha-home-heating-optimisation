"""Bounded, validated journal persistence; corrupt files are preserved, never rewritten."""

import asyncio
import bisect
import logging
import math

from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from .const import MAX_EVENTS, RETENTION_SECONDS, SCHEMA_VERSION, STORE_NAME

LOGGER = logging.getLogger(__name__)
EVENT_FIELDS = ("schema", "id", "time", "kind", "room_id", "scope", "origin", "data", "provenance")


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_event(event):
    """Accept any historical kind string so old definitions are preserved on load."""
    if not isinstance(event, dict) or any(key not in event for key in EVENT_FIELDS):
        raise ValueError("invalid journal event")
    if not isinstance(event["schema"], int) or event["schema"] < 1:
        raise ValueError("invalid journal event schema")
    if not isinstance(event["id"], str) or not event["id"]:
        raise ValueError("invalid journal event id")
    if not finite(event["time"]):
        raise ValueError("invalid journal event time")
    if not isinstance(event["kind"], str) or not isinstance(event["origin"], str):
        raise ValueError("invalid journal event kind or origin")
    for key in ("room_id", "scope"):
        if event[key] is not None and not isinstance(event[key], str):
            raise ValueError("invalid journal event link")
    if not isinstance(event["data"], dict) or not isinstance(event["provenance"], dict):
        raise ValueError("invalid journal event payload")


class JournalStore:
    def __init__(self, hass, entry_id):
        self.hass = hass
        self.backend = Store(hass, 1, f"{DOMAIN}.{entry_id}.{STORE_NAME}")
        self.events = []
        self.status = "ready"
        self.truncated = False
        self.dirty = False
        self.lock = asyncio.Lock()

    async def load(self):
        try:
            data = await self.backend.async_load()
            if data is not None:
                if not isinstance(data, dict) or data.get("schema") != SCHEMA_VERSION:
                    raise ValueError("unsupported journal schema")
                events = data.get("events")
                if not isinstance(events, list):
                    raise ValueError("invalid journal collection")
                for event in events:
                    validate_event(event)
                self.events = sorted(events, key=lambda e: e["time"])
                self.truncated = bool(data.get("truncated", False))
                self.limit()
            self.status = "ready"
        except Exception:
            # Preserve the unreadable file for inspection; the journal stays read-only.
            self.events = []
            self.status = "storage_read_only"
            LOGGER.exception("Heating journal could not be loaded; preserving the existing file")

    def append(self, event):
        if self.events and event["time"] < self.events[-1]["time"]:
            index = bisect.bisect_right([e["time"] for e in self.events], event["time"])
            self.events.insert(index, event)
        else:
            self.events.append(event)
        self.dirty = True
        self.limit()

    def limit(self):
        if len(self.events) > MAX_EVENTS:
            self.events = self.events[-MAX_EVENTS:]
            self.truncated = True
            self.dirty = True

    def prune(self, now):
        cutoff = now - RETENTION_SECONDS
        kept = [e for e in self.events if e["time"] >= cutoff]
        if len(kept) != len(self.events):
            self.events = kept
            self.dirty = True

    async def save(self):
        if self.status == "storage_read_only":
            return
        async with self.lock:
            # Events are immutable after insertion; copy the list so concurrent
            # records cannot alter the payload while HA serialises it.
            data = {
                "schema": SCHEMA_VERSION,
                "events": list(self.events),
                "truncated": self.truncated,
            }
            try:
                await self.backend.async_save(data)
                self.status = "ready"
                self.dirty = False
            except Exception:
                self.status = "save_failed"
                LOGGER.exception("Heating journal save failed; retaining events in memory")
