"""Owner-approved bounded trials of allowlisted tunables with automatic rollback.

A trial changes exactly one allowlisted tunable through the owning control
coordinator's ``set_tunable`` and nothing else. It never changes a mode, an enable
switch, DHW protection or any actuator; the existing controllers keep deciding and
writing exactly as before. Proposals only come from the ``propose_trial`` service;
advisor or AI output never creates or starts one. Every running trial is rolled
back on expiry, on any safety condition, on ``stop_trial``, on unload and at the
next startup. Outcomes are recorded as associations, never causal proof.
"""

import asyncio
import logging
import math
import uuid
from copy import deepcopy
from datetime import timedelta

from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .const import (
    ACTIVE_STATES,
    ALLOWED_PARAMETERS,
    DEFICIT_ALLOWANCE_KH,
    EVALUABLE_STATES,
    EVIDENCE_TYPE,
    EXCLUDED_NOTE,
    INTERRUPTED_START_SECONDS,
    MAX_COMFORT_FLOOR_C,
    MAX_DURATION_HOURS,
    MAX_ROLLBACK_ATTEMPTS,
    METRIC_KEYS,
    MIN_COMFORT_FLOOR_C,
    MIN_DURATION_HOURS,
    OUTCOMES,
    OVERSHOOT_ALLOWANCE_KH,
    PRIVATE_FIELDS,
    STATES,
    TICK_SECONDS,
    TRANSITIONS,
)
from .store import TrialStore

LOGGER = logging.getLogger(__name__)
ISSUE_ROLLBACK_FAILED = "trial_rollback_failed"
TOLERANCE = 1e-6
# States in which the trial value is, or may be, applied to the controller.
APPLIED_STATES = ("starting", "running")


def parameter_spec(scope, parameter):
    """Allowlist lookup; anything not listed is rejected before any coordinator call."""
    kind = "boiler" if scope == "boiler" else "room"
    spec = ALLOWED_PARAMETERS[kind].get(parameter)
    if spec is None:
        raise ServiceValidationError(
            f"Parameter {parameter!r} is not permitted for a {kind} trial. {EXCLUDED_NOTE}"
        )
    return spec


def check_bounds(spec, baseline, target):
    if not (spec["min"] <= target <= spec["max"]):
        raise ServiceValidationError(f"Target must be between {spec['min']} and {spec['max']}")
    if abs(target - baseline) > spec["max_step"] + TOLERANCE:
        raise ServiceValidationError(
            f"Target may differ from the baseline {baseline} by at most {spec['max_step']}"
        )
    if abs(target - baseline) <= TOLERANCE:
        raise ServiceValidationError("Target equals the current value; nothing to trial")


