"""Durable recommendation decisions and follow-up outcomes; never actuator commands.

A recommendation is extracted from each stored advisor report finding. Owners record
explicit decisions and outcomes here. Accepting or applying a recommendation changes
no room mode, boiler setting or DHW protection: the owner performs any change through
the existing controls and records it afterwards. Follow-up evidence is always labelled
as association; this module never claims a causal effect.
"""

import hashlib
import logging
from copy import deepcopy
from datetime import datetime, timedelta

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..control.configuration import actuator_fingerprint
from .evidence import encode

LOGGER = logging.getLogger(__name__)

SCHEMA = 1
MAX_RECOMMENDATIONS = 200
RETENTION_DAYS = 365
MAX_STORE_BYTES = 2000000
MIN_FOLLOWUP_DAYS = 7
MIN_COVERAGE_PERCENT = 80
STATES = ("proposed", "accepted", "rejected", "deferred", "applied", "evaluated")
DECISIONS = ("accepted", "rejected", "deferred")
OUTCOMES = ("improved", "no_change", "worse", "inconclusive", "failed")
TRANSITIONS = {
    "proposed": {"accepted", "rejected", "deferred"},
    "deferred": {"accepted", "rejected"},
    "accepted": {"applied"},
    "applied": {"evaluated"},
    "rejected": set(),
    "evaluated": set(),
}
PRIVATE_FIELDS = ("private_note", "intervention_note")
INTERVENTION_KINDS = ["command_sent", "manual_override", "mode_change", "adjustment_note"]
EVIDENCE_TYPE = "association"


def recommendation_id(report_id, index):
    return hashlib.sha1(f"{report_id}{index}".encode()).hexdigest()[:16]


def configuration_era(control):
    """Short hash of the actuator mappings; a change invalidates before/after comparison."""
    return hashlib.sha256(repr(actuator_fingerprint(control)).encode()).hexdigest()[:16]


def finding_scope(references, facts):
    """Room IDs and system flag from cited fact keys; index keys resolve via identity facts."""
    rooms, system = [], False
    for ref in references:
        parts = ref.split(".")
        room = None
        if parts[0] == "room" and len(parts) > 1:
            room = facts.get(f"room.{parts[1]}.identity", {}).get("id")
        elif parts[:2] == ["controller", "room"] and len(parts) > 2:
            room = facts.get(f"room.{parts[2]}.identity", {}).get("id")
        elif parts[:2] == ["house", "room"] and len(parts) > 2:
            room = parts[2]
        if isinstance(room, str):
            if room not in rooms:
                rooms.append(room)
        else:
            system = True
    return {"room_ids": rooms, "system": system}


def extract(report, now):
    """One proposed recommendation per finding of a stored, validated report."""
    facts = report.get("evidence", {}).get("facts", {})
    profile = report.get("profile", {})
    at = now.isoformat()
    return [
        {
            "id": recommendation_id(report["id"], index),
            "report_id": report["id"],
            "finding_index": index,
            "evidence_hash": report.get("evidence_hash"),
            "prompt_version": report.get("prompt_version"),
            "profile": {k: profile.get(k) for k in ("name", "model")},
            "scope": finding_scope(finding.get("evidence_ids", []), facts),
            "title": finding["title"],
            "action_text": finding["next_check"],
            "state": "proposed",
            "created_at": at,
            "updated_at": at,
            "history": [{"state": "proposed", "at": at, "by": "advisor"}],
        }
        for index, finding in enumerate(report.get("report", {}).get("findings", []))
    ]


def eligibility(rec, now, coverage=None, era=None, energy=None):
    """Pure follow-up eligibility check. Result is association evidence, never causal.

    coverage: percent known analytics coverage over the scope, or None when unknown.
    era: current configuration era hash, or None when unknown.
    energy: dict with optional booleans dhw_share_comparable and
        outdoor_degree_hours_comparable, or None when the energy module is absent.
    """
    reasons, unknown = [], []
    if rec.get("state") != "applied":
        reasons.append("not_applied")
    applied_at = _parse(rec.get("applied_at"))
    if applied_at is None:
        if "not_applied" not in reasons:
            reasons.append("applied_time_unknown")
    elif now - applied_at < timedelta(days=MIN_FOLLOWUP_DAYS):
        reasons.append("too_early")
    if coverage is None:
        reasons.append("coverage_unknown")
    elif coverage < MIN_COVERAGE_PERCENT:
        reasons.append("low_coverage")
    if era is None or rec.get("applied_era") is None:
        reasons.append("era_unknown")
    elif era != rec["applied_era"]:
        reasons.append("era_changed")
    if not isinstance(energy, dict):
        unknown.append("energy")
    else:
        for key, reason in (
            ("dhw_share_comparable", "dhw_share_not_comparable"),
            ("outdoor_degree_hours_comparable", "outdoor_degree_hours_not_comparable"),
        ):
            value = energy.get(key)
            if value is None:
                unknown.append(key)
            elif value is False:
                reasons.append(reason)
    return {
        "eligible": not reasons,
        "reasons": reasons,
        "unknown": unknown,
        "evidence_type": EVIDENCE_TYPE,
        "minimum_days": MIN_FOLLOWUP_DAYS,
        "minimum_coverage_percent": MIN_COVERAGE_PERCENT,
    }


