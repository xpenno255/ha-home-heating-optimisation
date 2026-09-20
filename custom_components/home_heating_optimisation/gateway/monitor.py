"""Watch configured RAMSES gateway online entities and alert on silence.

The monitor observes availability entities only. It never issues device
commands, never changes control decisions and never claims that a radio
command failed or succeeded; it reports that a gateway entity has not been
`on` for longer than the configured threshold.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from homeassistant.components import persistent_notification as pn
from homeassistant.core import callback
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util
from homeassistant.util import slugify

from ..const import DOMAIN

LOGGER = logging.getLogger(__name__)

EVENT_UNRESPONSIVE = f"{DOMAIN}_gateway_unresponsive"
EVENT_RECOVERED = f"{DOMAIN}_gateway_recovered"
DEFAULT_OFFLINE_MINUTES = 10
MIN_OFFLINE_MINUTES = 1
MAX_OFFLINE_MINUTES = 120
TICK = timedelta(seconds=60)
MIN_GRACE = timedelta(minutes=2)
FLAP_WINDOW = timedelta(hours=1)
STATES = ("online", "unresponsive", "unconfigured", "unavailable")


def gateway_slug(entity_id):
    """Stable short name from the entity's object ID, e.g. `gateway_a_online`."""
    return slugify(entity_id.split(".", 1)[-1])


def gateway_config(config):
    """Normalised gateway options; an empty entity list disables the monitor."""
    raw = config.get("gateways") or {}
    entities = []
    for entity in raw.get("gateway_entities") or []:
        if isinstance(entity, str) and entity and entity not in entities:
            entities.append(entity)
    try:
        minutes = int(raw.get("gateway_offline_minutes", DEFAULT_OFFLINE_MINUTES))
    except TypeError, ValueError:
        minutes = DEFAULT_OFFLINE_MINUTES
    minutes = min(max(minutes, MIN_OFFLINE_MINUTES), MAX_OFFLINE_MINUTES)
    return {
        "gateway_entities": entities,
        "gateway_offline_minutes": minutes,
        "gateway_notify": bool(raw.get("gateway_notify", True)),
        "gateway_events": bool(raw.get("gateway_events", True)),
    }


@dataclass
class GatewayState:
    entity_id: str
    slug: str
    status: str = "unavailable"
    source_state: str | None = None
    last_online: datetime | None = None
    offline_since: datetime | None = None
    online_since: datetime | None = None
    last_change: datetime | None = None
    alerted: bool = False
    outage_count: int = 0
    flaps: list[datetime] = field(default_factory=list)

    def flap_count(self, now):
        cutoff = now - FLAP_WINDOW
        self.flaps = [t for t in self.flaps if t >= cutoff]
        return len(self.flaps)

    def report(self, now):
        return {
            "entity_id": self.entity_id,
            "status": self.status,
            "source_state": self.source_state,
            "last_online": _iso(self.last_online),
            "since": _iso(self.offline_since) if self.status != "online" else None,
            "last_change": _iso(self.last_change),
            "outage_count": self.outage_count,
            "flap_count": self.flap_count(now),
        }


def _iso(value):
    return value.isoformat() if value else None


def _minutes(start, now):
    return max(0, round((now - start).total_seconds() / 60)) if start else 0


