"""Five-minute metered-energy buckets with context; failures never touch control."""

import asyncio
import base64
import hashlib
import json
import logging
import zlib
from datetime import datetime, timedelta, timezone

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_utc_time_change
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..const import DOMAIN, SYSTEM_SOURCES
from ..control.configuration import actuator_fingerprint
from ..observations import read
from .analysis import comparability, group_days, since_periods, summarise_period
from .const import (
    ALLOCATIONS,
    BUCKET_SECONDS,
    INTERVENTION_KINDS,
    MAX_BUCKETS,
    MAX_DAYS,
    QUALITIES,
    SAVE_EVERY_BUCKETS,
    SCHEMA_VERSION,
)
from .meter import allocation, bucket_context, finite, meter_delta, meter_specs

LOGGER = logging.getLogger(__name__)
CONTEXT_KEYS = ("heating_active", "dhw_active", "outdoor_temperature")


def configuration_era(config):
    """Stable short hash of the actuator bindings; observation-only has one era."""
    control = config.get("control")
    if not control:
        return "observation"
    fingerprint = json.dumps(actuator_fingerprint(control), sort_keys=True, default=str)
    return hashlib.sha256(fingerprint.encode()).hexdigest()[:12]


def meter_signature(specs):
    return [{k: spec[k] for k in ("slug", "entity", "kind", "unit")} for spec in specs]


def validate_bucket(bucket):
    if not isinstance(bucket, dict) or not finite(bucket.get("time")):
        raise ValueError("invalid bucket time")
    if not isinstance(bucket.get("meters"), dict):
        raise ValueError("invalid bucket meters")
    for reading in bucket["meters"].values():
        if not isinstance(reading, dict) or reading.get("quality") not in QUALITIES:
            raise ValueError("invalid meter quality")
        if reading.get("kwh") is not None and not finite(reading["kwh"]):
            raise ValueError("invalid meter kwh")
    for key in ("heating_share", "dhw_share", "outdoor"):
        if bucket.get(key) is not None and not finite(bucket[key]):
            raise ValueError("invalid bucket context")
    if bucket.get("allocation") not in ALLOCATIONS:
        raise ValueError("invalid bucket allocation")
    if not isinstance(bucket.get("era"), str) or type(bucket.get("intervention")) is not bool:
        raise ValueError("invalid bucket provenance")


def pack(data):
    buckets = data.pop("buckets")
    data["buckets_zlib"] = base64.b64encode(
        zlib.compress(json.dumps(buckets, separators=(",", ":"), allow_nan=False).encode(), 3)
    ).decode()
    return data


def unpack(data):
    if "buckets_zlib" in data:
        decoder = zlib.decompressobj()
        raw = decoder.decompress(base64.b64decode(data["buckets_zlib"], validate=True), 64 << 20)
        if not decoder.eof or decoder.unconsumed_tail:
            raise ValueError("energy history exceeds decompression limit")
        data = {**data, "buckets": json.loads(raw)}
    return data


class EnergyStore:
    def __init__(self, hass, entry_id):
        self.hass = hass
        self.backend = Store(hass, 1, f"{DOMAIN}.{entry_id}.energy")
        self.buckets = []
        self.last_readings = {}
        self.signature = None
        self.reset_count = 0
        self.status = "ready"
        self.lock = asyncio.Lock()

    async def load(self, signature):
        try:
            data = await self.backend.async_load()
            if data is not None:
                if data.get("schema") != SCHEMA_VERSION:
                    raise ValueError("unsupported energy schema")
                data = await self.hass.async_add_executor_job(unpack, data)
                buckets = data.get("buckets")
                if not isinstance(buckets, list):
                    raise ValueError("invalid bucket collection")
                for bucket in buckets:
                    validate_bucket(bucket)
                if any(a["time"] >= b["time"] for a, b in zip(buckets, buckets[1:])):
                    raise ValueError("bucket times must increase")
                readings = data.get("last_readings", {})
                if not isinstance(readings, dict):
                    raise ValueError("invalid last readings")
                self.buckets = buckets[-MAX_BUCKETS:]
                self.last_readings = readings
                self.signature = data.get("signature")
                self.reset_count = int(data.get("reset_count", 0))
            if self.signature != signature:
                # A meter set change starts a new record; old buckets are not comparable.
                self.buckets = []
                self.last_readings = {}
                self.reset_count = 0
                self.signature = signature
        except Exception:
            self.buckets = []
            self.last_readings = {}
            self.status = "storage_read_only"
            LOGGER.exception("Energy history could not be loaded; preserving the existing file")

    def append(self, bucket):
        if self.buckets and self.buckets[-1]["time"] == bucket["time"]:
            self.buckets[-1] = bucket
        else:
            self.buckets.append(bucket)
        self.buckets = self.buckets[-MAX_BUCKETS:]

    def prune(self, now):
        cutoff = now - MAX_DAYS * 86400
        self.buckets = [b for b in self.buckets if b["time"] >= cutoff]

    async def save(self):
        if self.status == "storage_read_only":
            return
        async with self.lock:
            data = {
                "schema": SCHEMA_VERSION,
                "signature": self.signature,
                "buckets": list(self.buckets),
                "last_readings": dict(self.last_readings),
                "reset_count": self.reset_count,
            }
            try:
                packed = await self.hass.async_add_executor_job(pack, data)
                await self.backend.async_save(packed)
                self.status = "ready"
            except Exception:
                self.status = "save_failed"
                LOGGER.exception("Energy history save failed; retaining buckets in memory")


