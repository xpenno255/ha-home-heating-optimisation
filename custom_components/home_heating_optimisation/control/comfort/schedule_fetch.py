"""Bounded background RF schedule requests, shared fairly between rooms."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from homeassistant.core import SupportsResponse
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)


class ScheduleFetcher:
    """Keep optional schedule downloads out of the actuator update path."""

    def __init__(self, hass, store):
        self.hass = hass
        self.store = store
        self.task = None
        self.closed = False

    def request(self, entity_id, lock, cache_available):
        try:
            self._request(entity_id, lock, cache_available)
        except Exception:
            # Bad optional download metadata must not abort an actuator cycle.
            _LOGGER.debug("Cannot queue RF schedule fetch for %s", entity_id, exc_info=True)

    def _failure_count(self):
        try:
            return min(5, max(0, int(self.store.get("ramses_schedule_failures", 0))))
        except TypeError, ValueError, OverflowError:
            return 0

    def _request(self, entity_id, lock, cache_available):
        if self.closed or self.task is not None and not self.task.done():
            return
        state = self.hass.states.get(entity_id)
        if (
            state is None
            or state.state in ("unavailable", "unknown")
            or not self.hass.services.has_service("ramses_cc", "get_zone_schedule")
        ):
            return
        failures = self._failure_count()
        retry = (
            timedelta(hours=24)
            if cache_available()
            else timedelta(minutes=min(60, 5 * 2 ** max(0, failures - 1)))
        )
        last = dt_util.parse_datetime(str(self.store.get("ramses_schedule_requested_at", "")))
        if last and dt_util.utcnow() - dt_util.as_utc(last) < retry:
            return
        self.task = self.hass.async_create_background_task(
            self._fetch(entity_id, lock),
            f"HHO schedule {entity_id}",
        )

    async def _fetch(self, entity_id, lock):
        # Radio schedule transfers are multi-message operations. Nine rooms must
        # not start transfers together when the integration is restored.
        async with lock:
            state = self.hass.states.get(entity_id)
            if self.closed or state is None or state.state in ("unavailable", "unknown"):
                return
            started = dt_util.utcnow()
            self.store.set("ramses_schedule_requested_at", started.isoformat())
            try:
                response_supported = (
                    self.hass.services.supports_response("ramses_cc", "get_zone_schedule")
                    is not SupportsResponse.NONE
                )
                async with asyncio.timeout(45):
                    response = await self.hass.services.async_call(
                        "ramses_cc",
                        "get_zone_schedule",
                        {"entity_id": entity_id},
                        blocking=True,
                        return_response=response_supported,
                    )
                # A successful fetch of an unchanged schedule still renews cache
                # freshness. An old fallback cache alone is not a new download.
                state = self.hass.states.get(entity_id)
                schedule = None
                if response_supported and isinstance(response, dict):
                    payload = response.get(entity_id, response)
                    if isinstance(payload, dict):
                        schedule = payload.get("schedule")
                elif (
                    state is not None
                    and state.state not in ("unknown", "unavailable")
                    and state.last_updated > started
                ):
                    # Older source versions without responses need a newly
                    # published schedule; a pre-existing attribute is insufficient.
                    schedule = state.attributes.get("schedule")
                success = isinstance(schedule, list) and bool(schedule)
                if success:
                    self.store.set("ramses_schedule", schedule)
                    self.store.set("ramses_schedule_saved_at", dt_util.utcnow().isoformat())
            except Exception:  # An optional RF failure must not break room control.
                success = False
                _LOGGER.debug("RF schedule fetch failed for %s", entity_id, exc_info=True)
            failures = 0 if success else min(5, self._failure_count() + 1)
            self.store.set("ramses_schedule_failures", failures)
            # The normal room cycle persists these fields with policy memory.
            # Do not race a background snapshot against an actuator-cycle save.

    async def stop(self):
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
