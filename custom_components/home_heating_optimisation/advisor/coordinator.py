"""Isolated, bounded advisor execution and private report retention."""

import asyncio
import logging
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
    FOLLOWUP_INSTRUCTIONS,
    INSTRUCTIONS,
    MAX_CONVERSATIONS,
    MAX_QUESTION_CHARS,
    MAX_TURNS_PER_CONVERSATION,
    PROMPT_VERSION,
    TASKS,
    ReportValidationError,
    build_evidence,
    build_followup_payload,
    encode,
    evidence_hash,
    followup_schema,
    report_schema,
    validate_followup_response,
    validate_response,
)
from .notify import EVENTS, FAILURE_EVENTS, deliver, failure_message, report_message
from .profiles import profile_info, profiles
from .recommendations import Recommendations
from .reports import latest_attributes, summarise

LOGGER = logging.getLogger(__name__)
MAX_REPORTS = 20
NOTIFY_DEFAULT = {"last_report_id": None, "failures": {}}
FOLLOWUP_TASK = "followup"
TURN_FIELDS = ("question", "answer", "references", "unsupported_claims", "missing_data")


def validate_conversation(conversation):
    if (
        not isinstance(conversation, dict)
        or not isinstance(conversation.get("id"), str)
        or not isinstance(conversation.get("report_id"), str)
        or not isinstance(conversation.get("evidence_hash"), str)
        or not isinstance(conversation.get("created_at"), str)
        or not isinstance(conversation.get("updated_at"), str)
        or not isinstance(conversation.get("turns"), list)
        or len(conversation["turns"]) > MAX_TURNS_PER_CONVERSATION
    ):
        raise ValueError("invalid_conversation")
    for turn in conversation["turns"]:
        if (
            not isinstance(turn, dict)
            or not isinstance(turn.get("turn"), int)
            or not isinstance(turn.get("profile"), dict)
            or not all(isinstance(turn.get(k), str) for k in ("question", "answer", "created_at"))
            or not all(
                isinstance(turn.get(k), list) and all(isinstance(s, str) for s in turn[k])
                for k in ("references", "unsupported_claims", "missing_data")
            )
        ):
            raise ValueError("invalid_conversation_turn")


async def generate(hass, **kwargs):
    # Do not import optional provider dependencies when the advisor is unused.
    from homeassistant.components.ai_task import async_generate_data

    return await async_generate_data(hass, **kwargs)


