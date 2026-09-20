"""Synchronous, never-raising event recording with debounced private persistence."""

import hashlib
import json
import logging
import math
import uuid
from collections import Counter
from copy import deepcopy
from datetime import datetime
from enum import Enum

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import HassJob, callback
from homeassistant.helpers.event import async_call_later
from homeassistant.util import dt as dt_util

from ..const import VERSION
from ..control.configuration import actuator_fingerprint
from .const import KINDS, ORIGINS, PRIVATE_KEYS, SAVE_DELAY_SECONDS, SCHEMA_VERSION, UNKNOWN
from .store import JournalStore

LOGGER = logging.getLogger(__name__)


def config_era(config):
    """Stable identity of the actuator bindings and room set, without entity names."""
    control = config.get("control") or {}
    rooms = sorted(str(r.get("id")) for r in config.get("rooms", []))
    payload = json.dumps([actuator_fingerprint(control), rooms], sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def json_safe(value):
    """Coerce to JSON-serialisable values; non-finite numbers become None."""
    if isinstance(value, bool) or value is None or isinstance(value, str):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Enum):
        return json_safe(value.value)
    if isinstance(value, dict):
        return {str(k): json_safe(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [json_safe(v) for v in value]
    return str(value)


def strip_private(value):
    if isinstance(value, dict):
        return {k: strip_private(v) for k, v in value.items() if k not in PRIVATE_KEYS}
    if isinstance(value, list):
        return [strip_private(v) for v in value]
    return value


class Journal:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.store = JournalStore(hass, entry.entry_id)
        self.enabled = bool(heating.config.get("journal_enabled", True))
        self.closed = False
        self._save_cancel = None
        self._era = config_era(heating.config)
        self._control_schema = (heating.config.get("control") or {}).get("schema")

    async def initialise(self):
        if not self.enabled:
            return
        await self.store.load()
        self.entry.async_on_unload(
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop)
        )

    @property
    def status(self):
        if not self.enabled:
            return "disabled"
        return self.store.status

    def defaults(self):
        return {
            "controller_version": VERSION,
            "control_schema": self._control_schema,
            "config_era": self._era,
            "model_version": None,
        }

    def record(
        self, kind, room_id=None, scope=None, origin="controller", data=None, provenance=None
    ):
        """Append one event. Synchronous, never raises; None when nothing was recorded."""
        try:
            if not self.enabled or self.closed or self.store.status == "storage_read_only":
                return None
            if kind not in KINDS:
                LOGGER.warning("Heating journal ignored unknown event kind %r", kind)
                return None
            payload = json_safe(data if isinstance(data, dict) else {})
            payload.setdefault("outcome", UNKNOWN)
            meta = self.defaults()
            if isinstance(provenance, dict):
                meta.update({k: v for k, v in json_safe(provenance).items() if v is not None})
            event = {
                "schema": SCHEMA_VERSION,
                "id": uuid.uuid4().hex,
                "time": dt_util.utcnow().timestamp(),
                "kind": kind,
                "room_id": str(room_id) if room_id is not None else None,
                "scope": str(scope) if scope is not None else None,
                "origin": origin if origin in ORIGINS else UNKNOWN,
                "data": payload,
                "provenance": meta,
            }
            self.store.append(event)
            self._schedule_save()
            return event
        except Exception:  # noqa: BLE001
            LOGGER.exception("Heating journal record failed; control continues")
            return None

    def events(self, kinds=None, room_id=None, since=None, until=None, limit=None):
        selected = [
            e
            for e in self.store.events
            if (kinds is None or e["kind"] in kinds)
            and (room_id is None or e["room_id"] == room_id)
            and (since is None or e["time"] >= since)
            and (until is None or e["time"] <= until)
        ]
        if limit is not None:
            selected = selected[-int(limit) :] if limit > 0 else []
        return deepcopy(selected)

    def export(self, include_private=False, **filters):
        events = self.events(**filters)
        return events if include_private else [strip_private(e) for e in events]

    def counts_by_kind(self):
        return dict(sorted(Counter(e["kind"] for e in self.store.events).items()))

    def summary(self):
        """Counts and status only: safe for diagnostics downloads."""
        return {
            "status": self.status,
            "event_count": len(self.store.events),
            "counts_by_kind": self.counts_by_kind(),
        }

    def attributes(self):
        events = self.store.events

        def iso(stamp):
            return datetime.fromtimestamp(stamp, dt_util.UTC).isoformat()

        return {
            "event_count": len(events),
            "oldest_at": iso(events[0]["time"]) if events else None,
            "newest_at": iso(events[-1]["time"]) if events else None,
            "counts_by_kind": self.counts_by_kind(),
            "truncated": self.store.truncated,
        }

    @callback
    def _schedule_save(self):
        if self._save_cancel is not None or self.closed:
            return
        self._save_cancel = async_call_later(
            self.hass, SAVE_DELAY_SECONDS, HassJob(self._flush, cancel_on_shutdown=True)
        )

    async def _flush(self, _now=None):
        self._save_cancel = None
        self.store.prune(dt_util.utcnow().timestamp())
        if self.store.dirty:
            await self.store.save()

    async def flush(self):
        """Persist now; used by tests and shutdown."""
        if self._save_cancel is not None:
            self._save_cancel()
            self._save_cancel = None
        await self._flush()

    async def stop(self, _event=None):
        if self.closed:
            return
        self.closed = True
        if self.enabled:
            await self.flush()
