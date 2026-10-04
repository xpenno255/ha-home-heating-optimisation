"""Stop a DHW charge when HA's cylinder reading passes the Evohome target.

Only ``ramses_cc.set_dhw_mode`` is used: a 60-minute temporary override with DHW
off, and ``follow_schedule`` to hand back early. The controller ends the override
on its own, so HA stopping or restarting can never leave DHW off for longer. Nothing
is sent unless the water heater follows its schedule, a charge is running and a
recent reading is at or above target plus margin; user boosts and overrides are
left alone.
"""

import asyncio
import logging
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_state_change_event, async_track_time_interval
from homeassistant.util import dt as dt_util

from .cutoff_policy import (
    CONFIRM,
    HOLD,
    MAX_ATTEMPTS,
    MAX_HOLD,
    RENEW_BEFORE,
    cloud_override,
    cutoff_config,
    cutoff_temperature,
    is_hold,
    recent_temperature,
    should_release,
    should_stop,
    usable,
)
from .policy import demand_value, read_mode, read_params

LOGGER = logging.getLogger(__name__)
CALL_TIMEOUT = 30.0
TICK = timedelta(seconds=30)
EXPIRY_SLACK = timedelta(minutes=3)
STATES = ("disabled", "misconfigured", "watching", "holding", "unconfirmed", "standby")


