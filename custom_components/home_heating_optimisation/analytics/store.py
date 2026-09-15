"""Bounded private history, isolated from legacy integrations' storage."""

import asyncio
import logging
import math

from homeassistant.helpers.storage import Store

from ..const import DOMAIN
from .const import MAX_ADJUSTMENTS, MAX_POINTS, SCHEMA_VERSION, SEMANTICS_VERSION

LOGGER = logging.getLogger(__name__)


def source_signature(config, unit):
    """Only input/definition changes create a new history era; names do not."""
    return {
        "semantics": SEMANTICS_VERSION,
        "unit": unit,
        "rooms": sorted(
            (
                {k: v for k, v in r.items() if k != "name" and v is not None}
                for r in config["rooms"]
            ),
            key=lambda r: r["id"],
        ),
        "system": {
            k: v
            for k, v in config.items()
            if k
            not in (
                "rooms",
                "analytics_enabled",
                "analysis_window_days",
                "update_interval_minutes",
                "comfort_tolerance",
                "recovery_minutes",
            )
            and v is not None
        },
    }


def finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def validate_point(point):
    if not isinstance(point, dict) or not finite(point.get("time")):
        raise ValueError("invalid observation timestamp")
    if not isinstance(point.get("zones"), dict) or not isinstance(point.get("context"), dict):
        raise ValueError("invalid observation collections")
    for zone in point["zones"].values():
        if not isinstance(zone, dict) or not all(
            finite(zone.get(k)) for k in ("valid_until", "demand_valid_until")
        ):
            raise ValueError("invalid observation validity")
        if zone.get("active") is not None and type(zone["active"]) is not bool:
            raise ValueError("invalid demand activity")
        for key in ("temperature", "target", "demand", "temperature_updated"):
            if zone.get(key) is not None and not finite(zone[key]):
                raise ValueError("invalid observed number")
    for key, value in point["context"].items():
        if value is not None and not (
            type(value) is bool if key in ("heating_active", "dhw_active") else finite(value)
        ):
            raise ValueError("invalid context value")
    if not isinstance(point.get("context_valid_until", {}), dict) or not all(
        finite(v) for v in point.get("context_valid_until", {}).values()
    ):
        raise ValueError("invalid context validity")


class HistoryStore:
    def __init__(self, hass, entry_id):
        self.backend = Store(hass, 1, f"{DOMAIN}.{entry_id}.history")
        self.observations = []
        self.adjustments = []
        self.signature = None
        self.previous_era = None
        self.status = "ready"
        self.truncated = False
        self.lock = asyncio.Lock()

    async def load(self, signature):
        try:
            data = await self.backend.async_load()
            if data is not None:
                if data.get("schema") != SCHEMA_VERSION:
                    raise ValueError("unsupported history schema")
                observations = data["observations"]
                adjustments = data["adjustments"]
                if not isinstance(observations, list) or not isinstance(adjustments, list):
                    raise ValueError("invalid history collections")
                for point in observations:
                    validate_point(point)
                if any(a["time"] >= b["time"] for a, b in zip(observations, observations[1:])):
                    raise ValueError("history timestamps must increase")
                for note in adjustments:
                    if (
                        not isinstance(note, dict)
                        or not finite(note.get("time"))
                        or not isinstance(note.get("note"), str)
                    ):
                        raise ValueError("invalid adjustment")
                self.observations = observations[-MAX_POINTS:]
                self.adjustments = adjustments[-MAX_ADJUSTMENTS:]
                self.signature = data.get("signature")
                self.previous_era = data.get("previous_era")
                self.truncated = data.get("truncated", False)
            if self.signature != signature:
                if self.observations:
                    # Retain one bounded prior era for inspection, never analyse it as current.
                    self.previous_era = {
                        "signature": self.signature,
                        "observations": self.observations,
                    }
                self.observations = []
                self.signature = signature
        except Exception:
            # Preserve unreadable/unsupported files; memory-only collection remains useful.
            self.observations = []
            self.adjustments = []
            self.status = "storage_read_only"
            LOGGER.exception("Heating history could not be loaded; preserving the existing file")

    def merge(self, observations):
        merged = {p["time"]: p for p in observations}
        merged.update({p["time"]: p for p in self.observations})
        self.observations = [merged[t] for t in sorted(merged)]
        self.limit()

    def append(self, point):
        if self.observations and self.observations[-1]["time"] == point["time"]:
            self.observations[-1] = point
        else:
            self.observations.append(point)
        self.limit()

    def limit(self):
        if len(self.observations) > MAX_POINTS:
            self.observations = self.observations[-MAX_POINTS:]
            self.truncated = True

    def prune(self, now):
        cutoff = now - 15 * 86400
        before = [p for p in self.observations if p["time"] < cutoff]
        self.observations = before[-1:] + [p for p in self.observations if p["time"] >= cutoff]

    async def save(self):
        if self.status == "storage_read_only":
            return
        async with self.lock:
            # Points and note dictionaries are immutable after insertion. Copy lists
            # so concurrent capture cannot alter the payload while HA serialises it.
            data = {
                "schema": SCHEMA_VERSION,
                "signature": self.signature,
                "observations": list(self.observations),
                "adjustments": list(self.adjustments),
                "previous_era": self.previous_era,
                "truncated": self.truncated,
            }
            try:
                await self.backend.async_save(data)
                self.status = "ready"
            except Exception:
                self.status = "save_failed"
                LOGGER.exception("Heating history save failed; retaining observations in memory")
