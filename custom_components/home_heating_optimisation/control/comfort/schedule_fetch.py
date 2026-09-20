"""Bounded background RF schedule requests, shared fairly between rooms."""

import asyncio
import logging
from contextlib import suppress
from datetime import timedelta

from homeassistant.core import SupportsResponse
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

STATUS_NOT_ATTEMPTED = "not_attempted"
STATUS_FETCHING = "fetching"
STATUS_OK = "ok"
STATUS_FAILED = "failed"

FAILURE_INCOMPLETE_FRAGMENTS = "incomplete_fragments"
FAILURE_TRANSPORT_TIMEOUT = "transport_timeout"
FAILURE_PARSER_ERROR = "parser_error"
FAILURE_UNAVAILABLE = "unavailable"
FAILURE_UNKNOWN = "unknown"

# Message fragments emitted by ramses_rf/ramses_cc, matched case-insensitively.
# Order matters: fragment assembly is checked before the generic "schedule" words.
_FAILURE_PATTERNS = (
    (FAILURE_INCOMPLETE_FRAGMENTS, ("decompress", "fragment")),
    (
        FAILURE_TRANSPORT_TIMEOUT,
        (
            "timed out",
            "timeout",
            "within",  # ramses_rf: "Failed to obtain schedule within N secs"
            "send failed",
            "transport",
            "no reply",
            "not connected",
        ),
    ),
    (FAILURE_PARSER_ERROR, ("invalid schedule", "switchpoint", "parse", "malformed")),
    (FAILURE_UNAVAILABLE, ("unavailable", "not found", "unknown service", "not ready")),
)


def classify_failure(exc: BaseException | None) -> str:
    """Map a fetch exception to a small, stable failure class for diagnostics.

    The source integration wraps library errors in ``HomeAssistantError`` with the
    original message, so the text is the most reliable discriminator available.
    """
    if exc is None:
        return FAILURE_UNAVAILABLE
    if isinstance(exc, TimeoutError | asyncio.TimeoutError):
        return FAILURE_TRANSPORT_TIMEOUT
    text = f"{type(exc).__name__}: {exc}".lower()
    for failure_class, needles in _FAILURE_PATTERNS:
        if any(needle in text for needle in needles):
            return failure_class
    if isinstance(exc, ValueError | TypeError | KeyError):
        return FAILURE_PARSER_ERROR
    return FAILURE_UNKNOWN


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

    def snapshot(self) -> dict:
        """Diagnostic view of the last download attempt; never raises."""
        try:
            next_retry = dt_util.parse_datetime(
                str(self.store.get("ramses_schedule_next_retry_at", ""))
            )
            return {
                "status": str(self.store.get("ramses_schedule_status", STATUS_NOT_ATTEMPTED)),
                "failure_class": self.store.get("ramses_schedule_failure_class"),
                "attempts": self._attempt_count(),
                "next_retry_at": dt_util.as_utc(next_retry) if next_retry else None,
            }
        except Exception:  # noqa: BLE001 - diagnostics must not break a cycle
            return {
                "status": STATUS_NOT_ATTEMPTED,
                "failure_class": None,
                "attempts": 0,
                "next_retry_at": None,
            }

    def _failure_count(self):
        try:
            return min(5, max(0, int(self.store.get("ramses_schedule_failures", 0))))
        except TypeError, ValueError, OverflowError:
            return 0

    def _attempt_count(self):
        try:
            return max(0, int(self.store.get("ramses_schedule_attempts", 0)))
        except TypeError, ValueError, OverflowError:
            return 0

    @staticmethod
    def _retry_interval(failures, cached):
        return (
            timedelta(hours=24)
            if cached
            else timedelta(minutes=min(60, 5 * 2 ** max(0, failures - 1)))
        )

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
        retry = self._retry_interval(self._failure_count(), cache_available())
        last = dt_util.parse_datetime(str(self.store.get("ramses_schedule_requested_at", "")))
        if last and dt_util.utcnow() - dt_util.as_utc(last) < retry:
            return
        self.task = self.hass.async_create_background_task(
            self._fetch(entity_id, lock, cache_available),
            f"HHO schedule {entity_id}",
        )

    async def _fetch(self, entity_id, lock, cache_available=lambda: False):
        # Radio schedule transfers are multi-message operations. Nine rooms must
        # not start transfers together when the integration is restored.
        async with lock:
            state = self.hass.states.get(entity_id)
            if self.closed or state is None or state.state in ("unavailable", "unknown"):
                return
            started = dt_util.utcnow()
            self.store.set("ramses_schedule_requested_at", started.isoformat())
            self.store.set("ramses_schedule_status", STATUS_FETCHING)
            self.store.set("ramses_schedule_attempts", self._attempt_count() + 1)
            failure: BaseException | None = None
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
            except Exception as exc:  # An optional RF failure must not break room control.
                success = False
                failure = exc
                _LOGGER.debug("RF schedule fetch failed for %s", entity_id, exc_info=True)
            failures = 0 if success else min(5, self._failure_count() + 1)
            self.store.set("ramses_schedule_failures", failures)
            self.store.set("ramses_schedule_status", STATUS_OK if success else STATUS_FAILED)
            self.store.set(
                "ramses_schedule_failure_class", None if success else classify_failure(failure)
            )
            if success:
                self.store.set("ramses_schedule_attempts", 0)
            try:
                cached = success or bool(cache_available())
            except Exception:  # noqa: BLE001 - informational only
                cached = success
            self.store.set(
                "ramses_schedule_next_retry_at",
                (started + self._retry_interval(failures, cached)).isoformat(),
            )
            # The normal room cycle persists these fields with policy memory.
            # Do not race a background snapshot against an actuator-cycle save.

    async def stop(self):
        self.closed = True
        if self.task is not None and not self.task.done():
            self.task.cancel()
            with suppress(asyncio.CancelledError):
                await self.task