class DhwCutoff:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.cfg = cutoff_config(heating.config)
        self.listeners = []
        self.closed = False
        # The hold this integration sent, while it may still be in force.
        self.hold_until = None
        self.hold_started = None
        self.sent_at = None
        self.confirmed = False
        self.attempts = 0
        self.stops = 0
        self.last = None
        self.last_block = None
        self.standby_reason = None
        # A stop the controller never echoed is not retried until this charge ends.
        self.gave_up = False
        self._unsub = []
        self._task = None
        self._lock = asyncio.Lock()

    # -- lifecycle ---------------------------------------------------------

    def start(self):
        if not usable(self.cfg):
            return
        entities = [self.cfg[k] for k in ("water_heater_entity", "demand_entity")]
        entities.append(self.cfg["cylinder_temp_entity"])
        if self.cfg["cloud_entity"]:
            entities.append(self.cfg["cloud_entity"])
        self._unsub.append(async_track_state_change_event(self.hass, entities, self._changed))
        self._unsub.append(async_track_time_interval(self.hass, self._changed, TICK))
        self._kick()

    @callback
    def close(self):
        """Stop watching. A hold in force expires on the controller by itself."""
        self.closed = True
        while self._unsub:
            self._unsub.pop()()

    @callback
    def _changed(self, _event=None):
        if not self.closed:
            self._kick()

    def _kick(self):
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self.step(), "hho_dhw_cutoff")

    # -- evaluation --------------------------------------------------------

    async def step(self):
        async with self._lock:
            if self.closed or not usable(self.cfg):
                return
            try:
                await self._step(dt_util.utcnow())
            finally:
                self._notify()

    async def _step(self, now):
        cfg = self.cfg
        heater = self.hass.states.get(cfg["water_heater_entity"])
        params = read_params(heater)
        mode = heater.attributes.get("mode") if heater is not None else None
        temperature = recent_temperature(self.hass.states.get(cfg["cylinder_temp_entity"]), now)
        demand = demand_value(self.hass.states.get(cfg["demand_entity"]), now)

        if demand is False:
            self.gave_up = False
        if self.hold_until is not None and now >= self.hold_until:
            self._clear()
        if self.hold_until is not None:
            await self._held(now, mode, temperature, params)
            return
        current = read_mode(heater)
        if current != "follow_schedule":
            # A boost, user override or unknown mode: never overridden here.
            self.standby_reason = f"water heater mode {current}"
            return
        # After an HA restart the RAMSES entity can show follow_schedule mid-boost; the
        # cloud entity keeps the controller's real mode (live 4 Oct 13:29).
        if reason := cloud_override(self.hass.states.get(cfg["cloud_entity"] or "")):
            self.standby_reason = reason
            return
        self.standby_reason = None
        if not self.gave_up and should_stop(demand, temperature, params, cfg["margin"]):
            self.attempts = 0
            await self._hold(now, temperature, params)

    async def _held(self, now, mode, temperature, params):
        if is_hold(mode):
            self.confirmed = True
            if should_release(temperature, params):
                if await self._send({"mode": "follow_schedule"}, "release", temperature):
                    self._clear()
            elif (
                self.hold_until - now <= RENEW_BEFORE and now - self.hold_started + HOLD <= MAX_HOLD
            ):
                await self._hold(now, temperature, params, action="renew")
            return
        if self.confirmed:
            # The controller ended the hold (its clock may run a little ahead of ours)
            # or someone replaced it; either way it is no longer ours.
            if self.hold_until - now > EXPIRY_SLACK:
                self._event("taken_over", {"mode": mode})
            self._clear()
            return
        if now - self.sent_at < CONFIRM:
            return
        if self.attempts >= MAX_ATTEMPTS:
            self._event("unconfirmed", {"attempts": self.attempts})
            self._clear()
            self.gave_up = True
            return
        await self._hold(now, temperature, params)

    async def _hold(self, now, temperature, params, action="stop"):
        request = {"mode": "temporary_override", "active": False, "duration": {"minutes": 60}}
        if await self._send(request, action, temperature, params):
            if action == "renew":
                self.attempts = 1
            else:
                self.attempts += 1
                if self.attempts == 1:
                    self.stops += 1
                    self.hold_started = now
            self.sent_at = now
            self.hold_until = now + HOLD
            self.confirmed = False

    def _clear(self):
        self.hold_until = self.hold_started = self.sent_at = None
        self.confirmed = False
        self.attempts = 0

    async def _send(self, request, action, temperature, params=None):
        controls = self.heating.controls
        entity = self.cfg["water_heater_entity"]
        if controls is None:
            self._block("control unavailable")
            return False
        async with controls.lock:
            if reason := controls.dhw_guard_reason(entity):
                self._block(reason)
                return False
            self.last_block = None
            data = {
                "action": action,
                "measured": temperature,
                "cutoff": cutoff_temperature(params, self.cfg["margin"]),
                "request": request,
            }
            try:
                async with asyncio.timeout(CALL_TIMEOUT):
                    await self.hass.services.async_call(
                        "ramses_cc", "set_dhw_mode", {"entity_id": entity, **request}, blocking=True
                    )
            except Exception as err:  # noqa: BLE001 - a timeout or RF fault may still apply
                LOGGER.warning("DHW cutoff %s for %s failed: %s", action, entity, err)
                self._event(action, {**data, "outcome": "failed", "error": type(err).__name__})
                return False
            LOGGER.info("DHW cutoff %s: %s at %s °C", action, entity, temperature)
            self._event(action, {**data, "outcome": "sent"})
            return True

    def _block(self, reason):
        if reason != self.last_block:
            self.last_block = reason
            self._event("blocked", {"reason": reason})

    def _event(self, event, data):
        self.last = {"event": event, "at": dt_util.utcnow().isoformat(), **data}
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                "dhw_cutoff",
                scope="system",
                origin="controller",
                data={"event": event, "outcome": data.get("outcome", event), **data},
            )
        except Exception:  # noqa: BLE001
            pass

    def _notify(self):
        for listener in list(self.listeners):
            listener()

    # -- status ------------------------------------------------------------

    @property
    def status(self):
        if not self.cfg["enabled"]:
            return "disabled"
        if not usable(self.cfg):
            return "misconfigured"
        if self.hold_until is not None:
            return "holding" if self.confirmed else "unconfirmed"
        if self.standby_reason:
            return "standby"
        return "watching"

    def report(self):
        heater = self.hass.states.get(self.cfg["water_heater_entity"] or "")
        params = read_params(heater)
        return {
            "status": self.status,
            "margin": self.cfg["margin"],
            "cutoff_temperature": cutoff_temperature(params, self.cfg["margin"]),
            "hold_until": self.hold_until.isoformat() if self.hold_until else None,
            "stops_since_start": self.stops,
            "standby_reason": self.standby_reason,
            "blocked": self.last_block,
            "last": self.last,
        }