class Advisor:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.config = heating.config.get("advisor", {})
        self.backend = Store(hass, 1, f"home_heating_optimisation.{entry.entry_id}.advisor")
        self.data = {
            "schema": 1,
            "reports": [],
            "attempts": [],
            "scheduled": {},
            "conversations": [],
            "notify": deepcopy(NOTIFY_DEFAULT),
        }
        self.status = "disabled" if not self.config.get("enabled") else "ready"
        self.notify_status = "enabled" if self.config.get("notify_enabled") else "disabled"
        self.error_type = None
        self.storage_ready = True
        self.recommendations = Recommendations(hass, entry, heating)
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
                # Schema 1 stores written before follow-ups have no conversations.
                conversations = data.setdefault("conversations", [])
                if not isinstance(conversations, list):
                    raise ValueError("invalid_conversations")
                for conversation in conversations:
                    validate_conversation(conversation)
                # Schema 1 stores written before notifications have no notify state.
                notify = data.get("notify", deepcopy(NOTIFY_DEFAULT))
                if (
                    not isinstance(notify, dict)
                    or not isinstance(notify.get("failures", {}), dict)
                    or not isinstance(notify.get("last_report_id"), (str, type(None)))
                ):
                    raise ValueError("invalid_notify_state")
                data["notify"] = {**deepcopy(NOTIFY_DEFAULT), **notify}
                self.data = data
                self.data["reports"] = self.data["reports"][-MAX_REPORTS:]
                self.data["conversations"] = conversations[-MAX_CONVERSATIONS:]
        except Exception:
            self.storage_ready = False
            self.status = "storage_read_only"
        # Recommendation storage is independent: its corruption never blocks reports.
        await self.recommendations.initialise()

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
            "notify_status": self.notify_status,
            "recommendations": self.recommendations.quality(),
        }

    def latest(self):
        return self.data["reports"][-1] if self.data["reports"] else None

    def latest_attributes(self):
        report = self.latest()
        return latest_attributes(report) if report else {}

    def report_summary(self, report_id=None):
        """Rendered text plus allowlisted fields; evidence values are never included."""
        if report_id:
            report = next((r for r in self.data["reports"] if r["id"] == report_id), None)
            if report is None:
                raise ServiceValidationError("Unknown or deleted advisor report ID")
        else:
            report = self.latest()
            if report is None:
                raise ServiceValidationError("No advisor report has been retained yet")
        return summarise(report)

    def find_report(self, report_id):
        report = next((r for r in self.data["reports"] if r["id"] == report_id), None)
        if report is None:
            raise ServiceValidationError("Unknown advisor report ID")
        return report

    def find_conversation(self, conversation_id):
        conversation = next(
            (c for c in self.data["conversations"] if c["id"] == conversation_id), None
        )
        if conversation is None:
            raise ServiceValidationError("Unknown advisor conversation ID")
        return conversation

    def conversation_summaries(self, report_id):
        # IDs and turn counts only; question/answer text stays out of listings.
        return [
            {"id": c["id"], "turns": len(c["turns"]), "updated_at": c["updated_at"]}
            for c in self.data["conversations"]
            if c["report_id"] == report_id
        ]

    def report_list(self, report_id=None):
        if report_id:
            return deepcopy(self.find_report(report_id))
        return {
            **self.quality(),
            "profiles": profiles(self.hass),
            "tasks": self.config,
            "reports": [
                {
                    **{k: r[k] for k in ("id", "created_at", "task", "profile", "report")},
                    "conversations": self.conversation_summaries(r["id"]),
                }
                for r in reversed(self.data["reports"])
            ],
        }

    def followup_conversation(self, conversation_id):
        return deepcopy(self.find_conversation(conversation_id))

    async def persist(self):
        try:
            await self.backend.async_save(deepcopy(self.data))
        except Exception as err:
            self.storage_ready = False
            self.changed("save_failed")
            raise HomeAssistantError(
                "Advisor storage failed; no further AI calls until reload"
            ) from err

    def gate(self, task):
        if self.closed or not self.config.get("enabled"):
            raise ServiceValidationError("Enable the Heating Advisor in integration options")
        if self.running:
            raise ServiceValidationError("A heating review is already running")
        if not self.storage_ready:
            raise ServiceValidationError("Advisor storage is unavailable")
        entity = self.config.get(task)
        if (task not in TASKS and task != FOLLOWUP_TASK) or not entity:
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
        return entity, attempts, now

    def live_quality(self):
        snapshot = self.heating.snapshot()
        return {
            "availability_percent": snapshot.input_availability,
            "system": {
                k: {"value": r.value, "quality": r.quality} for k, r in snapshot.system.items()
            },
            "room_quality": {
                r.id: {
                    k: {"value": getattr(r, k).value, "quality": getattr(r, k).quality}
                    for k in ("air", "target", "demand")
                }
                for r in snapshot.rooms
            },
        }

    async def call_profile(self, entity, task_name, instructions, structure):
        # Re-check the profile immediately before the call, then persist the
        # attempt so failures and restarts still count against the budget.
        profile = profile_info(self.hass, entity)
        if profile["status"] != "ready":
            raise ServiceValidationError("AI profile changed or became unavailable before the call")
        await self.persist()
        async with asyncio.timeout(self.config.get("timeout_seconds", 120)):
            result = await generate(
                self.hass,
                task_name=task_name,
                entity_id=entity,
                instructions=instructions,
                structure=structure,
            )
        return profile, result

    async def execute(self, work, notify_task=None):
        # notify_task: heating task whose failures are notified; None disables failure alerts.
        self.error_type = None
        self.running = asyncio.current_task()
        self.changed("running")
        try:
            return await work()
        except asyncio.CancelledError:
            self.changed("cancelled")
            raise
        except TimeoutError as err:
            self.changed("timeout")
            if notify_task:
                await self.notify_failure(notify_task, "timeout")
            raise HomeAssistantError("Heating Advisor timed out; no automatic retry") from err
        except (ValueError, TypeError) as err:
            self.error_type = (
                err.code if isinstance(err, ReportValidationError) else "invalid_payload"
            )
            self.changed("invalid_response")
            if notify_task:
                await self.notify_failure(notify_task, "invalid_response")
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
                if notify_task:
                    await self.notify_failure(notify_task, "provider_failed")
            raise HomeAssistantError(
                f"Heating Advisor failed ({self.error_type}); heating observation continues"
            ) from err
        finally:
            self.running = None
            if not self.closed:
                self.heating.async_set_updated_data(self.heating.data)

    async def run(self, task, question="", scheduled_key=None):
        if self.closed or not self.config.get("enabled"):
            raise ServiceValidationError("Enable the Heating Advisor in integration options")
        analytics = self.heating.analytics
        if analytics is None or analytics.backfill_status in ("pending", "running"):
            raise ServiceValidationError("Wait for heating analytics to become ready")
        entity, attempts, now = self.gate(task)

        async def work():
            await analytics.refresh()
            evidence = build_evidence(
                analytics.report(),
                self.heating.house_report(),
                self.live_quality(),
                task,
                question,
                energy=self.heating.energy.report() if self.heating.energy else None,
            )
            self.data["attempts"] = attempts + [now.timestamp()]
            if scheduled_key:
                self.data["scheduled"][task] = scheduled_key
            profile, result = await self.call_profile(
                entity,
                f"heating_{task}",
                INSTRUCTIONS + "\nTask: " + TASKS[task] + "\nEvidence JSON:\n" + encode(evidence),
                report_schema(vol.In(tuple(evidence["facts"]))),
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
            # Conversations outlive their report only until the report is dropped.
            retained = {r["id"] for r in self.data["reports"]}
            self.data["conversations"] = [
                c for c in self.data["conversations"] if c["report_id"] in retained
            ]
            await self.persist()
            # Never raises; a recommendation store failure cannot fail a stored report.
            await self.recommendations.add_report(report)
            self.changed("ready")
            self.journal(report)
            await self.notify_report(report)
            return deepcopy(report)

        return await self.execute(work, notify_task=task)

    async def followup(self, report_id, question, conversation_id=None):
        # Bounded question about one retained report and its saved evidence snapshot.
        # No tools, no control authority, no fallback to another task's profile.
        question = question.strip() if isinstance(question, str) else ""
        if not 1 <= len(question) <= MAX_QUESTION_CHARS:
            raise ServiceValidationError(
                f"Ask a follow-up question of 1-{MAX_QUESTION_CHARS} characters"
            )
        report = self.find_report(report_id)
        if evidence_hash(report["evidence"]) != report["evidence_hash"]:
            raise ServiceValidationError("Saved evidence does not match this report")
        conversation = None
        if conversation_id:
            conversation = self.find_conversation(conversation_id)
            if conversation["report_id"] != report_id:
                raise ServiceValidationError("Conversation belongs to a different report")
            if conversation["evidence_hash"] != report["evidence_hash"]:
                raise ServiceValidationError("Conversation evidence snapshot no longer matches")
            if len(conversation["turns"]) >= MAX_TURNS_PER_CONVERSATION:
                raise ServiceValidationError(
                    f"Conversation limit of {MAX_TURNS_PER_CONVERSATION} turns reached"
                )
        entity, attempts, now = self.gate(FOLLOWUP_TASK)
        prior = conversation["turns"] if conversation else []

        async def work():
            quality = self.live_quality()
            payload = build_followup_payload(
                report["evidence"],
                report["report"],
                prior,
                question,
                {
                    "availability_percent": quality["availability_percent"],
                    "room_quality": {
                        room: {k: v["quality"] for k, v in flags.items()}
                        for room, flags in quality["room_quality"].items()
                    },
                },
            )
            self.data["attempts"] = attempts + [now.timestamp()]
            profile, result = await self.call_profile(
                entity,
                "heating_followup",
                FOLLOWUP_INSTRUCTIONS + "\nFollow-up JSON:\n" + encode(payload),
                followup_schema(vol.In(tuple(report["evidence"]["facts"]))),
            )
            validated = validate_followup_response(result.data, report["evidence"])
            created_at = dt_util.utcnow().isoformat()
            turn = {
                "turn": len(prior) + 1,
                "created_at": created_at,
                "prompt_version": PROMPT_VERSION,
                "profile": profile,
                "question": question,
                **validated,
            }
            target = conversation
            if target is None:
                target = {
                    "id": uuid4().hex,
                    "report_id": report_id,
                    "evidence_hash": report["evidence_hash"],
                    "created_at": created_at,
                    "updated_at": created_at,
                    "turns": [],
                }
                self.data["conversations"] = (self.data["conversations"] + [target])[
                    -MAX_CONVERSATIONS:
                ]
            target["turns"] = target["turns"] + [turn]
            target["updated_at"] = created_at
            await self.persist()
            self.record_followup(target, turn)
            self.changed("ready")
            return {
                "conversation_id": target["id"],
                "report_id": report_id,
                "turn": turn["turn"],
                "turns_remaining": MAX_TURNS_PER_CONVERSATION - turn["turn"],
                "profile": profile,
                "question": question,
                **{k: deepcopy(validated[k]) for k in TURN_FIELDS if k != "question"},
                "validation": "Structure and reference existence checked; AI interpretations require human review.",
            }

        return await self.execute(work)

    def journal(self, report):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "advisor_report",
                scope="system",
                origin="advisor",
                data={
                    k: v
                    for k, v in latest_attributes(report).items()
                    if k in ("report_id", "task", "profile_name", "finding_count")
                },
            )
        except Exception:  # noqa: BLE001 - journal failures never affect the advisor
            LOGGER.warning("Heating Advisor could not journal report %s", report["id"])

    def notify_wanted(self, event):
        return (
            self.config.get("notify_enabled")
            and event in EVENTS
            and event in self.config.get("notify_on", ["report_ready"])
        )

    async def notify_report(self, report):
        try:
            await self._notify_report(report)
        except Exception as err:  # noqa: BLE001 - notifications never affect run()
            self.notify_status = "error"
            LOGGER.warning("Heating Advisor notification error: %s", type(err).__name__)

    async def notify_failure(self, task, status):
        try:
            await self._notify_failure(task, status)
        except Exception as err:  # noqa: BLE001 - notifications never affect run()
            self.notify_status = "error"
            LOGGER.warning("Heating Advisor notification error: %s", type(err).__name__)

    async def _notify_report(self, report):
        if not self.notify_wanted("report_ready"):
            return
        if self.data["notify"].get("last_report_id") == report["id"]:
            self.notify_status = "skipped_duplicate"
            return
        # Record before delivery so a crash mid-send cannot repeat the notification.
        self.data["notify"]["last_report_id"] = report["id"]
        await self.persist_notify_state()
        title, message = report_message(report, self.config.get("notify_include_summary", False))
        await self.send(title, message, f"{self.entry.entry_id}_advisor_report")

    async def _notify_failure(self, task, status):
        event = FAILURE_EVENTS.get(status)
        if not event or not self.notify_wanted(event):
            return
        day = dt_util.as_local(dt_util.utcnow()).date().isoformat()
        failures = self.data["notify"].setdefault("failures", {})
        if failures.get(task, {}).get("day") == day:
            self.notify_status = "skipped_duplicate"
            return
        failures[task] = {"status": status, "day": day}
        await self.persist_notify_state()
        title, message = failure_message(task, status, self.error_type)
        await self.send(title, message, f"{self.entry.entry_id}_advisor_failure")

    async def persist_notify_state(self):
        if not self.storage_ready:
            return
        try:
            await self.backend.async_save(deepcopy(self.data))
        except Exception:  # noqa: BLE001 - dedupe state is best effort
            LOGGER.warning("Heating Advisor could not save notification state")

    async def send(self, title, message, notification_id):
        try:
            await deliver(
                self.hass, self.config.get("notify_targets", []), title, message, notification_id
            )
        except Exception as err:  # noqa: BLE001 - delivery never affects run() or heating
            self.notify_status = "delivery_failed"
            LOGGER.warning("Heating Advisor notification delivery failed: %s", type(err).__name__)
        else:
            self.notify_status = "sent"

    def record_followup(self, conversation, turn):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "advisor_followup",
                scope="system",
                origin="service",
                data={
                    "report_id": conversation["report_id"],
                    "conversation_id": conversation["id"],
                    "turn": turn["turn"],
                    "profile_name": turn["profile"].get("name"),
                },
            )
        except Exception:  # noqa: BLE001 - journaling never blocks a follow-up
            pass

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
