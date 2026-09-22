"""Raise the cylinder target for one weekly session, then restore it.

Only ``ramses_cc.set_dhw_params`` is used, always with all three parameters.
Evohome keeps the DHW schedule, on/off decision and cutoff; no mode, boost or
schedule command is ever sent. A successful service call is only a request:
the controller's reply reaches HA about a minute later, and only that
observed state is treated as the current value.
"""

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta

from homeassistant.core import callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .policy import (
    ENTITY_FIELDS,
    TEMPORARY_MODES,
    Evidence,
    demand_value,
    dhw_config,
    next_window,
    params_equal,
    read_allowed,
    read_mode,
    read_params,
    session_due,
    temperature_value,
    usable,
)
from .store import DhwStore

LOGGER = logging.getLogger(__name__)
CALL_TIMEOUT = 30.0
TICK = timedelta(seconds=30)
# HA shows the controller's reply about 60 s after a change; never judge sooner.
SETTLE_SECONDS = 90
# A restore not observed after this long may be sent again; it is never re-sent blindly.
CONFIRM_SECONDS = 300
QUICK_ATTEMPTS = 3
SLOW_RETRY_SECONDS = 1800
MAX_ATTEMPTS = 6
# Unknown actuator state this long past the deadline raises a repair.
UNKNOWN_GRACE = timedelta(minutes=15)
ISSUE_RESTORE = "dhw_schedule_restore"
STATES = (
    "disabled",
    "armed",
    "paused",
    "elevated",
    "restoring",
    "recovery_pending",
    "storage_read_only",
    "misconfigured",
)


