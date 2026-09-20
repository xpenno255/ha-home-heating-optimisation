"""Isolated, bounded advisor execution and private report retention."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from uuid import uuid4

import voluptuous as vol
from homeassistant.const import EVENT_HOMEASSISTANT_STOP
from homeassistant.core import callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from .evidence import (
    INSTRUCTIONS,
    PROMPT_VERSION,
    TASKS,
    ReportValidationError,
    build_evidence,
    encode,
    evidence_hash,
    report_schema,
    validate_response,
)
from .profiles import profile_info, profiles

MAX_REPORTS = 20


async def generate(hass, **kwargs):
    # Do not import optional provider dependencies when the advisor is unused.
    from homeassistant.components.ai_task import async_generate_data

    return await async_generate_data(hass, **kwargs)


class Advisor:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.config = heating.config.get("advisor", {})
        self.backend = Store(hass, 1, f"home_heating_optimisation.{entry.entry_id}.advisor")
        self.data = {"schema": 1, "reports": [], "attempts": [], "scheduled": {}}
        self.status = "disabled" if not self.config.get("enabled") else "ready"
        self.error_type = None
        self.storage_ready = True
        self.closed = False
        self.running = None
        self.schedule_task = None
        self.unsubscribers = []

    async def initialise(self):
        try:
            data = await self.backend.async_load()
            if data is not None:
                if (
                    data.get("schema") != 1
                    or not isinstance(data.get("reports"), list)
                    or not isinstance(data.get("attempts"), list)
                    or not isinstance(data.get("scheduled"), dict)
                    or len(encode(data).encode()) > 2000000
                ):
                    raise ValueError("invalid_store")
                if any(
                    not isinstance(t, (float, int)) or isinstance(t, bool) for t in data["attempts"]
                ):
                    raise ValueError("invalid_attempts")
                for report in data["reports"]:
                    if (
                        not isinstance(report.get("id"), str)
                        or not isinstance(report.get("created_at"), str)
                        or report.get("task") not in TASKS
                        or not isinstance(report.get("profile"), dict)
                    ):
                        raise ValueError("invalid_report")
                    validate_response(report["report"], report["evidence"])
                self.data = data
                self.data["reports"] = self.data["reports"][-MAX_REPORTS:]
        except Exception:
            self.storage_ready = False
            self.status = "storage_read_only"

    def changed(self, status):
        self.status = status
        if not self.closed:
            self.heating.async_set_updated_data(self.heating.data)

    def quality(self):
        return {
            "status": self.status,
            "error_type": self.error_type,
            "report_count": len(self.data["reports"]),
            "last_report_at": self.data["reports"][-1]["created_at"]
            if self.data["reports"]
            else None,
            "running": self.running is not None,
        }

    def report_list(self, report_id=None):
        if report_id:
            report = next((r for r in self.data["reports"] if r["id"] == report_id), None)
            if report is None:
                raise ServiceValidationError("Unknown advisor report ID")
            return deepcopy(report)
        return {
            **self.quality(),
            "profiles": profiles(self.hass),
            "tasks": self.config,
            "reports": [
                {k: r[k] for k in ("id", "created_at", "task", "profile", "report")}
                for r in reversed(self.data["reports"])
            ],
        }

    async def persist(self):
        try:
            await self.backend.async_save(deepcopy(self.data))
        except Exception as err:
            self.storage_ready = False
            self.changed("save_failed")
            raise HomeAssistantError(
                "Advisor storage failed; no further AI calls until reload"
            ) from err

    async def run(self, task, question="", scheduled_key=None):
        if self.closed or not self.config.get("enabled"):
            raise ServiceValidationError("Enable the Heating Advisor in integration options")
        if self.running:
            raise ServiceValidationError("A heating review is already running")
        if not self.storage_ready:
            raise ServiceValidationError("Advisor storage is unavailable")
        analytics = self.heating.analytics
        if analytics is None or analytics.backfill_status in ("pending", "running"):
            raise ServiceValidationError("Wait for heating analytics to become ready")
        entity = self.config.get(task)
        if task not in TASKS or not entity:
            raise ServiceValidationError("Select an AI Task profile for this heating task")
        profile = profile_info(self.hass, entity)
        if profile["status"] != "ready":
            self.changed(profile["status"])
            raise ServiceValidationError(f"AI profile cannot run: {profile['status']}")
        now = dt_util.utcnow()
        attempts = [t for t in self.data["attempts"] if t > now.timestamp() - 86400]
        if len(attempts) >= self.config.get("max_calls_per_day", 4):
            raise ServiceValidationError("Heating Advisor rolling 24-hour call limit reached")
        if attempts and now.timestamp() - max(attempts) < 60:
            raise ServiceValidationError("Wait one minute between heating reviews")
        self.error_type = None
        self.running = asyncio.current_task()
        self.changed("running")
        try:
            await analytics.refresh()
            snapshot = self.heating.snapshot()
            evidence = build_evidence(
                analytics.report(),
                self.heating.house_report(),
                {
                    "availability_percent": snapshot.input_availability,
                    "system": {
                        k: {"value": r.value, "quality": r.quality}
                        for k, r in snapshot.system.items()
                    },
                    "room_quality": {
                        r.id: {
                            k: {"value": getattr(r, k).value, "quality": getattr(r, k).quality}
                            for k in ("air", "target", "demand")
                        }
                        for r in snapshot.rooms
                    },
                },
                task,
                question,
                energy=self.heating.energy.report() if self.heating.energy else None,
            )
            profile = profile_info(self.hass, entity)
            if profile["status"] != "ready":
                raise ServiceValidationError(
                    "AI profile changed or became unavailable before the call"
                )
            self.data["attempts"] = attempts + [now.timestamp()]
            if scheduled_key:
                self.data["scheduled"][task] = scheduled_key
            # Persist the attempt before a provider call, including failures/restarts.
            await self.persist()
            async with asyncio.timeout(self.config.get("timeout_seconds", 120)):
                result = await generate(
                    self.hass,
                    task_name=f"heating_{task}",
                    entity_id=entity,
                    instructions=INSTRUCTIONS
                    + "\nTask: "
                    + TASKS[task]
                    + "\nEvidence JSON:\n"
                    + encode(evidence),
                    structure=report_schema(vol.In(tuple(evidence["facts"]))),
                )
            validated = validate_response(result.data, evidence)
            report = {
                "id": uuid4().hex,
                "created_at": dt_util.utcnow().isoformat(),
                "task": task,
                "profile": profile,
                "prompt_version": PROMPT_VERSION,
                "evidence_hash": evidence_hash(evidence),
                "evidence": evidence,
                "report": validated,
                "validation": "Structure and reference existence checked; AI interpretations require human review.",
            }
            self.data["reports"] = (self.data["reports"] + [report])[-MAX_REPORTS:]
            await self.persist()
            self.changed("ready")
            return deepcopy(report)
        except asyncio.CancelledError:
            self.changed("cancelled")
            raise
        except TimeoutError as err:
            self.changed("timeout")
            raise HomeAssistantError("Heating Advisor timed out; no automatic retry") from err
        except (ValueError, TypeError) as err:
            self.error_type = (
                err.code if isinstance(err, ReportValidationError) else "invalid_payload"
            )
            self.changed("invalid_response")
            raise HomeAssistantError(
                f"Heating Advisor rejected invalid evidence or response ({self.error_type})"
            ) from err
        except Exception as err:
            self.error_type = (
                "context_limit"
                if "maximum context length" in str(err).lower()
                else "schema_unsupported"
                if "grammar error" in str(err).lower()
                else type(err).__name__
            )
            if self.storage_ready:
                self.changed("provider_failed")
            raise HomeAssistantError(
                f"Heating Advisor failed ({self.error_type}); heating observation continues"
            ) from err
        finally:
            self.running = None
            if not self.closed:
                self.heating.async_set_updated_data(self.heating.data)

    @callback
    def start(self):
        self.unsubscribers = [
            async_track_time_interval(self.hass, self.schedule, timedelta(minutes=1)),
            self.hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, self.stop),
        ]

    @callback
    def schedule(self, now):
        if (
            self.closed
            or not self.config.get("enabled")
            or self.running
            or (self.schedule_task and not self.schedule_task.done())
        ):
            return
        local = dt_util.as_local(now)
        if local.hour != self.config.get("schedule_hour", 9):
            return
        for task in ("daily_summary", "weekly_review"):
            if not self.config.get(f"schedule_{task}") or not self.config.get(task):
                continue
            if task == "weekly_review" and local.weekday() != 0:
                continue
            key = local.date().isoformat()
            if self.data["scheduled"].get(task) == key:
                continue
            self.schedule_task = self.entry.async_create_background_task(
                self.hass, self.scheduled_run(task, key), "Heating Advisor scheduled review"
            )
            break

    async def scheduled_run(self, task, key):
        try:
            await self.run(task, scheduled_key=key)
        except HomeAssistantError:
            # Status is visible; never log private provider responses or retry a call.
            pass

    async def stop(self, _event=None):
        self.closed = True
        for unsub in self.unsubscribers:
            unsub()
        self.unsubscribers.clear()
        for task in {self.running, self.schedule_task} - {None, asyncio.current_task()}:
            if not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
