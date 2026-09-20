"""Optional background analytics; observation setup never waits for Recorder."""

import asyncio
import logging
from copy import deepcopy
from dataclasses import asdict
from datetime import timedelta
from functools import partial

from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.helpers.event import (
    async_track_state_change_event,
    async_track_time_interval,
    async_track_utc_time_change,
)
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from ..observations import watched_entities
from .analyzer import compare_windows, compute_analytics
from .backfill import async_backfill
from .const import SAMPLE_SECONDS
from .observations import snapshot
from .sampling import should_capture
from .store import HistoryStore, source_signature

LOGGER = logging.getLogger(__name__)


def calculate(points, notes, config, now, timezone):
    rooms = {r["id"]: r["name"] for r in config["rooms"]}
    compute = partial(
        compute_analytics,
        points,
        list(rooms),
        zone_names=rooms,
        timezone_name=timezone,
        tolerance=config.get("comfort_tolerance", 0.3),
        recovery_minutes=config.get("recovery_minutes", 120),
        adjustment_times=[n["time"] for n in notes],
    )
    main = compute(config.get("analysis_window_days", 7), now=now)
    current = compute(1, now=now)
    previous = compute(1, now=now - timedelta(days=1))
    return {
        "analysis": asdict(main),
        "comparison": asdict(compare_windows(current, previous, rooms)),
    }


class AnalyticsCoordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry, config, state_reader=None):
        super().__init__(hass, LOGGER, name=f"{DOMAIN} history", config_entry=entry)
        self.state_reader = state_reader or hass.states.get
        self.entry = entry
        self.config = config
        self.sources = watched_entities(config)
        self.store = HistoryStore(hass, entry.entry_id)
        self.backfill_status = "pending"
        self.task = None
        self.refresh_task = None
        self.closed = False
        self.source_states = {}
        self.last_capture = None
        self.calculation_lock = asyncio.Lock()
        self.unsubscribers = []
        self.async_set_updated_data(
            calculate([], [], config, dt_util.utcnow(), hass.config.time_zone)
        )

    async def initialise(self):
        await self.store.load(
            source_signature(self.config, self.hass.config.units.temperature_unit)
        )

    @callback
    def capture(self, _event=None):
        if self.closed:
            return
        now = dt_util.utcnow()
        states = {e: s for e in self.sources if (s := self.state_reader(e)) is not None}
        changed = {_event.data["entity_id"]} if hasattr(_event, "data") else set()
        before = self.source_states
        self.source_states = states
        if should_capture(self.config, changed, before, states, now.timestamp(), self.last_capture):
            self.store.append(
                snapshot(states, now, self.config, self.hass.config.units.temperature_unit)
            )
            self.last_capture = now.timestamp()

    @callback
    def start(self):
        self.capture()
        self.unsubscribers.extend(
            [
                async_track_state_change_event(self.hass, self.sources, self.capture),
                async_track_utc_time_change(
                    self.hass,
                    self.capture,
                    minute=list(range(0, 60, SAMPLE_SECONDS // 60)),
                    second=0,
                ),
                async_track_time_interval(
                    self.hass,
                    self.schedule_refresh,
                    timedelta(minutes=self.config.get("update_interval_minutes", 15)),
                ),
                self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop),
            ]
        )
        self.task = self.entry.async_create_background_task(
            self.hass, self.backfill(), "Heating history backfill"
        )

    async def backfill(self):
        now = dt_util.utcnow()
        self.backfill_status = "running"
        try:
            points, truncated, self.backfill_status = await async_backfill(
                self.hass, self.config, now - timedelta(days=15), now
            )
            self.store.merge(points)
            self.store.truncated |= truncated
        except Exception:
            self.backfill_status = "failed"
            LOGGER.exception("Heating Recorder backfill failed; live collection continues")
        await self.refresh()

    @callback
    def schedule_refresh(self, _event=None):
        if not self.closed and (self.refresh_task is None or self.refresh_task.done()):
            self.refresh_task = self.entry.async_create_background_task(
                self.hass, self.refresh(), "Heating analytics refresh"
            )

    async def refresh(self):
        async with self.calculation_lock:
            if self.closed:
                return
            now = dt_util.utcnow()
            self.store.prune(now.timestamp())
            data = await self.hass.async_add_executor_job(
                calculate,
                list(self.store.observations),
                list(self.store.adjustments),
                self.config,
                now,
                self.hass.config.time_zone,
            )
            await self.store.save()
            if not self.closed:
                self.async_set_updated_data(data)

    def report(self):
        """Structured evidence for a human or future AI task; notes stay out of Recorder."""
        report = deepcopy(
            {
                **self.data,
                "quality": self.quality(),
                "rooms": [{"id": r["id"], "name": r["name"]} for r in self.config["rooms"]],
                "adjustments": self.store.adjustments,
                "recent_decision_context": [
                    {"time": p["time"], "intent": p.get("intent", {})}
                    for p in self.store.decision_context[-100:]
                ],
                "definitions": {
                    "demand_active": "selected demand > 0",
                    "freshness_basis": self.config.get("history_state_policy", "recorded_state"),
                    "coverage": "Known recorded state duration; not proof of fresh physical samples. Recent-change coverage is reported separately.",
                    "context_sampling_seconds": 60,
                    "decision_context": "reported controller intent and estimates; not measured room comfort",
                    "control": "configured_controllers"
                    if self.config.get("control")
                    else "observation_only",
                },
            }
        )

        # Reports apply the same evidence gate as sensors. Raw partial-window totals
        # must not be mistaken for whole-window performance by a future AI consumer.
        for stats in report["analysis"]["zone_stats"].values():
            stats["suppressed_metrics"] = []
            for key, coverage in (
                ("within_band", "coverage"),
                ("deficit_degree_hours", "coverage"),
                ("overshoot_degree_hours", "coverage"),
                ("duty_cycle", "demand_coverage"),
            ):
                if stats[coverage] < 80:
                    stats[key] = None
                    stats["suppressed_metrics"].append(key)
        report["settings"] = {
            "comfort_tolerance_c": self.config.get("comfort_tolerance", 0.3),
            "recovery_minutes": self.config.get("recovery_minutes", 120),
        }
        return report

    def quality(self):
        return {
            "backfill": self.backfill_status,
            "storage": self.store.status,
            "observation_count": len(self.store.observations),
            "history_truncated": self.store.truncated,
            "adjustment_count": len(self.store.adjustments),
            "imported_era_count": len(self.store.imported_eras),
            "imported_observation_count": sum(
                len(e["observations"]) for e in self.store.imported_eras
            ),
            "decision_context_count": len(self.store.decision_context),
            "history_state_policy": self.config.get("history_state_policy", "recorded_state"),
        }

    async def stop(self, _event=None):
        if self.closed:
            return
        self.closed = True
        for unsubscribe in self.unsubscribers:
            unsubscribe()
        self.unsubscribers.clear()
        for task in (self.task, self.refresh_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        # Explicit gap boundary prevents a last pre-shutdown reading covering downtime.
        self.store.append({"time": dt_util.utcnow().timestamp(), "zones": {}, "context": {}})
        await self.store.save()