class DhwSchedule:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.cfg = dhw_config(heating.config)
        self.store = DhwStore(hass, entry.entry_id)
        self.listeners = []
        self.closed = False
        self.stopping = False
        self.armed_since = None
        self.evidence = None
        self.issue = False
        self.last_block = None
        self._unsub = []
        self._task = None
        self._step_lock = asyncio.Lock()
        # Monotonic time of the last request of any kind, and of the last restore request.
        self._written = None
        self._restore_sent = None

    # -- lifecycle ---------------------------------------------------------

    async def initialise(self):
        await self.store.load()
        txn = self.store.data["transaction"]
        if txn is not None:
            # A restart never resumes an elevated session; it is restored instead.
            if txn["phase"] != "restoring":
                txn["phase"] = "restoring"
                txn["outcome"] = "interrupted"
            # Judge nothing until a reply to any earlier request could have arrived.
            self._written = self._since(txn.get("written_at"), SETTLE_SECONDS)
            if txn.get("restore_sent_at"):
                self._restore_sent = self._since(txn["restore_sent_at"], SLOW_RETRY_SECONDS)

    def _since(self, stamp, cap):
        age = (dt_util.utcnow() - datetime.fromisoformat(stamp)).total_seconds() if stamp else 0
        return self._mono() - min(max(age, 0), cap)

    def start(self):
        self.armed_since = dt_util.utcnow()
        entities = [e for e in self._entities() if e]
        if entities:
            self._unsub.append(async_track_state_change_event(self.hass, entities, self._changed))
        self._unsub.append(async_track_time_interval(self.hass, self._changed, TICK))
        self._kick()

    @callback
    def close(self):
        self.closed = True
        while self._unsub:
            self._unsub.pop()()

    async def stop(self):
        """Stop scheduling and hand back any owned elevation while control is still up."""
        if self.closed:
            return
        self.stopping = True
        while self._unsub:
            self._unsub.pop()()
        async with self._step_lock:
            txn = self.store.data["transaction"]
            if txn is not None and txn["phase"] != "restoring":
                txn["phase"] = "restoring"
                txn["outcome"] = "interrupted"
                self.evidence = None
                await self.store.save()
                # One prompt restore from an observed owned value. A restore already in
                # flight is not re-sent; the next start confirms or retries it.
                await self._restore(txn, dt_util.utcnow(), force=True)
        self.close()

    def _mono(self):
        return time.monotonic()

    def _entities(self):
        txn = self.store.data["transaction"]
        own = [self._entity(txn)] if txn else []
        return {*own, *(self.cfg[k] for k in ENTITY_FIELDS)}

    @callback
    def _changed(self, _event=None):
        if self.closed or self.stopping:
            return
        self._observe()
        self._kick()

    def _kick(self):
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self.step(), "hho_dhw_schedule")

    # -- evaluation --------------------------------------------------------

    async def step(self):
        async with self._step_lock:
            if self.closed or self.stopping:
                return
            try:
                await self._step(dt_util.utcnow())
            finally:
                self._notify()

    async def _step(self, now):
        txn = self.store.data["transaction"]
        if txn is not None:
            await self._progress(txn, now)
            return
        cfg, data = self.cfg, self.store.data
        if not self.store.ready or not usable(cfg) or data["paused"] == cfg["revision"]:
            return
        if data["applied_revision"] != cfg["revision"]:
            await self._baseline(now)
            return
        due = session_due(
            now, cfg, dt_util.get_default_time_zone(), self.armed_since, data["consumed"]
        )
        if due is not None:
            await self._open(*due, now)

    def _state(self, entity):
        return self.hass.states.get(entity) if entity else None

    def _entity(self, txn):
        """The persisted actuator, following registry renames by its registry id."""
        if uid := txn.get("entity_uuid"):
            resolved = er.async_resolve_entity_id(er.async_get(self.hass), uid)
            if resolved:
                return resolved
        return txn["entity"]

    def _guard(self, entity, restore=False):
        controls = self.heating.controls
        if controls is None:
            return "control unavailable"
        reason = controls.dhw_guard_reason(entity, restore=restore)
        if reason is None and not restore and not self.store.ready:
            return "DHW schedule storage unavailable"
        return reason

    def _block(self, reason):
        if reason != self.last_block:
            self.last_block = reason
            self._journal("blocked", {"reason": reason})

    async def _baseline(self, now):
        """One-time normal-target write for a new revision; reuses the restore path."""
        entity = self.cfg["water_heater_entity"]
        params = read_params(self._state(entity))
        if params is None:
            self._block("water heater parameters unavailable")
            return
        normal = {**params, "setpoint": self.cfg["normal_target"]}
        if params_equal(params, normal):
            await self._finish(
                {"kind": "baseline", "revision": self.cfg["revision"], "day": None}, "baseline_ok"
            )
            return
        txn = self._transaction("baseline", entity, now, None, now, params, normal)
        txn["phase"] = "restoring"
        self.store.data["transaction"] = txn
        if not await self.store.save():
            self.store.data["transaction"] = None
            self._block("DHW schedule state could not be saved")
            return
        await self._restore(txn, now)

    def _transaction(self, kind, entity, now, day, deadline, original, restore, raised=None):
        registered = er.async_get(self.hass).async_get(entity)
        return {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "revision": self.cfg["revision"],
            "day": day.isoformat() if day else now.date().isoformat(),
            "entity": entity,
            "entity_uuid": registered.id if registered else None,
            "requested_at": now.isoformat(),
            "deadline": deadline.isoformat(),
            "phase": "raising",
            "original": original,
            "restore": restore,
            "raised": raised,
            "candidates": [p for p in (original, restore, raised) if p is not None],
            "attempts": 0,
            "written_at": None,
            "outcome": None,
        }

    async def _open(self, day, start, end, now):
        cfg = self.cfg
        entity = cfg["water_heater_entity"]
        if reason := self._guard(entity):
            self._block(reason)
            return
        state = self._state(entity)
        params = read_params(state)
        if params is None:
            self._block("water heater parameters unavailable")
            return
        if read_mode(state) in TEMPORARY_MODES:
            self._block("a manual DHW override is active")
            return
        if abs(params["setpoint"] - cfg["normal_target"]) >= 0.01:
            # Someone else changed the target; never overwrite it.
            await self._pause(day.isoformat(), "external_change")
            return
        raised = {**params, "setpoint": cfg["high_target"]}
        txn = self._transaction("elevation", entity, now, day, end, params, params, raised)
        # Durable intent before any command: the date is consumed even if the write fails.
        previous = dict(self.store.data)
        self.store.data["consumed"] = day.isoformat()
        self.store.data["transaction"] = txn
        if not await self.store.save():
            self.store.data = {**previous, "consumed": day.isoformat()}
            self._record(txn, "storage_failed")
            return
        self.evidence = Evidence()
        self._observe()
        result = await self._write(txn, raised, [params], restore=False)
        if result == "conflict":
            await self._end(txn, "external_change", pause=True)
            return
        if result in ("rejected", "blocked"):
            await self._end(txn, f"raise_{result}")
            return
        txn["phase"] = "raised"
        txn["ambiguous"] = result == "ambiguous"
        await self.store.save()

    @callback
    def _observe(self):
        txn = self.store.data["transaction"]
        if txn is None or self.evidence is None or txn["phase"] != "raised":
            return
        now = dt_util.utcnow()
        temp_state = self._state(self.cfg["cylinder_temp_entity"])
        temperature = temperature_value(temp_state, now)
        if temperature is not None:
            seen = getattr(temp_state, "last_reported", None) or temp_state.last_updated
            # A reading from before the request is not evidence from this session.
            if seen < datetime.fromisoformat(txn["requested_at"]):
                temperature = None
        self.evidence.observe(
            self._mono(),
            demand_value(self._state(self.cfg["demand_entity"]), now),
            temperature,
            read_allowed(self._state(self._entity(txn))),
            txn["raised"]["setpoint"],
        )

    async def _progress(self, txn, now):
        if txn["phase"] == "raised":
            if self.evidence is None:
                await self._begin_restore(txn, "interrupted")
                return
            self._observe()
            params = read_params(self._state(self._entity(txn)))
            if self._settled() and params is not None and not self._candidate(txn, params):
                await self._end(txn, "external_change", pause=True)
                return
            outcome = self.evidence.result(self._mono())
            if outcome is None and now >= datetime.fromisoformat(txn["deadline"]):
                outcome = self.evidence.deadline_outcome()
            if outcome is not None:
                await self._begin_restore(txn, outcome)
            return
        await self._restore(txn, now)

    def _settled(self, seconds=SETTLE_SECONDS):
        return self._written is None or self._mono() - self._written >= seconds

    def _candidate(self, txn, params):
        return any(params_equal(params, c) for c in txn["candidates"])

    async def _begin_restore(self, txn, outcome):
        txn["phase"] = "restoring"
        txn["outcome"] = outcome
        self.evidence = None
        await self.store.save()
        await self._restore(txn, dt_util.utcnow())

    async def _restore(self, txn, now, force=False):
        """Observe, then restore only from a value this transaction owns."""
        params = read_params(self._state(self._entity(txn)))
        if params is not None and params_equal(params, txn["restore"]):
            if self._settled() and not force:
                await self._end(txn, txn.get("outcome") or "baseline_ok")
            return
        if params is None:
            # Unknown state keeps recovery pending rather than guessing.
            if now >= datetime.fromisoformat(txn["deadline"]) + UNKNOWN_GRACE:
                self._raise_issue(txn)
            return
        if not self._candidate(txn, params):
            if self._settled() and not force:
                await self._end(txn, "external_change", pause=True)
            return
        if not force:
            since = None if self._restore_sent is None else self._mono() - self._restore_sent
            if txn["attempts"] >= QUICK_ATTEMPTS and (since is None or since >= CONFIRM_SECONDS):
                # The quick attempts went unconfirmed: tell the owner, keep retrying slowly.
                self._raise_issue(txn)
            wait = CONFIRM_SECONDS if txn["attempts"] < QUICK_ATTEMPTS else SLOW_RETRY_SECONDS
            if not self._settled() or (since is not None and since < wait):
                return
        if txn["attempts"] >= QUICK_ATTEMPTS:
            self._raise_issue(txn)
        if txn["attempts"] >= MAX_ATTEMPTS:
            return
        # A baseline is a new setpoint choice and takes the full guard; recovering an
        # owned elevation only needs the integration to be running.
        recovery = txn["kind"] != "baseline"
        result = await self._write(txn, txn["restore"], txn["candidates"], restore=recovery)
        if result == "conflict":
            await self._end(txn, "external_change", pause=True)
        elif result != "blocked":
            txn["attempts"] += 1
            self._restore_sent = self._mono()
            txn["restore_sent_at"] = _now()
            await self.store.save()

    async def _write(self, txn, target, expected, restore):
        """Revalidate under the shared controls lock, then send one bounded request."""
        controls = self.heating.controls
        entity = self._entity(txn)
        if controls is None:
            return "blocked"
        async with controls.lock:
            if reason := self._guard(entity, restore=restore):
                self._block(reason)
                return "blocked"
            params = read_params(self._state(entity))
            if params is None:
                return "blocked"
            if not any(params_equal(params, p) for p in expected):
                return "conflict"
            if not restore and read_mode(self._state(entity)) in TEMPORARY_MODES:
                return "blocked"
            request = {
                "entity_id": entity,
                "setpoint": target["setpoint"],
                "overrun": params["overrun"],
                "differential": params["differential"],
            }
            self._journal("command_requested", {**request, "transaction": txn["id"]})
            self._written = self._mono()
            txn["written_at"] = _now()
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    await self.hass.services.async_call(
                        "ramses_cc", "set_dhw_params", request, blocking=True
                    )
            except ServiceValidationError as err:
                self._journal("command_result", {"outcome": "rejected", "error": str(err)})
                return "rejected"
            except Exception as err:  # noqa: BLE001 - a timeout or RF fault may still apply
                self._journal("command_result", {"outcome": "ambiguous", "error": str(err)})
                return "ambiguous"
            # Sent to the integration only; the controller's reply is observed later.
            self._journal("command_result", {"outcome": "sent", "transaction": txn["id"]})
            return "sent"

    async def _pause(self, day, outcome):
        self.store.data["paused"] = self.cfg["revision"]
        self.store.data["consumed"] = max(filter(None, (self.store.data["consumed"], day)))
        self.store.data["last"] = {"outcome": outcome, "day": day, "at": _now()}
        await self.store.save()
        self._journal("paused", {"outcome": outcome, "day": day})

    async def _end(self, txn, outcome, pause=False):
        if pause:
            self.store.data["paused"] = txn["revision"]
        await self._finish(txn, outcome)

    async def _finish(self, txn, outcome):
        data = self.store.data
        data["transaction"] = None
        if txn["kind"] == "baseline" and outcome == "baseline_ok":
            data["applied_revision"] = txn["revision"]
            if data["paused"] == txn["revision"]:
                data["paused"] = None
        self._record(txn, outcome)
        self._written = self._restore_sent = None
        self.evidence = None
        self.last_block = None
        await self.store.save()
        if self.issue:
            ir.async_delete_issue(self.hass, DOMAIN, ISSUE_RESTORE)
            self.issue = False

    def _record(self, txn, outcome):
        self.store.data["last"] = {
            "kind": txn["kind"],
            "day": txn.get("day"),
            "outcome": outcome,
            "at": _now(),
        }
        self._journal("session", {"kind": txn["kind"], "outcome": outcome, "day": txn.get("day")})

    def _raise_issue(self, txn):
        if self.issue:
            return
        self.issue = True
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            ISSUE_RESTORE,
            is_fixable=False,
            severity=ir.IssueSeverity.ERROR,
            translation_key=ISSUE_RESTORE,
            translation_placeholders={
                "entity": self._entity(txn),
                "target": str(txn["restore"]["setpoint"]),
            },
        )

    def _journal(self, event, data):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "dhw_schedule", scope="system", origin="controller", data={"event": event, **data}
            )
        except Exception:  # noqa: BLE001
            pass

    def _notify(self):
        for listener in list(self.listeners):
            listener()

    # -- status ------------------------------------------------------------

    @property
    def uncertain(self):
        """A requested target change is not yet confirmed by an observed reply."""
        return self.store.data["transaction"] is not None

    @property
    def status(self):
        txn = self.store.data["transaction"]
        if txn is not None:
            if self.issue:
                return "recovery_pending"
            return "elevated" if txn["phase"] == "raised" else "restoring"
        if not self.store.ready:
            return "storage_read_only"
        if not self.cfg["enabled"]:
            return "disabled"
        if not usable(self.cfg):
            return "misconfigured"
        if self.store.data["paused"] == self.cfg["revision"]:
            return "paused"
        return "armed"

    def report(self):
        txn = self.store.data["transaction"]
        upcoming = None
        if self.status == "armed":
            upcoming = next_window(dt_util.utcnow(), self.cfg, dt_util.get_default_time_zone())
        return {
            "status": self.status,
            "normal_target": self.cfg["normal_target"],
            "high_target": self.cfg["high_target"],
            "weekdays": self.cfg["weekdays"],
            "window": f"{self.cfg['window_start'][:5]}-{self.cfg['window_end'][:5]}",
            "next_window": upcoming.isoformat() if upcoming else None,
            "charging_seen": bool(self.evidence and self.evidence.charged),
            "target_reached": bool(self.evidence and self.evidence.reached),
            "transaction": None
            if txn is None
            else {k: txn.get(k) for k in ("kind", "day", "phase", "deadline", "attempts")},
            "last": self.store.data["last"],
            "blocked": self.last_block,
            "storage": self.store.status,
            "meaning": "requested setpoint changes; controller replies are observed, not assumed",
        }


def _now():
    return dt_util.utcnow().isoformat()