class GatewayMonitor:
    """One alert per outage, one recovery, and a per-gateway diagnostic state."""

    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        options = gateway_config(heating.config)
        self.entities = options["gateway_entities"]
        self.threshold = timedelta(minutes=options["gateway_offline_minutes"])
        self.notify = options["gateway_notify"]
        self.events = options["gateway_events"]
        self.grace = max(self.threshold, MIN_GRACE)
        # A gateway must stay on this long before an outage is declared over.
        self.recovery_settle = min(self.threshold, MIN_GRACE)
        self.states = {e: GatewayState(e, gateway_slug(e)) for e in self.entities}
        self.started_at = None
        self.stopped = False
        self.last_error = None
        self.listeners = []
        self._unsub = []

    @property
    def enabled(self):
        return bool(self.entities)

    @property
    def status(self):
        if not self.enabled:
            return "unconfigured"
        if any(s.status == "unresponsive" for s in self.states.values()):
            return "unresponsive"
        if self.started_at is None:
            return "unavailable"
        return (
            "online" if all(s.status == "online" for s in self.states.values()) else "unavailable"
        )

    def start(self):
        if not self.enabled or self._unsub:
            return
        self.stopped = False
        self.started_at = dt_util.utcnow()
        self._unsub.append(
            async_track_state_change_event(self.hass, self.entities, self._on_state_change)
        )
        self._unsub.append(async_track_time_interval(self.hass, self._on_tick, TICK))
        self.evaluate()

    @callback
    def stop(self):
        self.stopped = True
        for unsub in self._unsub:
            unsub()
        self._unsub.clear()

    @callback
    def _on_state_change(self, event):
        self.evaluate()

    @callback
    def _on_tick(self, now):
        self.evaluate()

    def evaluate(self, now=None):
        """Re-read every gateway entity; failures are logged and never propagate."""
        if self.stopped or not self.enabled:
            return
        now = now or dt_util.utcnow()
        changed = False
        for state in self.states.values():
            try:
                changed |= self._evaluate_one(state, now)
            except Exception as err:  # noqa: BLE001 - optional feature must stay isolated
                self.last_error = f"{type(err).__name__}: {err}"
                LOGGER.exception("Gateway monitor failed for %s", state.entity_id)
        if changed:
            for listener in list(self.listeners):
                listener()

    def _evaluate_one(self, state, now):
        raw = self.hass.states.get(state.entity_id)
        source_state = raw.state if raw else None
        online = source_state == "on"
        changed = source_state != state.source_state
        state.source_state = source_state
        if online:
            state.last_online = now
            if state.online_since is None:
                state.online_since = now
                if state.offline_since is not None:
                    state.flaps.append(now)
                    changed = True
            if state.alerted:
                if now - state.online_since < self.recovery_settle:
                    return changed
                self._recover(state, now)
                changed = True
            elif state.offline_since is not None:
                state.offline_since = None
                changed = True
            if state.status != "online":
                state.status = "online"
                state.last_change = now
                changed = True
            return changed
        state.online_since = None
        if state.offline_since is None:
            state.offline_since = raw.last_changed if raw else now
            state.status = "unavailable"
            state.last_change = now
            changed = True
        if (
            not state.alerted
            and now - state.offline_since >= self.threshold
            and now - self.started_at >= self.grace
        ):
            self._alert(state, now)
            changed = True
        return changed

    def _alert(self, state, now):
        state.alerted = True
        state.status = "unresponsive"
        state.last_change = now
        state.outage_count += 1
        duration = _minutes(state.offline_since, now)
        flap_count = state.flap_count(now)
        ir.async_create_issue(
            self.hass,
            DOMAIN,
            f"gateway_unresponsive_{state.slug}",
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key="gateway_unresponsive",
            translation_placeholders={
                "entity": state.entity_id,
                "since": self._local(state.offline_since),
                "minutes": str(duration),
            },
        )
        if self.notify:
            pn.async_create(
                self.hass,
                self._outage_message(state, now),
                title="Heating gateway not responding",
                notification_id=self._notification_id(state),
            )
        self._fire(EVENT_UNRESPONSIVE, state, now, duration, flap_count)
        self._journal(state, "unresponsive", duration)

    def _recover(self, state, now):
        duration = _minutes(state.offline_since, now)
        flap_count = state.flap_count(now)
        state.alerted = False
        state.status = "online"
        state.last_change = now
        state.offline_since = None
        ir.async_delete_issue(self.hass, DOMAIN, f"gateway_unresponsive_{state.slug}")
        pn.async_dismiss(self.hass, self._notification_id(state))
        if self.notify:
            flaps = f" It flapped {flap_count} times in the last hour." if flap_count > 1 else ""
            pn.async_create(
                self.hass,
                f"Gateway {state.entity_id} is reported online again after about "
                f"{duration} minutes.{flaps} This confirms the online entity only, not radio "
                "delivery of any command.",
                title="Heating gateway recovered",
                notification_id=f"{self._notification_id(state)}_recovered",
            )
        self._fire(EVENT_RECOVERED, state, now, duration, flap_count)
        self._journal(state, "recovered", duration)

    def _outage_message(self, state, now):
        others = [s for s in self.states.values() if s is not state]
        others_online = sum(1 for s in others if s.source_state == "on")
        if state.source_state in (None, "unavailable", "unknown"):
            how = f"is {state.source_state or 'missing'} in Home Assistant"
        else:
            how = f"reports {state.source_state}"
        pool = (
            f"Other configured gateways online: {others_online} of {len(others)}."
            if others
            else "No other gateway is configured for monitoring."
        )
        return (
            f"Gateway {state.entity_id} {how} and has not been online since "
            f"{self._local(state.offline_since)} ({_minutes(state.offline_since, now)} minutes). "
            f"{pool} Room and boiler control continue; radio schedules fall back to the "
            "cached copy. This does not show that any radio command failed or that a radiator "
            "did not move. Check the gateway's power, USB or network link and the ramses_cc "
            "diagnostics."
        )

    def _notification_id(self, state):
        return f"{DOMAIN}_gateway_{state.slug}"

    def _local(self, value):
        return dt_util.as_local(value).strftime("%Y-%m-%d %H:%M") if value else "unknown"

    def _fire(self, event, state, now, duration, flap_count):
        if not self.events:
            return
        self.hass.bus.async_fire(
            event,
            {
                "entity_id": state.entity_id,
                "since": _iso(state.offline_since),
                "duration_minutes": duration,
                "flap_count": flap_count,
            },
        )

    def _journal(self, state, status, duration):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "gateway",
                scope="system",
                origin="source",
                data={
                    "entity_id": state.entity_id,
                    "state": status,
                    "duration_minutes": duration,
                },
            )
        except Exception:  # noqa: BLE001 - journal is optional
            LOGGER.debug("Journal rejected gateway event", exc_info=True)

    def report(self):
        now = dt_util.utcnow()
        return {
            "status": self.status,
            "threshold_minutes": int(self.threshold.total_seconds() // 60),
            "notify": self.notify,
            "events": self.events,
            "last_error": self.last_error,
            "gateways": {s.slug: s.report(now) for s in self.states.values()},
        }