class EnergyEvidence(DataUpdateCoordinator):
    """Aligned five-minute buckets; read-only access for reports and the advisor."""

    def __init__(self, hass, entry, heating):
        super().__init__(hass, LOGGER, name=f"{DOMAIN} energy", config_entry=entry)
        self.entry = entry
        self.heating = heating
        self.config = heating.config
        self.specs = meter_specs(self.config)
        self.store = EnergyStore(hass, entry.entry_id)
        self.samples = []
        self.carry = None
        self.unsubscribers = []
        self.closed = False
        self.unsaved = 0
        self.save_task = None
        self.async_set_updated_data({"last_bucket_at": None})

    @property
    def slugs(self):
        return [spec["slug"] for spec in self.specs]

    async def initialise(self):
        await self.store.load(meter_signature(self.specs))

    @callback
    def start(self):
        self.sample()
        watched = sorted(
            {self.config[k] for k in CONTEXT_KEYS if self.config.get(k)}
            | {spec["entity"] for spec in self.specs}
        )
        self.unsubscribers.extend(
            [
                async_track_state_change_event(self.hass, watched, self.sample),
                async_track_utc_time_change(
                    self.hass,
                    self.close_bucket,
                    minute=list(range(0, 60, BUCKET_SECONDS // 60)),
                    second=0,
                ),
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop),
            ]
        )

    def context_sample(self, now, at=None):
        """Context at *now*; freshness follows the history state policy like analytics."""
        states = {
            e: s
            for k in CONTEXT_KEYS
            if (e := self.config.get(k)) and (s := self.hass.states.get(e))
        }
        held = self.config.get("history_state_policy", "recorded_state") == "recorded_state"
        values = {}
        for key in CONTEXT_KEYS:
            spec = SYSTEM_SOURCES[key]
            values[key] = read(
                states,
                self.config.get(key),
                now,
                kind=spec.kind,
                max_age=None if held else spec.max_age,
            ).value
        return (
            at if at is not None else now.timestamp(),
            values["heating_active"],
            values["dhw_active"],
            values["outdoor_temperature"],
        )

    @callback
    def sample(self, _event=None):
        if self.closed:
            return
        try:
            self.samples.append(self.context_sample(dt_util.utcnow()))
        except Exception:
            LOGGER.exception("Energy context sample failed")

    def reading(self, spec, now):
        state = self.hass.states.get(spec["entity"])
        if state is None or state.state in ("unknown", "unavailable"):
            return None
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return None
        if not finite(value):
            return None
        return {
            "value": value,
            "unit": state.attributes.get("unit_of_measurement"),
            "time": now.timestamp(),
            "entity": spec["entity"],
        }

    def intervention(self, start, end):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return False
        try:
            events = journal.events(
                kinds=sorted(INTERVENTION_KINDS),
                since=datetime.fromtimestamp(start, timezone.utc),
                until=datetime.fromtimestamp(end, timezone.utc),
                limit=1,
            )
            return bool(events)
        except Exception:
            LOGGER.debug("Journal lookup failed; intervention flag unknown", exc_info=True)
            return False

    def build_bucket(self, now):
        end = int(now.timestamp() // BUCKET_SECONDS) * BUCKET_SECONDS
        start = end - BUCKET_SECONDS
        # The closing sample is pinned to the bucket end so it carries into the next
        # bucket as its opening state; event samples at the boundary take precedence.
        samples = ([self.carry] if self.carry else []) + sorted(self.samples, key=lambda s: s[0])
        samples.append(self.context_sample(now, at=end))
        context = bucket_context(samples, start, end)
        self.carry = samples[-1]
        self.samples = []
        meters = {}
        for spec in self.specs:
            current = self.reading(spec, now)
            previous = self.store.last_readings.get(spec["slug"])
            delta = meter_delta(previous, current, spec)
            if delta["quality"] in ("reset", "rollover"):
                self.store.reset_count += 1
            meters[spec["slug"]] = delta
            if current is not None:
                self.store.last_readings[spec["slug"]] = current
        return {
            "time": start,
            "meters": meters,
            "heating_share": context["heating"],
            "dhw_share": context["dhw"],
            "outdoor": context["outdoor"],
            "allocation": allocation(context["heating"], context["dhw"]),
            "era": configuration_era(self.config),
            "intervention": self.intervention(start, end),
        }

    @callback
    def close_bucket(self, now=None):
        if self.closed:
            return
        now = now or dt_util.utcnow()
        try:
            self.store.append(self.build_bucket(now))
            self.store.prune(now.timestamp())
            self.async_set_updated_data({"last_bucket_at": now.isoformat()})
        except Exception:
            LOGGER.exception("Energy bucket failed; heating control is unaffected")
            return
        self.unsaved += 1
        if self.unsaved >= SAVE_EVERY_BUCKETS and (self.save_task is None or self.save_task.done()):
            self.unsaved = 0
            self.save_task = self.entry.async_create_background_task(
                self.hass, self.store.save(), "Heating energy save"
            )

    def days(self, limit=MAX_DAYS):
        cutoff = dt_util.utcnow().timestamp() - limit * 86400
        buckets = [b for b in self.store.buckets if b["time"] >= cutoff]
        return group_days(buckets, self.hass.config.time_zone, self.slugs)

    def coverage(self, days=7):
        recent = self.days(days)
        if not recent:
            return 0.0
        expected = days * 86400 // BUCKET_SECONDS
        metered = sum(d["coverage_percent"] * 86400 // BUCKET_SECONDS / 100 for d in recent)
        return round(min(100.0, 100 * metered / expected), 1)

    @property
    def status(self):
        if not self.specs:
            return "no_meter"
        if self.store.status == "storage_read_only":
            return "storage_read_only"
        return "ready" if self.coverage(1) > 0 else "insufficient_data"

    def quality(self):
        recent = self.days(7)
        unknown = [d["allocation_unknown_share"] for d in recent]
        return {
            "status": self.status,
            "storage": self.store.status,
            "meter_count": len(self.specs),
            "meter_kinds": sorted({spec["kind"] for spec in self.specs}),
            "coverage_percent_7d": self.coverage(7),
            "last_bucket_at": self.data.get("last_bucket_at"),
            "reset_count": self.store.reset_count,
            "allocation_unknown_share": round(sum(unknown) / len(unknown), 3) if unknown else None,
            "bucket_count": len(self.store.buckets),
            "era": configuration_era(self.config),
        }

    def daily_kwh(self, slug):
        today = self.days(1)
        if not today:
            return None
        current = today[-1]
        local_today = dt_util.now().date().isoformat()
        return current["kwh"].get(slug) if current["date"] == local_today else None

    def comparability(self, since):
        """Period from *since* to now against the equal-length period before it."""
        a, b, length = since_periods(
            self.days(), since, dt_util.utcnow(), self.hass.config.time_zone
        )
        return comparability(a, b, self.slugs, expected_days=length)

    def report(self, days=7):
        """Calendar window of *days* ending today against the equal-length window before.

        Coverage and comparability are measured against the requested calendar span,
        so wholly missing days count; both windows carry ``expected_days``.
        """
        days = int(days)
        now = dt_util.utcnow()
        recent, previous, length = since_periods(
            self.days(), now - timedelta(days=days - 1), now, self.hass.config.time_zone
        )
        return {
            "quality": self.quality(),
            "meters": meter_signature(self.specs),
            "expected_days": length,
            "observed_days": len(recent),
            "previous_observed_days": len(previous),
            "days": recent,
            "summary": summarise_period(recent, self.slugs, expected_days=length),
            "recent_vs_previous": comparability(recent, previous, self.slugs, expected_days=length),
            "definitions": {
                "kwh": "Metered input energy from the selected counter; not delivered heat unless a delivered_heat meter is configured separately.",
                "heating_degree_hours": "Sum over five-minute buckets of max(0, 15.5 - outdoor) hours; weather context only.",
                "allocation": "heating, dhw, idle or unknown per bucket; energy is never split between heating and DHW.",
                "era": "Hash of actuator bindings when control is configured, else 'observation'.",
                "kwh_per_degree_hour": "Association between metered energy and weather; not causal evidence or savings.",
                "coverage": "Share of the five-minute buckets expected over the whole requested calendar span that carry an ok or rollover meter delta; wholly missing days count against it.",
            },
        }

    async def stop(self, _event=None):
        if self.closed:
            return
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        if self.save_task and not self.save_task.done():
            self.save_task.cancel()
            try:
                await self.save_task
            except asyncio.CancelledError:
                pass
        await self.store.save()
