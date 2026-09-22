"""Private DHW schedule state; read-only on corruption, never blocks room control."""

import logging
from copy import deepcopy

from homeassistant.helpers.storage import Store

LOGGER = logging.getLogger(__name__)
SCHEMA = 1
STORE_SUFFIX = "dhw_schedule"
EMPTY = {
    # Highest local date (ISO) already used for an elevated session.
    "consumed": None,
    # Configuration revision whose one-time normal-target write has been handled.
    "applied_revision": None,
    # Configuration revision paused by an external change; a new revision re-arms.
    "paused": None,
    # The one owned setpoint change still to be restored, if any.
    "transaction": None,
    "last": None,
}
PARAM_KEYS = ("setpoint", "overrun", "differential")


def _params(value):
    return isinstance(value, dict) and all(
        isinstance(value.get(k), (int, float)) and not isinstance(value.get(k), bool)
        for k in PARAM_KEYS
    )


def valid_transaction(txn):
    return (
        isinstance(txn, dict)
        and all(isinstance(txn.get(k), str) for k in ("id", "day", "entity", "deadline", "phase"))
        and _params(txn.get("original"))
        and _params(txn.get("restore"))
        and isinstance(txn.get("candidates"), list)
        and all(_params(c) for c in txn["candidates"])
    )


class DhwStore:
    def __init__(self, hass, entry_id):
        self.backend = Store(hass, SCHEMA, f"home_heating_optimisation.{entry_id}.{STORE_SUFFIX}")
        self.data = deepcopy(EMPTY)
        self.ready = True
        self.status = "ready"

    async def load(self):
        try:
            data = await self.backend.async_load()
            if data is None:
                return
            if (
                not isinstance(data, dict)
                or data.get("schema") != SCHEMA
                or not isinstance(data.get("state"), dict)
            ):
                raise ValueError("invalid_store")
            state = {**deepcopy(EMPTY), **{k: data["state"].get(k) for k in EMPTY}}
            if state["transaction"] is not None and not valid_transaction(state["transaction"]):
                raise ValueError("invalid_transaction")
            self.data = state
        except Exception:
            # Keep the file for inspection; the schedule makes no writes until reload.
            LOGGER.warning("DHW schedule storage is unreadable; DHW scheduling is paused")
            self.data = deepcopy(EMPTY)
            self.ready = False
            self.status = "storage_read_only"

    async def save(self):
        """Persist; a failure is recorded, never raised. Callers write nothing on False."""
        if not self.ready:
            return False
        try:
            await self.backend.async_save({"schema": SCHEMA, "state": deepcopy(self.data)})
        except Exception:
            LOGGER.warning("DHW schedule state could not be saved")
            self.status = "save_failed"
            return False
        self.status = "ready"
        return True