def _parse(value):
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return dt_util.as_utc(parsed) if parsed else None


def _valid(rec):
    return (
        isinstance(rec, dict)
        and isinstance(rec.get("id"), str)
        and isinstance(rec.get("report_id"), str)
        and rec.get("state") in STATES
        and isinstance(rec.get("title"), str)
        and isinstance(rec.get("action_text"), str)
        and isinstance(rec.get("scope"), dict)
        and isinstance(rec["scope"].get("room_ids"), list)
        and isinstance(rec.get("history"), list)
        and all(
            isinstance(h, dict) and h.get("state") in STATES and isinstance(h.get("at"), str)
            for h in rec["history"]
        )
        and isinstance(rec.get("created_at"), str)
        and isinstance(rec.get("updated_at"), str)
    )


class Recommendations:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.backend = Store(
            hass, SCHEMA, f"home_heating_optimisation.{entry.entry_id}.recommendations"
        )
        self.data = {"schema": SCHEMA, "recommendations": []}
        self.status = "ready"
        self.storage_ready = True

    # Storage -----------------------------------------------------------------

    async def initialise(self):
        try:
            data = await self.backend.async_load()
            if data is not None:
                if (
                    data.get("schema") != SCHEMA
                    or not isinstance(data.get("recommendations"), list)
                    or len(encode(data).encode()) > MAX_STORE_BYTES
                    or not all(_valid(r) for r in data["recommendations"])
                ):
                    raise ValueError("invalid_store")
                self.data = {"schema": SCHEMA, "recommendations": data["recommendations"]}
                self.bound(dt_util.utcnow())
        except Exception:
            # Preserve the unreadable file for inspection; never overwrite it.
            self.storage_ready = False
            self.status = "storage_read_only"

    def bound(self, now):
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        kept = [r for r in self.data["recommendations"] if r["updated_at"] >= cutoff]
        self.data["recommendations"] = kept[-MAX_RECOMMENDATIONS:]

    async def persist(self):
        if not self.storage_ready:
            raise HomeAssistantError("Recommendation storage is read-only until reload")
        try:
            await self.backend.async_save(deepcopy(self.data))
        except Exception as err:
            self.status = "save_failed"
            self.changed()
            raise HomeAssistantError(
                "Recommendation is held in memory but could not be saved"
            ) from err
        self.status = "ready"
        self.changed()

    def changed(self):
        """Refresh count-only entities; failures here never affect the decision."""
        try:
            if self.heating.data is not None:
                self.heating.async_set_updated_data(self.heating.data)
        except Exception:
            LOGGER.debug("Recommendation sensor refresh skipped")

    def quality(self):
        counts = dict.fromkeys(STATES, 0)
        for rec in self.data["recommendations"]:
            counts[rec["state"]] += 1
        recs = self.data["recommendations"]
        return {
            "status": self.status,
            "counts": counts,
            "latest_id": recs[-1]["id"] if recs else None,
            "eligible_for_evaluation": sum(
                1 for r in recs if r["state"] == "applied" and self.assess(r)["eligible"]
            ),
        }

    # Report intake -----------------------------------------------------------

    async def add_report(self, report):
        """Called by Advisor.run after a report is stored; failures never fail the run."""
        if not self.storage_ready:
            return []
        try:
            now = dt_util.utcnow()
            known = {r["id"] for r in self.data["recommendations"]}
            added = [r for r in extract(report, now) if r["id"] not in known]
            self.data["recommendations"].extend(added)
            self.bound(now)
            for rec in added:
                self.journal(rec, None, "proposed", origin="advisor")
            await self.persist()
            return added
        except Exception:
            LOGGER.warning("Recommendation store could not record report findings")
            return []

    # Transitions -------------------------------------------------------------

    def writable(self, rec_id):
        """Locate a recommendation for an owner transition; read-only storage refuses first."""
        if not self.storage_ready:
            raise HomeAssistantError("Recommendation storage is read-only until reload")
        return self.find(rec_id)

    def find(self, rec_id):
        rec = next((r for r in self.data["recommendations"] if r["id"] == rec_id), None)
        if rec is None:
            raise ServiceValidationError("Unknown recommendation ID")
        return rec

    def transition(self, rec, to, **fields):
        """Owner-driven state change. Never calls any control coordinator."""
        if to not in TRANSITIONS.get(rec["state"], set()):
            raise ServiceValidationError(f"Recommendation is {rec['state']}; cannot move to {to}")
        now = dt_util.utcnow().isoformat()
        previous = rec["state"]
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["state"] = to
        rec["updated_at"] = now
        rec["history"].append({"state": to, "at": now, "by": "owner"})
        self.journal(rec, previous, to, origin="user")
        return rec

    async def decide(self, rec_id, decision, note=None, defer_until=None):
        if decision not in DECISIONS:
            raise ServiceValidationError("Decision must be accepted, rejected or deferred")
        rec = self.writable(rec_id)
        fields = {"private_note": note}
        if decision == "deferred":
            fields["defer_until"] = (
                dt_util.as_utc(defer_until).isoformat()
                if isinstance(defer_until, datetime)
                else None
            )
        self.transition(rec, decision, **fields)
        await self.persist()
        return self.public(rec)

    async def mark_applied(self, rec_id, journal_event_id=None, intervention_note=None):
        rec = self.writable(rec_id)
        self.transition(
            rec,
            "applied",
            applied_at=dt_util.utcnow().isoformat(),
            applied_era=configuration_era(self.heating.config.get("control")),
            journal_event_id=journal_event_id,
            intervention_note=intervention_note,
        )
        await self.persist()
        return self.public(rec)

    async def evaluate(self, rec_id, outcome, window_start, window_end, note=None):
        if outcome not in OUTCOMES:
            raise ServiceValidationError("Unknown evaluation outcome")
        if window_end <= window_start:
            raise ServiceValidationError("Evaluation window must end after it starts")
        rec = self.writable(rec_id)
        assessment = self.assess(rec)
        self.transition(
            rec,
            "evaluated",
            outcome=outcome,
            evaluation_window={
                "start": dt_util.as_utc(window_start).isoformat(),
                "end": dt_util.as_utc(window_end).isoformat(),
            },
            evaluation_eligibility=assessment,
            evidence_type=EVIDENCE_TYPE,
            evaluation_note=note,
        )
        await self.persist()
        return self.public(rec)

    # Follow-up context -------------------------------------------------------

    def coverage(self, rec):
        analytics = getattr(self.heating, "analytics", None)
        if analytics is None:
            return None
        try:
            stats = analytics.report()["analysis"]["zone_stats"]
        except Exception:
            return None
        rooms = rec["scope"].get("room_ids") or list(stats)
        values = [
            stats[r]["coverage"]
            for r in rooms
            if r in stats and isinstance(stats[r].get("coverage"), (int, float))
        ]
        return min(values) if values else None

    def energy_context(self, rec):
        energy = getattr(self.heating, "energy", None)
        if energy is None:
            return None
        try:
            context = energy.comparability(since=_parse(rec.get("applied_at")))
        except Exception:
            return None
        return context if isinstance(context, dict) else None

    def assess(self, rec):
        try:
            era = configuration_era(self.heating.config.get("control"))
        except Exception:
            era = None
        return eligibility(rec, dt_util.utcnow(), self.coverage(rec), era, self.energy_context(rec))

    def journal(self, rec, previous, to, origin):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            rooms = rec["scope"].get("room_ids", [])
            journal.record(
                "recommendation",
                room_id=rooms[0] if len(rooms) == 1 else None,
                scope="system" if rec["scope"].get("system") or len(rooms) != 1 else rooms[0],
                origin=origin,
                data={
                    "recommendation_id": rec["id"],
                    "from": previous,
                    "to": to,
                    "outcome": rec.get("outcome"),
                },
            )
        except Exception:
            LOGGER.debug("Journal unavailable for recommendation event")

    def linked_interventions(self, rec):
        journal = getattr(self.heating, "journal", None)
        since = _parse(rec.get("applied_at"))
        if journal is None or since is None:
            return None
        try:
            rooms = rec["scope"].get("room_ids", [])
            if rooms and not rec["scope"].get("system"):
                events = []
                for room in rooms:
                    events.extend(
                        journal.events(kinds=INTERVENTION_KINDS, room_id=room, since=since)
                    )
            else:
                events = journal.events(kinds=INTERVENTION_KINDS, since=since)
            return len(events)
        except Exception:
            return None

    # Reads -------------------------------------------------------------------

    def public(self, rec, include_private=False):
        item = deepcopy(rec)
        if not include_private:
            for field in PRIVATE_FIELDS:
                item.pop(field, None)
        if rec["state"] in ("applied", "evaluated"):
            item["eligibility"] = self.assess(rec)
            count = self.linked_interventions(rec)
            if count is not None:
                item["linked_intervention_count"] = count
        return item

    def report_list(self, state=None, report_id=None, room_id=None, include_private=False):
        items = [
            self.public(r, include_private)
            for r in reversed(self.data["recommendations"])
            if (state is None or r["state"] == state)
            and (report_id is None or r["report_id"] == report_id)
            and (room_id is None or room_id in r["scope"].get("room_ids", []))
        ]
        return {
            **self.quality(),
            "evidence_type": EVIDENCE_TYPE,
            "note": "Accepting a recommendation authorises nothing; outcomes are associations, not causal proof.",
            "recommendations": items,
        }