def _finite(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse(value):
    if not isinstance(value, str):
        return None
    parsed = dt_util.parse_datetime(value)
    return dt_util.as_utc(parsed) if parsed else None


class Trials:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.store = TrialStore(hass, entry.entry_id)
        self.lock = asyncio.Lock()
        self.unsub = None
        self.closed = False

    # Lifecycle ---------------------------------------------------------------

    async def initialise(self):
        """Load, then roll back anything left running: a restart never resumes a trial."""
        try:
            async with self.lock:
                await self.store.load(dt_util.utcnow())
                for trial in [t for t in self.store.trials if t["state"] in ACTIVE_STATES]:
                    await self._rollback(trial, "stopped", "restart", origin="controller")
                await self.store.save()
        except Exception:
            LOGGER.exception("Trial startup handling failed; heating control is unaffected")

    def start(self):
        if self.unsub is None:
            self.unsub = async_track_time_interval(
                self.hass, self._tick, timedelta(seconds=TICK_SECONDS)
            )

    async def stop(self, _event=None):
        if self.closed:
            return
        self.closed = True
        if self.unsub:
            self.unsub()
            self.unsub = None
        try:
            async with self.lock:
                for trial in [t for t in self.store.trials if t["state"] in APPLIED_STATES]:
                    await self._rollback(trial, "stopped", "unload", origin="controller")
                await self.store.save()
        except Exception:
            LOGGER.exception("Trial shutdown rollback failed")

    # Helpers -----------------------------------------------------------------

    @property
    def controls(self):
        return self.heating.controls

    def coordinator(self, scope):
        controls = self.controls
        if controls is None or not controls.config or controls.boiler is None:
            raise ServiceValidationError("Configure and load consolidated controls first")
        if scope == "boiler":
            return controls.boiler
        if scope not in controls.rooms:
            raise ServiceValidationError("Scope must be a controlled room ID or 'boiler'")
        return controls.rooms[scope]

    def find(self, trial_id):
        trial = next((t for t in self.store.trials if t["id"] == trial_id), None)
        if trial is None:
            raise ServiceValidationError("Unknown trial ID")
        return trial

    def writable(self, trial_id):
        if not self.store.ready:
            raise HomeAssistantError("Trial storage is read-only until reload")
        return self.find(trial_id)

    def rooms_for(self, trial):
        if trial["scope"] == "boiler":
            controls = self.controls
            return list(controls.rooms) if controls and controls.config else []
        return [trial["scope"]]

    def metrics(self, rooms):
        """Current analytics window metrics per room, or None where unavailable."""
        analytics = getattr(self.heating, "analytics", None)
        result = {}
        stats = {}
        if analytics is not None:
            try:
                stats = analytics.report()["analysis"]["zone_stats"]
            except Exception:
                stats = {}
        for room in rooms:
            zone = stats.get(room) or {}
            result[room] = {
                key: zone.get(key) if isinstance(zone.get(key), (int, float)) else None
                for key in METRIC_KEYS
            }
        return result

    def comfort(self, rooms):
        """Measured air and estimated operative comfort, labelled separately."""
        out = {"air_temp": {}, "operative_temp": {}}
        controls = self.controls
        for room in rooms:
            c = controls.rooms.get(room) if controls and controls.config else None
            data = getattr(c, "data", None)
            out["air_temp"][room] = getattr(data, "air_temp", None)
            out["operative_temp"][room] = getattr(data, "operative_temp", None)
        return out

    def transition(self, trial, to, origin, **fields):
        if to not in TRANSITIONS.get(trial["state"], set()):
            raise ServiceValidationError(f"Trial is {trial['state']}; cannot move to {to}")
        now = dt_util.utcnow().isoformat()
        previous = trial["state"]
        trial.update({k: v for k, v in fields.items() if v is not None})
        trial["state"] = to
        trial["updated_at"] = now
        trial["history"].append(
            {"state": to, "at": now, "by": "owner" if origin == "user" else "controller"}
        )
        self.journal(trial, previous, to, origin)
        self.changed()
        return trial

    def journal(self, trial, previous, to, origin, **extra):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "trial",
                room_id=None if trial["scope"] == "boiler" else trial["scope"],
                scope=trial["scope"],
                origin=origin,
                data={
                    "trial_id": trial["id"],
                    "parameter": trial["parameter"],
                    "from": previous,
                    "to": to,
                    "baseline_value": trial["baseline_value"],
                    "target_value": trial["target_value"],
                    "reason": trial.get("stop_reason"),
                    **extra,
                },
            )
        except Exception:
            LOGGER.debug("Journal unavailable for trial event")

    def changed(self):
        try:
            if self.heating.data is not None:
                self.heating.async_set_updated_data(self.heating.data)
        except Exception:
            LOGGER.debug("Trial sensor refresh skipped")

    async def persist(self):
        if not await self.store.save():
            raise HomeAssistantError("Trial state is held in memory but could not be saved")

    # Proposal ----------------------------------------------------------------

    async def propose(
        self,
        scope,
        parameter,
        target_value,
        rationale,
        duration_hours,
        comfort_floor_c=None,
        recommendation_id=None,
    ):
        """The only creation path. Captures baseline, bounds, criteria and rollback."""
        if not self.store.ready:
            raise HomeAssistantError("Trial storage is read-only until reload")
        coordinator = self.coordinator(scope)
        spec = parameter_spec(scope, parameter)
        if not isinstance(duration_hours, int) or not (
            MIN_DURATION_HOURS <= duration_hours <= MAX_DURATION_HOURS
        ):
            raise ServiceValidationError(
                f"Duration must be {MIN_DURATION_HOURS} to {MAX_DURATION_HOURS} hours"
            )
        if comfort_floor_c is not None and not (
            MIN_COMFORT_FLOOR_C <= comfort_floor_c <= MAX_COMFORT_FLOOR_C
        ):
            raise ServiceValidationError(
                f"Comfort floor must be {MIN_COMFORT_FLOOR_C} to {MAX_COMFORT_FLOOR_C} °C"
            )
        if recommendation_id is not None:
            recommendations = getattr(
                getattr(self.heating, "advisor", None), "recommendations", None
            )
            if recommendations is None:
                raise ServiceValidationError("Recommendations are unavailable")
            recommendations.find(recommendation_id)
        baseline = float(coordinator.get_tunable(parameter))
        target = float(target_value)
        check_bounds(spec, baseline, target)
        rooms = list(self.controls.rooms) if scope == "boiler" else [scope]
        metrics = self.metrics(rooms)
        now = dt_util.utcnow()
        at = now.isoformat()
        trial = {
            "id": uuid.uuid4().hex[:16],
            "scope": scope,
            "parameter": parameter,
            "baseline_value": baseline,
            "target_value": target,
            "rollback_value": baseline,
            "bounds": {
                "min": spec["min"],
                "max": spec["max"],
                "max_step": spec["max_step"],
                "unit": spec["unit"],
            },
            "duration_hours": duration_hours,
            "comfort_floor_c": comfort_floor_c,
            "recommendation_id": recommendation_id,
            "private_rationale": rationale,
            "baseline_metrics": metrics,
            "success_criteria": {
                "rooms": rooms,
                "deficit_degree_hours_not_above": {
                    r: metrics[r]["deficit_degree_hours"] for r in rooms
                },
                "overshoot_degree_hours_not_above": {
                    r: metrics[r]["overshoot_degree_hours"] for r in rooms
                },
                "within_band_not_below": {r: metrics[r]["within_band"] for r in rooms},
                "evidence_type": EVIDENCE_TYPE,
            },
            "stop_criteria": {
                "rooms": rooms,
                "max_deficit_degree_hours": {
                    r: (metrics[r]["deficit_degree_hours"] or 0.0) + DEFICIT_ALLOWANCE_KH
                    for r in rooms
                },
                "max_overshoot_degree_hours": {
                    r: (metrics[r]["overshoot_degree_hours"] or 0.0) + OVERSHOOT_ALLOWANCE_KH
                    for r in rooms
                },
                "comfort_floor_c": comfort_floor_c,
                "manual_override": "stop",
                "mode_leaves_active_or_auto": "stop",
                "source_stale_or_guarded": "stop",
                "dhw_active": "note_only",
            },
            "expires_at": None,
            "state": "proposed",
            "created_at": at,
            "updated_at": at,
            "history": [{"state": "proposed", "at": at, "by": "owner"}],
        }
        self.store.trials.append(trial)
        self.store.bound(now)
        self.journal(trial, None, "proposed", "user")
        self.changed()
        await self.persist()
        return self.public(trial)

    # Approval and start ------------------------------------------------------

    def readiness(self, trial):
        """Every reason a trial may not run now; a trial never activates control."""
        controls = self.controls
        if controls is None or not controls.config or controls.boiler is None:
            return "consolidated controls are not configured"
        if controls.settings.get("ownership") != "ready":
            return "complete legacy handover first"
        scope = trial["scope"]
        if scope == "boiler":
            if controls.boiler.override != "auto":
                return "boiler must already be in auto; a trial never activates control"
            if getattr(controls.boiler.data, "manual_hold_active", False):
                return "a boiler manual hold is active; a trial never clears a hold"
        else:
            if scope not in controls.rooms:
                return "room is no longer controlled"
            if controls.rooms[scope].mode != "active":
                return "room must already be active; a trial never activates control"
        if any(t["state"] in ACTIVE_STATES and t["id"] != trial["id"] for t in self.store.trials):
            return "another trial is running; one change at a time"
        reason = controls.guard_reason(scope)
        if reason:
            return reason
        return None

    async def approve(self, trial_id):
        async with self.lock:
            trial = self.writable(trial_id)
            if trial["state"] != "proposed":
                raise ServiceValidationError(
                    f"Trial is {trial['state']}; only proposals are approved"
                )
            if reason := self.readiness(trial):
                raise ServiceValidationError(reason)
            self.transition(trial, "approved", "user", approved_at=dt_util.utcnow().isoformat())
            await self.persist()
        return self.public(trial)

    async def reject(self, trial_id, note=None):
        trial = self.writable(trial_id)
        self.transition(trial, "rejected", "user", private_note=note)
        await self.persist()
        return self.public(trial)

    async def start_trial(self, trial_id):
        """Persist the rollback intent first, then apply the value through set_tunable.

        Order matters: the trial is saved as ``starting`` (with baseline, rollback
        value and expiry) before the controller store changes, so a crash between the
        two leaves a supervised trial that the next startup rolls back, never an
        unsupervised changed value. The apply-and-persist section is shielded from
        cancellation and re-checks readiness and the baseline under the controls lock
        immediately before the write; if the caller is cancelled after the write, the
        baseline is restored at once.
        """
        async with self.lock:
            trial = self.writable(trial_id)
            if trial["state"] != "approved":
                raise ServiceValidationError(f"Trial is {trial['state']}; approve it first")
            if reason := self.readiness(trial):
                raise ServiceValidationError(reason)
            scope, parameter = trial["scope"], trial["parameter"]
            target = float(trial["target_value"])
            coordinator = self.gate(scope, parameter, target)
            before = float(coordinator.get_tunable(parameter))
            check_bounds(trial["bounds"], before, target)
            if abs(before - trial["baseline_value"]) > TOLERANCE:
                raise ServiceValidationError(
                    "Current value differs from the proposal baseline; propose again"
                )
            self.gate(scope, parameter, before)
            now = dt_util.utcnow()
            snapshot = deepcopy(trial)
            self.transition(
                trial,
                "starting",
                "user",
                baseline_value=before,
                rollback_value=before,
                started_at=now.isoformat(),
                expires_at=(now + timedelta(hours=trial["duration_hours"])).isoformat(),
            )
            if not await self.store.save():
                trial.clear()
                trial.update(snapshot)
                self.journal(trial, "starting", "approved", "controller", note="not_persisted")
                self.changed()
                raise ServiceValidationError(
                    "Trial state could not be persisted; nothing was changed"
                )
            written = []
            inner = asyncio.ensure_future(
                self._activate(trial, coordinator, scope, parameter, before, target, written)
            )
            try:
                await asyncio.shield(inner)
            except asyncio.CancelledError:
                # The caller went away; let the shielded section finish so nothing is
                # half-done, then restore the baseline rather than leave it applied.
                try:
                    await inner
                except Exception:  # noqa: BLE001
                    pass
                if written and trial["state"] in APPLIED_STATES:
                    await self._rollback(trial, "stopped", "interrupted_start")
                    await self.store.save()
                raise
            except Exception as err:
                if not written:
                    raise
                # Restore immediately and refuse; the trial never reaches running.
                await self._rollback(trial, "stopped", "start_failed", origin="controller")
                await self.store.save()
                raise HomeAssistantError(f"Trial start failed; baseline restored: {err}") from err
        return self.public(trial)

    async def _activate(self, trial, coordinator, scope, parameter, before, target, written):
        """Recheck under the controls lock, write, verify, persist ``running``.

        *written* is appended to the moment a write is attempted so the caller knows a
        rollback may be needed. A recheck failure ends the saved intent as ``stopped``
        (reason ``conditions_changed``) and writes nothing.
        """
        async with self.controls.lock:
            reason = self.readiness(trial)
            current = float(coordinator.get_tunable(parameter))
            if reason is None and abs(current - before) > TOLERANCE:
                reason = "current value changed since the start was requested"
            if reason:
                trial["stop_reason"] = "conditions_changed"
                trial["stop_detail"] = {"reason": reason}
                trial["ended_at"] = dt_util.utcnow().isoformat()
                self.transition(trial, "stopped", "controller")
                await self.store.save()
                raise ServiceValidationError(f"Trial not started; conditions changed: {reason}")
            written.append(True)
            await self._tuning_write(scope, parameter, target)
            after = float(coordinator.get_tunable(parameter))
        self.transition(
            trial,
            "running",
            "user",
            applied={"before": before, "after": after, "control_store_saved": True},
        )
        await self.persist()

    # Tuning write gate ---------------------------------------------------------

    def gate(self, scope, parameter, value):
        """The only admission check for a tunable write; persisted fields are never trusted.

        Returns the owning coordinator or raises ServiceValidationError. The scope must
        be a controlled room or "boiler", the parameter must be on the allowlist for
        that scope and the value must be finite and within the allowlisted range.
        """
        coordinator = self.coordinator(scope)
        spec = parameter_spec(scope, parameter)
        if not _finite(value) or not (spec["min"] <= value <= spec["max"]):
            raise ServiceValidationError(
                f"Value {value!r} for {parameter} is outside {spec['min']} to {spec['max']}"
            )
        return coordinator

    async def _tuning_write(self, scope, parameter, value):
        """Gate, apply through set_tunable, persist the controller store, verify readback.

        Every trial write (start, rollback, restart rollback) goes through here. A
        gate refusal raises ServiceValidationError before anything is written; a store
        save or readback failure raises HomeAssistantError after the in-memory write.
        Returns the coordinator written to.
        """
        coordinator = self.gate(scope, parameter, value)
        coordinator.set_tunable(parameter, float(value))
        try:
            await coordinator._store.async_save()
        except Exception as err:
            raise HomeAssistantError("Control store save failed after a tunable change") from err
        if not getattr(coordinator._store, "ready", True):
            raise HomeAssistantError("Control store is not writable after a tunable change")
        after = float(coordinator.get_tunable(parameter))
        if abs(after - float(value)) > TOLERANCE:
            raise HomeAssistantError("Tunable value did not read back")
        return coordinator

    # Running -----------------------------------------------------------------

    async def _tick(self, _now=None):
        if self.closed:
            return
        try:
            async with self.lock:
                changed = False
                now = dt_util.utcnow()
                for trial in [t for t in self.store.trials if t["state"] in APPLIED_STATES]:
                    # A starting trial is supervised exactly like a running one.
                    changed = await self._check(trial) or changed
                    if trial["state"] == "starting" and self.start_interrupted(trial, now):
                        changed = (
                            await self._rollback(trial, "stopped", "interrupted_start") or changed
                        )
                for trial in [t for t in self.store.trials if t["state"] == "rollback_failed"]:
                    if trial.get("rollback_attempts", 0) >= MAX_ROLLBACK_ATTEMPTS:
                        continue  # give up retrying; the Repairs issue stays until stopped
                    changed = (
                        await self._rollback(trial, "rolled_back", trial["stop_reason"]) or changed
                    )
                if changed:
                    await self.store.save()
        except Exception:
            LOGGER.exception("Trial supervision tick failed")

    def start_interrupted(self, trial, now):
        """A trial left ``starting`` past the grace period never finished its start."""
        started = _parse(trial.get("started_at"))
        return started is None or (now - started).total_seconds() > INTERRUPTED_START_SECONDS

    async def _check(self, trial):
        """Roll back on the first breached condition; DHW activity is only noted."""
        now = dt_util.utcnow()
        controls = self.controls
        scope = trial["scope"]
        if controls is None or controls.closed or not controls.ready:
            return await self._rollback(trial, "stopped", "controls_unavailable")
        expires = _parse(trial.get("expires_at"))
        if expires is not None and now >= expires:
            return await self._rollback(trial, "expired", "duration_elapsed")
        coordinator = controls.boiler if scope == "boiler" else controls.rooms.get(scope)
        if coordinator is None:
            return await self._rollback(trial, "stopped", "scope_removed")
        mode = coordinator.override if scope == "boiler" else coordinator.mode
        if mode not in ("auto", "active"):
            return await self._rollback(trial, "stopped", "mode_changed")
        if scope == "boiler":
            if getattr(coordinator.data, "manual_hold_active", False):
                # Roll back only; the hold itself is never touched.
                return await self._rollback(trial, "stopped", "manual_hold")
        else:
            try:
                manual = coordinator._memory().manual_setpoint
            except Exception:
                manual = None
            if manual is not None:
                return await self._rollback(trial, "stopped", "manual_override")
        if reason := controls.guard_reason(scope):
            return await self._rollback(trial, "stopped", f"guard: {reason}")
        rooms = self.rooms_for(trial)
        floor = trial.get("comfort_floor_c")
        if floor is not None:
            for room, air in self.comfort(rooms)["air_temp"].items():
                if isinstance(air, (int, float)) and air < floor:
                    return await self._rollback(
                        trial, "stopped", "comfort_floor", room=room, air_temp=air
                    )
        criteria = trial["stop_criteria"]
        for room, values in self.metrics(rooms).items():
            for metric, key in (
                ("deficit_degree_hours", "max_deficit_degree_hours"),
                ("overshoot_degree_hours", "max_overshoot_degree_hours"),
            ):
                limit = criteria.get(key, {}).get(room)
                value = values.get(metric)
                if isinstance(limit, (int, float)) and isinstance(value, (int, float)):
                    if value > limit:
                        return await self._rollback(
                            trial, "stopped", f"{metric}_exceeded", room=room, value=value
                        )
        if scope == "boiler" and getattr(controls.boiler.data, "dhw_active", False):
            if not trial.get("dhw_active_observed"):
                trial["dhw_active_observed"] = True
                trial["updated_at"] = now.isoformat()
                self.journal(
                    trial, trial["state"], trial["state"], "controller", note="dhw_active_noted"
                )
                return True
        return False

    async def _rollback(self, trial, to, reason, origin="controller", **detail):
        """Restore the baseline through the tuning write gate; persist and verify.

        Success requires the gate to admit the write, the controller store save to
        succeed and the readback to match. Anything else is ``rollback_failed`` with a
        Repairs issue; the supervision tick retries once a minute up to
        MAX_ROLLBACK_ATTEMPTS and the owner can always ``stop_trial`` again.
        """
        trial["stop_reason"] = reason
        if detail:
            trial["stop_detail"] = detail
        trial.setdefault("ended_at", dt_util.utcnow().isoformat())
        trial["rollback_attempts"] = trial.get("rollback_attempts", 0) + 1
        verified = False
        try:
            await self._tuning_write(trial["scope"], trial["parameter"], trial["rollback_value"])
            verified = True
            trial.pop("rollback_error", None)
        except ServiceValidationError as err:
            LOGGER.error("Trial rollback refused by the tuning gate: %s", err)
            trial["rollback_error"] = str(err)
        except Exception as err:
            LOGGER.exception("Trial rollback failed")
            trial["rollback_error"] = str(err)
        trial["rollback_verified"] = verified
        if verified:
            ir.async_delete_issue(self.hass, DOMAIN, f"{ISSUE_ROLLBACK_FAILED}_{trial['id']}")
            self.transition(
                trial, "rolled_back" if trial["state"] == "rollback_failed" else to, origin
            )
        else:
            if trial["state"] != "rollback_failed":
                self.transition(trial, "rollback_failed", origin)
            else:
                trial["updated_at"] = dt_util.utcnow().isoformat()
                self.journal(trial, "rollback_failed", "rollback_failed", origin, note="retry")
                self.changed()
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"{ISSUE_ROLLBACK_FAILED}_{trial['id']}",
                is_fixable=False,
                severity=ir.IssueSeverity.ERROR,
                translation_key=ISSUE_ROLLBACK_FAILED,
                translation_placeholders={
                    "scope": trial["scope"],
                    "parameter": trial["parameter"],
                    "value": str(trial["rollback_value"]),
                },
            )
        return True

    async def stop_trial(self, trial_id, complete=False, note=None):
        async with self.lock:
            trial = self.writable(trial_id)
            if trial["state"] not in ACTIVE_STATES:
                raise ServiceValidationError(f"Trial is {trial['state']}; nothing to stop")
            to = "completed" if complete and trial["state"] == "running" else "stopped"
            await self._rollback(trial, to, "owner", origin="user")
            if note:
                trial["private_note"] = note
            await self.persist()
        return self.public(trial)

    # Evaluation --------------------------------------------------------------

    async def evaluate(self, trial_id, outcome, note=None):
        if outcome not in OUTCOMES:
            raise ServiceValidationError("Unknown evaluation outcome")
        trial = self.writable(trial_id)
        if trial["state"] not in EVALUABLE_STATES:
            raise ServiceValidationError(f"Trial is {trial['state']}; evaluate only ended trials")
        rooms = self.rooms_for(trial)
        now = dt_util.utcnow().isoformat()
        trial["evaluation"] = {
            "outcome": outcome,
            "evidence_type": EVIDENCE_TYPE,
            "at": now,
            "window": {"start": trial.get("started_at"), "end": trial.get("ended_at")},
            "predeclared": {
                "success_criteria": deepcopy(trial["success_criteria"]),
                "stop_criteria": deepcopy(trial["stop_criteria"]),
            },
            "baseline_metrics": deepcopy(trial.get("baseline_metrics", {})),
            "measured_metrics": self.metrics(rooms),
            "comfort": {
                "air_temp": {
                    "meaning": "measured room air temperature",
                    "values": self.comfort(rooms)["air_temp"],
                },
                "operative_temp": {
                    "meaning": "estimated operative comfort from the steady-state model; not measured",
                    "values": self.comfort(rooms)["operative_temp"],
                },
            },
            "note": (
                "Metrics are analytics-window totals that include time outside the trial; "
                "differences are associations, not causal effects."
            ),
        }
        if note:
            trial["private_note"] = note
        trial["updated_at"] = now
        self.journal(trial, trial["state"], trial["state"], "user", outcome=outcome)
        self.changed()
        await self.persist()
        return self.public(trial)

    # Reads -------------------------------------------------------------------

    def public(self, trial, include_private=False):
        item = deepcopy(trial)
        if not include_private:
            for field in PRIVATE_FIELDS:
                item.pop(field, None)
        return item

    def quality(self):
        counts = dict.fromkeys(STATES, 0)
        for trial in self.store.trials:
            counts[trial["state"]] += 1
        running = next((t for t in self.store.trials if t["state"] in APPLIED_STATES), None)
        return {
            "status": self.store.status,
            "counts": counts,
            "running_scope": running["scope"] if running else None,
            "running_parameter": running["parameter"] if running else None,
            "expires_at": running.get("expires_at") if running else None,
        }

    def report_list(self, state=None, scope=None, include_private=False):
        return {
            **self.quality(),
            "allowed_parameters": deepcopy(ALLOWED_PARAMETERS),
            "excluded": EXCLUDED_NOTE,
            "evidence_type": EVIDENCE_TYPE,
            "trials": [
                self.public(t, include_private)
                for t in reversed(self.store.trials)
                if (state is None or t["state"] == state) and (scope is None or t["scope"] == scope)
            ],
        }
