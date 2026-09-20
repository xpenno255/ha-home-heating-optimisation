"""Validated, bounded trial persistence; read-only on corruption, never blocks control."""

import json
import logging
from copy import deepcopy
from datetime import timedelta

from homeassistant.helpers.storage import Store

from .const import (
    ACTIVE_STATES,
    ALLOWED_PARAMETERS,
    MAX_STORE_BYTES,
    MAX_TRIALS,
    RETENTION_DAYS,
    SCHEMA,
    STATES,
    STORE_SUFFIX,
)

LOGGER = logging.getLogger(__name__)
REQUIRED_STR = ("id", "scope", "parameter", "created_at", "updated_at")
REQUIRED_NUM = ("baseline_value", "target_value", "rollback_value")


def _number(value):
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def allowlisted(scope, parameter):
    """The parameter must be on the closed allowlist for the scope kind."""
    kind = "boiler" if scope == "boiler" else "room"
    return parameter in ALLOWED_PARAMETERS[kind]


def structurally_valid(trial):
    return (
        isinstance(trial, dict)
        and all(isinstance(trial.get(k), str) for k in REQUIRED_STR)
        and all(_number(trial.get(k)) for k in REQUIRED_NUM)
        and trial.get("state") in STATES
        and isinstance(trial.get("duration_hours"), int)
        and isinstance(trial.get("bounds"), dict)
        and isinstance(trial.get("stop_criteria"), dict)
        and isinstance(trial.get("success_criteria"), dict)
        and isinstance(trial.get("history"), list)
        and all(
            isinstance(h, dict) and h.get("state") in STATES and isinstance(h.get("at"), str)
            for h in trial["history"]
        )
    )


def valid_trial(trial):
    """Well formed and on the allowlist; persisted fields are never trusted otherwise."""
    return structurally_valid(trial) and allowlisted(trial["scope"], trial["parameter"])


class TrialStore:
    def __init__(self, hass, entry_id):
        self.backend = Store(hass, SCHEMA, f"home_heating_optimisation.{entry_id}.{STORE_SUFFIX}")
        self.trials = []
        self.ready = True
        self.status = "ready"

    async def load(self, now):
        try:
            data = await self.backend.async_load()
            if data is None:
                return
            if (
                not isinstance(data, dict)
                or data.get("schema") != SCHEMA
                or not isinstance(data.get("trials"), list)
                or len(json.dumps(data).encode()) > MAX_STORE_BYTES
                or not all(structurally_valid(t) for t in data["trials"])
                or len({t["id"] for t in data["trials"]}) != len(data["trials"])
            ):
                raise ValueError("invalid_store")
            # A well-formed entry naming a parameter outside the allowlist is never
            # trusted: inactive ones are dropped; active ones are kept only so the
            # coordinator's write gate can refuse the rollback, flag the trial as
            # rollback_failed and raise a Repairs issue without writing anything.
            self.trials = []
            for trial in data["trials"]:
                if valid_trial(trial):
                    self.trials.append(trial)
                elif trial["state"] in ACTIVE_STATES:
                    LOGGER.error(
                        "Trial %s names non-allowlisted parameter %r; kept for refusal only",
                        trial["id"],
                        trial["parameter"],
                    )
                    self.trials.append(trial)
                else:
                    LOGGER.warning("Dropped trial %s with non-allowlisted parameter", trial["id"])
            self.bound(now)
        except Exception:
            # Keep the unreadable file for inspection; refuse writes until reload.
            LOGGER.warning("Trial storage is unreadable; trials are read-only until reload")
            self.trials = []
            self.ready = False
            self.status = "storage_read_only"

    def bound(self, now):
        cutoff = (now - timedelta(days=RETENTION_DAYS)).isoformat()
        kept = [t for t in self.trials if t["updated_at"] >= cutoff or t["state"] in ACTIVE_STATES]
        if len(kept) > MAX_TRIALS:
            active = [t for t in kept if t["state"] in ACTIVE_STATES]
            inactive = [t for t in kept if t["state"] not in ACTIVE_STATES]
            kept = inactive[len(kept) - MAX_TRIALS :] + active
            kept.sort(key=lambda t: t["created_at"])
        self.trials = kept

    async def save(self):
        """Persist; a failure is recorded, never raised into control paths."""
        if not self.ready:
            return False
        try:
            await self.backend.async_save({"schema": SCHEMA, "trials": deepcopy(self.trials)})
        except Exception:
            LOGGER.warning("Trial state is held in memory but could not be saved")
            self.status = "save_failed"
            return False
        self.status = "ready"
        return True
