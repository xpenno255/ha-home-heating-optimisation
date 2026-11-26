"""Shadow optimum start / stop: predict, journal and score; never touch a device.

The only service this calls is ``weather.get_forecasts`` (read-only). It does not use
the control-edit path, so enabling it never changes any controller's mode. Open
scoring records live in memory only; scored results and fitted models are kept in a
private store so a restart loses at most the morning in progress.

With the pre-heat option on it also keeps one plan per room for the next upward
switchpoint (see ``preheat``). It still never writes: the room controller reads the plan
through ``Optimum.preheat`` and decides for itself whether to act on it. The plan is kept
in the store so a restart in the middle of a warm-up carries on.
"""

import asyncio
import logging
from dataclasses import asdict, replace
from datetime import timedelta

from homeassistant.core import callback
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..analytics.const import DEFAULT_TOLERANCE
from .forecast import REFRESH, Forecast, parse_forecast
from .learning import KEEP_EVENTS, RoomFit, dedup_events, fit_all
from .model import (
    RoomModel,
    StartParams,
    next_switchpoints,
    predict_start,
    predict_stop,
    setpoint_at,
)
from .policy import optimum_config
from .preheat import (
    CAPTURE_WINDOW,
    CHARGE_RANGE,
    LEAD_BIAS_MAX,
    LEAD_BIAS_MIN,
    MAX_CHARGES,
    PreheatPlan,
    avoid_charge,
    charge_length,
    dhw_wait_deadline,
    finalise_capture,
    lead_error_minutes,
    learn_lead,
    next_dhw_on,
    note_report,
)
from .preheat_policy import preheat_config
from .scoring import rolling, score_start

LOGGER = logging.getLogger(__name__)
STATES = ("disabled", "needs_analytics", "learning", "ready")
TICK = timedelta(minutes=5)
RELEARN = timedelta(hours=1)
RELEARN_RETRY = timedelta(minutes=10)
# A setpoint this far below the warm-up's target ends it early.
ABANDON_K = 0.3
CLOSE_AFTER = timedelta(minutes=120)
# A setpoint that differs from the target this long after the switchpoint is a change of
# plan (manual hold, next period), which ends the record early.
CHANGE_AFTER = timedelta(minutes=30)
MIN_STEP_K = 0.25
# HHO's model offset (commanded air target minus schedule) allowed into the goal.
OFFSET_MIN, OFFSET_MAX = -1.0, 2.0
# A pre-heat is scored this long after its switchpoint, so overshoot after arrival counts.
PREHEAT_SCORE_AFTER = timedelta(minutes=60)
# A room reading is not planned from unless its sensor was physically heard from within
# this long: its value changed, another value on its device changed, or its radio
# reports having heard it (see the room controller's ``_air_evidence``). A state merely
# written again proves nothing. Without that evidence no pre-heat is planned or begun.
MAX_SOURCE_AGE = timedelta(minutes=45)
# How many scored pre-heats are remembered, so that none is learnt from twice.
MAX_SCORED_KEYS = 100
MAX_SAMPLES = 400
MAX_SCORES = 30
CALL_TIMEOUT = 30.0
SCHEMA = 1
STORE_SUFFIX = "optimum"


def _iso(value):
    return value.isoformat() if value is not None else None


class Optimum:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.cfg = optimum_config(heating.config)
        self.pcfg = preheat_config(heating.config)
        self.plans = {}  # room id -> PreheatPlan for the next upward switchpoint
        self.preheat_last = {}  # room id -> summary of its last scored pre-heat
        # "room id|HH:MM" (local switch time) -> extra lead in minutes learnt from arrivals
        self.lead_bias = {}
        # "room id|plan key" of every pre-heat already scored
        self.scored_keys = []
        # Stored events held duplicates when loaded: the fits shown predate the clean-up
        # until the next learning pass has rebuilt them.
        self.refit_pending = False
        # room id -> (plan key, reason) while a due pre-heat waits for a usable reading
        self.waiting_plans = {}
        self.finishing = {}  # room id -> started plan past its switchpoint, awaiting its score
        # room id -> older ones still awaiting theirs, oldest first: a room with warm-ups
        # less than an hour apart has more than one waiting at a time.
        self.earlier = {}
        self.dhw_charges = []  # minutes of recent DHW charges, oldest first
        self._dhw_since = None
        self._dirty = False
        self.listeners = []
        self.closed = False
        self.fits = {}
        self.events = {}  # room id -> warm-up events, kept across the analytics window
        self.waiting = None  # why learning has not run yet, for the status sensor
        self.forecast = Forecast()
        self.forecast_attempt = None
        self.learned_at = None
        self.records = {}  # room id -> open start records
        self.stops = {}  # room id -> {switchpoint key: stop record}
        self.latest = {}  # room id -> {"start": record|None, "stop": record|None}
        self.scores = []
        self.holdouts = {}
        self.outdoor_source = "current"
        self.last_error = None
        self._unsub = []
        self._task = None
        self._lock = asyncio.Lock()
        self._store = Store(
            hass, SCHEMA, f"home_heating_optimisation.{entry.entry_id}.{STORE_SUFFIX}"
        )

    # -- lifecycle ---------------------------------------------------------

    async def initialise(self):
        if not self.cfg["enabled"]:
            return
        try:
            data = await self._store.async_load()
            if isinstance(data, dict):
                self.scores = [s for s in data.get("scores", []) if isinstance(s, dict)][
                    -MAX_SCORES:
                ]
                for room_id, raw in (data.get("fits") or {}).items():
                    model = raw.get("model")
                    self.fits[room_id] = RoomFit(
                        **{**raw, "model": RoomModel(**model) if model else None}
                    )
                stored = {
                    k: [e for e in v if isinstance(e, dict)]
                    for k, v in (data.get("events") or {}).items()
                    if isinstance(v, list)
                }
                self.events = {k: dedup_events(v)[-KEEP_EVENTS:] for k, v in stored.items()}
                self.refit_pending = any(len(self.events[k]) != len(v) for k, v in stored.items())
                if self.refit_pending:
                    # The stored fits were built with the duplicates in: rebuild them at
                    # the next step rather than when the hourly pass comes round.
                    self.learned_at = None
                self._load_preheat(data)
        except Exception:  # noqa: BLE001 - a bad store only costs the learned models
            LOGGER.warning("Optimum store unreadable; learning again", exc_info=True)
            self.fits, self.events, self.scores = {}, {}, []

    def _load_preheat(self, data):
        for room_id, raw in (data.get("plans") or {}).items():
            try:
                self.plans[room_id] = PreheatPlan.from_dict(raw)
            except Exception:  # noqa: BLE001 - one bad plan only costs that room's plan
                LOGGER.debug("Optimum pre-heat plan for %s unreadable", room_id)
        self.dhw_charges = [
            float(m) for m in (data.get("dhw_charges") or []) if isinstance(m, (int, float))
        ][-MAX_CHARGES:]
        self.preheat_last = {
            k: v for k, v in (data.get("preheat_last") or {}).items() if isinstance(v, dict)
        }
        self.lead_bias = {
            str(k): float(v)
            for k, v in (data.get("lead_bias") or {}).items()
            if isinstance(v, (int, float))
        }
        for room_id, raw in (data.get("finishing") or {}).items():
            try:
                self.finishing[room_id] = PreheatPlan.from_dict(raw)
            except Exception:  # noqa: BLE001 - one bad plan only costs that room's score
                LOGGER.debug("Optimum finishing plan for %s unreadable", room_id)
        for room_id, raws in (data.get("earlier") or {}).items():
            for raw in raws if isinstance(raws, list) else ():
                try:
                    self.earlier.setdefault(room_id, []).append(PreheatPlan.from_dict(raw))
                except Exception:  # noqa: BLE001
                    LOGGER.debug("Optimum earlier plan for %s unreadable", room_id)
        self.scored_keys = [str(k) for k in (data.get("scored_keys") or [])][-MAX_SCORED_KEYS:]
        # A pre-heat that was running when this start happened, and whose switchpoint
        # temperature had not yet been captured, was interrupted: it is still reported,
        # but it teaches nothing. One captured before the start keeps its capture.
        # So was one whose planned start had already passed: it may have begun just
        # before the restart, in the moments before that was saved.
        now = dt_util.utcnow()

        def restored(plan):
            cut = plan.capture is None if plan.started else plan.start_at <= now
            return replace(plan, interrupted=True) if cut and not plan.interrupted else plan

        self.plans = {k: restored(v) for k, v in self.plans.items()}
        self.finishing = {k: restored(v) for k, v in self.finishing.items()}
        self.earlier = {k: [restored(v) for v in plans] for k, plans in self.earlier.items()}

    def start(self):
        if not self.cfg["enabled"]:
            return
        self._unsub.append(async_track_time_interval(self.hass, self._changed, TICK))
        self._kick()

    @callback
    def close(self):
        self.closed = True
        while self._unsub:
            self._unsub.pop()()

    @callback
    def _changed(self, _event=None):
        if not self.closed:
            self._kick()

    def _kick(self):
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self.step(), "hho_optimum")

    def _document(self):
        return {
            "schema": SCHEMA,
            "scores": self.scores,
            "fits": {k: asdict(v) for k, v in self.fits.items()},
            "events": self.events,
            "plans": {k: v.to_dict() for k, v in self.plans.items()},
            "dhw_charges": self.dhw_charges,
            "preheat_last": self.preheat_last,
            # What arrivals have taught and which pre-heats it came from go in one
            # document, so neither can be saved without the other.
            "lead_bias": self.lead_bias,
            "finishing": {k: v.to_dict() for k, v in self.finishing.items()},
            "earlier": {k: [v.to_dict() for v in plans] for k, plans in self.earlier.items()},
            "scored_keys": self.scored_keys[-MAX_SCORED_KEYS:],
        }

    async def _save(self):
        try:
            await self._store.async_save(self._document())
        except Exception:  # noqa: BLE001
            LOGGER.warning("Optimum store save failed", exc_info=True)

    def _save_soon(self):
        """Save without waiting for the next step: a pre-heat's start or hold must not be
        lost to a restart in the minutes before it."""
        self._dirty = False
        self.hass.async_create_task(self._save(), "home_heating_optimisation optimum save")

    # -- inputs ------------------------------------------------------------

    @property
    def analytics(self):
        return getattr(self.heating, "analytics", None)

    def _room_config(self, room_id):
        rooms = self.heating.config.get("rooms") or []
        return next((r for r in rooms if r.get("id") == room_id), None)

    def _observation(self, room_id, now=None):
        """The room's air reading as an observation, or None when there is none to use.

        For a controlled room this is the reading its controller accepted this cycle, with
        the controller's own judgement of it, so the planner never re-reads a sensor the
        controller has set aside (a stale value restored after a restart, say). A room
        without a controller is read from its mapped sensor, or its thermostat.

        ``value`` and ``source``; ``qualified`` and ``quality`` (whether it may teach
        anything, and if not why); ``changed_at`` (when the value last changed: the time
        it counts as evidence of the temperature from); ``evidence``, ``evidence_at`` and
        ``evidence_minutes`` (the latest sign the sensor was physically heard from, what
        it was and how long ago; None when there is none), which the planning limit
        applies to; ``reported_at`` (the source's last write, which proves nothing).
        """
        now = now or dt_util.utcnow()
        controls = getattr(self.heating, "controls", None)
        c = (getattr(controls, "rooms", None) or {}).get(room_id)
        d = getattr(c, "air_observed", None) or getattr(c, "data", None)
        if d is not None and getattr(d, "air_quality", None) is not None:
            value, source = d.air_temp, d.air_temp_source
            reported, changed = d.air_reported_at, d.air_changed_at
            qualified, quality = d.air_qualified, d.air_quality
            evidence_at, evidence = d.air_evidence_at, d.air_evidence
        else:
            room = self._room_config(room_id)
            if room is None:
                return None
            source = room.get("air_sensor") or room["climate"]
            state = self.hass.states.get(source)
            if state is None:
                return None
            own = bool(room.get("air_sensor"))
            value = _number(state.state if own else state.attributes.get("current_temperature"))
            reported = getattr(state, "last_reported", None) or state.last_updated
            changed = state.last_changed if own else None
            qualified, quality = value is not None and own, "ok" if own else "fallback"
            evidence_at, evidence = changed, "value change" if own else "none"
        if value is None or reported is None:
            return None
        return {
            "value": value,
            "source": source,
            "qualified": bool(qualified),
            "quality": quality,
            "reported_at": reported,
            "changed_at": changed,
            "evidence": evidence,
            "evidence_at": evidence_at,
            # The controller has already refused evidence dated after its own cycle; a
            # cycle that ran since ``now`` was taken is zero minutes old, not negative.
            "evidence_minutes": None
            if evidence_at is None
            else max(0.0, round((now - evidence_at).total_seconds() / 60, 1)),
        }

    def _read(self, room_id, now=None):
        """(room temperature, zone setpoint) for planning.

        The temperature is None when the room has no reading, or its sensor has not been
        physically heard from within ``MAX_SOURCE_AGE``: a number from a sensor that may
        have gone quiet is not where the room is now. With no temperature no pre-heat is
        planned, moved or begun, and the room follows its ordinary schedule.
        """
        room = self._room_config(room_id)
        if room is None:
            return None, None
        climate = self.hass.states.get(room["climate"])
        setpoint = _number(climate.attributes.get("temperature")) if climate else None
        obs = self._observation(room_id, now)
        age = obs["evidence_minutes"] if obs else None
        if age is None or age > MAX_SOURCE_AGE.total_seconds() / 60:
            return None, setpoint
        return obs["value"], setpoint

    def _outdoor_now(self, controls):
        for c in controls.rooms.values():
            if c.data and c.data.outdoor_temp is not None:
                return c.data.outdoor_temp
        boiler = controls.boiler
        return boiler.data.outdoor_temp if boiler and boiler.data else None

    @staticmethod
    def _next_switch(c, now):
        """(current setpoint, switchpoint time, next setpoint) or None."""
        data = c.data
        current = data.schedule_setpoint
        if (
            current is not None
            and data.next_switchpoint_at is not None
            and data.next_switchpoint_setpoint is not None
            and data.next_switchpoint_at > now
        ):
            return current, data.next_switchpoint_at, data.next_switchpoint_setpoint
        try:
            schedule = c._ramses_schedule(c._config.get("primary_climate"))
            local = dt_util.as_local(now)
            upcoming = next_switchpoints(schedule, local, 1)
            current = setpoint_at(schedule, local) if current is None else current
        except Exception:  # noqa: BLE001
            return None
        if not upcoming or current is None:
            return None
        return current, dt_util.as_utc(upcoming[0][0]), upcoming[0][1]

    def _next_warmup(self, c, now, switch):
        """(switchpoint, from, to) of the next upward step, or None when unknown.

        The cloud schedule gives only the next switchpoint; the RAMSES weekly schedule, when
        cached, finds an upward one further ahead (e.g. tomorrow 07:00 from tonight).
        """
        if switch is not None and switch[2] - switch[0] >= MIN_STEP_K:
            return switch[1], switch[0], switch[2]
        try:
            schedule = c._ramses_schedule(c._config.get("primary_climate"))
            local = dt_util.as_local(now)
            previous = setpoint_at(schedule, local)
            for at, setpoint in next_switchpoints(schedule, local, 8):
                if previous is not None and setpoint - previous >= MIN_STEP_K:
                    return dt_util.as_utc(at), previous, setpoint
                previous = setpoint
        except Exception:  # noqa: BLE001 - no schedule just means no hint
            return None
        return None

    # -- evaluation --------------------------------------------------------

    async def step(self, now=None):
        async with self._lock:
            if self.closed or not self.cfg["enabled"]:
                return
            try:
                await self._step(now or dt_util.utcnow())
                self.last_error = None
            except Exception as err:  # noqa: BLE001 - never let a shadow feature break the runtime
                self.last_error = f"{type(err).__name__}: {err}"
                LOGGER.warning("Optimum step failed", exc_info=True)
            finally:
                for listener in list(self.listeners):
                    listener()

    async def _step(self, now):
        controls = self.heating.controls
        if controls is None or self.analytics is None or not controls.rooms:
            return
        await self._refresh_forecast(now)
        await self._learn(now)
        changed = self._track_dhw(controls, now)
        current = self._outdoor_now(controls)
        outdoor_at, source = self.forecast.outdoor_function(now, current)
        self.outdoor_source = source
        self.latest = {}
        for room_id, c in controls.rooms.items():
            if c.data is None:
                continue
            changed |= self._room(now, room_id, c, outdoor_at, source, current)
        changed |= self._dirty
        self._dirty = False
        if changed:
            await self._save()

    async def _refresh_forecast(self, now):
        entity = self.cfg["forecast_entity"]
        if not entity or not self.forecast.due(now):
            return
        if self.forecast_attempt and now - self.forecast_attempt < REFRESH:
            return
        self.forecast_attempt = now
        try:
            async with asyncio.timeout(CALL_TIMEOUT):
                response = await self.hass.services.async_call(
                    "weather",
                    "get_forecasts",
                    {"entity_id": entity, "type": "hourly"},
                    blocking=True,
                    return_response=True,
                )
            state = self.hass.states.get(entity)
            fahrenheit = bool(state and state.attributes.get("temperature_unit") == "°F")
            points = parse_forecast(response, entity, fahrenheit)
        except Exception as err:  # noqa: BLE001 - the current outdoor reading is the fallback
            LOGGER.debug("Optimum forecast unavailable: %s", err)
            return
        if points:
            self.forecast.update(points, now)

    async def _learn(self, now):
        due = self.learned_at is None or now - self.learned_at >= RELEARN
        if not due and not any(f.status == "ready" for f in self.fits.values()):
            due = now - self.learned_at >= RELEARN_RETRY
        if not due:
            return
        # After a restart the analytics history loads in the background; learning from an
        # empty or half-loaded store would leave every room unfitted until the next retry.
        if getattr(self.analytics, "backfill_status", None) in ("pending", "running"):
            self.waiting = "history loading"
            return
        if not self.analytics.store.observations:
            self.waiting = "no history yet"
            return
        self.waiting = None
        self.learned_at = now
        options = self.heating.config
        params = {
            "tolerance": options.get("comfort_tolerance", DEFAULT_TOLERANCE),
            "timezone": self.hass.config.time_zone,
            "min_warmups": self.cfg["min_warmups"],
            "min_cooldowns": self.cfg["min_cooldowns"],
        }
        observations = list(self.analytics.store.observations)
        zones = list(self.heating.controls.rooms)
        self.fits, self.events = await self.hass.async_add_executor_job(
            fit_all, observations, zones, params, self.events, now.timestamp()
        )
        self.refit_pending = False
        await self._save()

    def _room(self, now, room_id, c, outdoor_at, source, current):
        ts = now.timestamp()
        temp, setpoint = self._read(room_id, now)
        sample = [ts, temp, setpoint, current]
        records = self.records.setdefault(room_id, [])
        changed = False
        for rec in list(records):
            rec["samples"] = [*rec["samples"], sample][-MAX_SAMPLES:]
            if self._closing(rec, now, setpoint):
                records.remove(rec)
                self._score(room_id, rec)
                changed = True
        fit = self.fits.get(room_id)
        view = {"start": None, "stop": None}
        self.latest[room_id] = view
        changed |= self._retire_plan(now, room_id, temp)
        switch = self._next_switch(c, now)
        view["warmup"] = self._next_warmup(c, now, switch)
        if fit is not None and temp is not None and switch is not None:
            self._predict(now, room_id, c, switch, temp, fit, outdoor_at, source, records, view)
        for rec in records:
            self._freeze(now, room_id, rec)
        changed |= self._advance_plan(now, room_id, c, temp, records, view)
        for stop in self.stops.get(room_id, {}).values():
            if not stop["frozen"] and now >= stop["off_at"]:
                stop["frozen"] = True
                self._event("stop_predicted", room_id, self._stop_data(stop))
        stops = self.stops.setdefault(room_id, {})
        for key in [k for k, v in stops.items() if v["switch_ts"] < ts - 3600]:
            del stops[key]
        return changed

    def _predict(self, now, room_id, c, switch, temp, fit, outdoor_at, source, records, view):
        current_sp, switch_at, next_sp = switch
        until = (switch_at - now).total_seconds() / 60
        max_lead = self.max_lead_minutes
        if until > max_lead:
            return
        if next_sp - current_sp >= MIN_STEP_K and fit.model is not None:
            model = fit.model
            goal = self._goal(c, next_sp)
            params = StartParams(self.cfg["margin_minutes"], max_lead)
            pred = predict_start(
                now,
                switch_at,
                goal,
                temp,
                model,
                outdoor_at,
                self.cfg["boost_flow"],
                params,
                plan_flow=self.pcfg["plan_flow"] if self.preheat_mode != "off" else None,
            )
            key = int(switch_at.timestamp())
            rec = next((r for r in records if r["key"] == key), None)
            if rec is None:
                rec = {
                    "key": key,
                    "switch_ts": switch_at.timestamp(),
                    "samples": [],
                    "frozen": None,
                    "journaled": None,
                }
                records.append(rec)
                rec["samples"] = [[now.timestamp(), temp, self._read(room_id)[1], outdoor_at(now)]]
            outdoors = [
                outdoor_at(now + timedelta(minutes=m)) for m in range(0, int(until) + 1, 30)
            ]
            rec.update(
                target=goal,
                schedule_target=next_sp,
                from_setpoint=current_sp,
                delay_minutes=pred.delay_minutes,
                boost_flow=self.cfg["boost_flow"],
                mpk=pred.base_minutes_per_k,
                plan_factor=pred.plan_factor,
                median_supply=model.median_supply,
                outdoor_source=source,
                forecast_mean=sum(outdoors) / len(outdoors) if source == "forecast" else None,
                temp=temp,
                prediction=pred,
            )
            view["start"] = rec
            self._plan(now, room_id, pred, switch_at, next_sp, goal, temp, view)
        elif current_sp - next_sp >= MIN_STEP_K and fit.coast_rate is not None:
            stop = predict_stop(switch_at, fit.coast_rate, self.cfg["coast_drop"], max_lead)
            key = int(switch_at.timestamp())
            stops = self.stops.setdefault(room_id, {})
            record = stops.setdefault(key, {"frozen": False})
            record.update(
                switch_ts=switch_at.timestamp(),
                target=current_sp,
                next_setpoint=next_sp,
                off_at=stop.off_at,
                coast_minutes=stop.coast_minutes,
                rate=stop.rate,
                temp=temp,
            )
            view["stop"] = record

    def _freeze(self, now, room_id, rec):
        pred = rec.get("prediction")
        if pred is None or rec["frozen"] is not None:
            return
        if now >= pred.start_at:
            rec["frozen"] = {
                "start_ts": pred.start_at.timestamp(),
                "lead_minutes": pred.lead_minutes,
                "frozen_at": now.timestamp(),
            }
            self._event("start_predicted", room_id, self._start_data(rec))
        elif rec["journaled"] is None or abs(rec["journaled"] - pred.start_at.timestamp()) >= 300:
            rec["journaled"] = pred.start_at.timestamp()
            self._event("start_updated", room_id, self._start_data(rec), routine=True)

    @staticmethod
    def _start_data(rec):
        pred = rec["prediction"]
        return {
            "switchpoint": _iso(dt_util.utc_from_timestamp(rec["switch_ts"])),
            "schedule_target": rec["schedule_target"],
            "goal": rec["target"],
            "target": rec["target"],
            "temperature": rec["temp"],
            "start_at": _iso(pred.start_at),
            "lead_minutes": pred.lead_minutes,
            "late_minutes": pred.late_minutes,
            "minutes_per_k": pred.minutes_per_k,
            "heating_minutes": pred.heating_minutes,
            "delay_minutes": pred.delay_minutes,
            "outdoor_model": pred.outdoor_used,
            "boost_flow": rec["boost_flow"],
            "boost_lead_minutes": pred.boost_lead_minutes,
            "boost_late_minutes": pred.boost_late_minutes,
            "boost_gain_minutes": pred.boost_gain_minutes,
            "outdoor_source": rec["outdoor_source"],
        }

    @staticmethod
    def _stop_data(stop):
        return {
            "switchpoint": _iso(dt_util.utc_from_timestamp(stop["switch_ts"])),
            "target": stop["target"],
            "next_setpoint": stop["next_setpoint"],
            "off_at": _iso(stop["off_at"]),
            "coast_minutes": stop["coast_minutes"],
            "coast_rate": stop["rate"],
            "temperature": stop["temp"],
        }

    @staticmethod
    def _closing(rec, now, setpoint):
        if "schedule_target" not in rec:
            return False
        after = now.timestamp() - rec["switch_ts"]
        if after >= CLOSE_AFTER.total_seconds():
            return True
        # Close early only when the target has come down again (a new period or a manual
        # change). HHO's own commanded target sits above the schedule (live 14 Oct: 19.5 for
        # a 19.0 step), which is the warm-up itself, not a change.
        low = min(rec["schedule_target"], rec.get("goal", rec["schedule_target"]))
        return (
            after >= CHANGE_AFTER.total_seconds()
            and setpoint is not None
            and setpoint < low - ABANDON_K
        )

    @staticmethod
    def _offset(c):
        """HHO's commanded air target above the schedule, within -1.0 to +2.0 K, else 0."""
        data = c.data
        try:
            offset = data.air_setpoint - data.schedule_setpoint
        except TypeError:
            return 0.0
        return min(OFFSET_MAX, max(OFFSET_MIN, offset)) if offset == offset else 0.0

    # -- pre-heat plan -------------------------------------------------------

    @property
    def preheat_mode(self):
        """``off``, ``shadow`` or ``active``; off whenever the predictor itself is disabled."""
        return self.pcfg["mode"] if self.cfg["enabled"] else "off"

    @property
    def max_lead_minutes(self):
        """Earliest a start is predicted: the pre-heat lead when pre-heat is on."""
        if self.preheat_mode != "off":
            return self.pcfg["max_lead_minutes"]
        return self.cfg["max_lead_minutes"]

    def _goal(self, c, next_sp):
        """The target a warm-up aims at: HHO's commanded air target for the next period."""
        if self.preheat_mode != "off":
            exact = getattr(c.data, "preheat_setpoint", None)
            if exact is not None and exact == exact:
                return next_sp + min(OFFSET_MAX, max(OFFSET_MIN, exact - next_sp))
        return next_sp + self._offset(c)

    def _track_dhw(self, controls, now):
        """Learn charge lengths from the boiler's hot water flag; True when one was added."""
        boiler = controls.boiler
        active = bool(boiler is not None and boiler.data is not None and boiler.data.dhw_active)
        if active:
            if self._dhw_since is None:
                self._dhw_since = now
            return False
        if self._dhw_since is None:
            return False
        minutes = (now - self._dhw_since).total_seconds() / 60
        self._dhw_since = None
        if not CHARGE_RANGE[0] <= minutes <= CHARGE_RANGE[1]:
            return False
        self.dhw_charges = [*self.dhw_charges, round(minutes, 1)][-MAX_CHARGES:]
        return True

    def _dhw_blocking(self):
        """Hot water is charging: space heating waits, so a new pre-heat must not begin."""
        controls = self.heating.controls
        boiler = controls.boiler if controls else None
        return bool(boiler is not None and boiler.data is not None and boiler.data.dhw_active)

    def _dhw_on(self):
        entity = (self.heating.config.get("dhw_cutoff") or {}).get("cloud_entity")
        return next_dhw_on(self.hass.states.get(entity)) if entity else None

    def _plan(self, now, room_id, pred, switch_at, next_sp, goal, temp, view):
        """Keep the room's plan for this switchpoint current until it has started."""
        if self.preheat_mode == "off":
            return
        key = int(switch_at.timestamp())
        old = self.plans.get(room_id)
        if old is not None and old.started and now < old.switch_at:
            return  # already warming: the plan no longer moves
        if pred.heating_minutes <= 0:
            if old is not None and old.key == key:
                del self.plans[room_id]
                self._dirty = True
            return
        # Earlier arrivals at this time of day move the start: earlier for a room that
        # was short at the switchpoint, later for one that was over. Never past the
        # longest lead, never later than a minute before the switchpoint.
        bias = self._lead_bias(room_id, switch_at)
        biased = pred.start_at - timedelta(minutes=bias)
        biased = max(biased, switch_at - timedelta(minutes=self.max_lead_minutes))
        biased = min(biased, switch_at - timedelta(minutes=1))
        start_at, late = avoid_charge(
            biased,
            switch_at,
            pred.lead_minutes,
            self._dhw_on(),
            charge_length(self.dhw_charges),
        )
        late = max(late, pred.late_minutes)
        same = old if old is not None and old.key == key else None
        plan = PreheatPlan(
            room_id=room_id,
            key=key,
            start_at=start_at,
            switch_at=switch_at,
            schedule_target=next_sp,
            goal=goal,
            start_temp=temp,
            delay_minutes=pred.delay_minutes,
            # The planned line still ends where it did, so it stretches or shrinks with
            # the start.
            climb_minutes=max(0.0, pred.heating_minutes - pred.delay_minutes + bias),
            lead_bias_minutes=bias,
            planned_minutes=max(0, round((switch_at - start_at).total_seconds() / 60)),
            late_risk_minutes=late,
            plan_factor=pred.plan_factor,
            latest_start=switch_at
            - timedelta(minutes=pred.boost_lead_minutes or pred.lead_minutes),
            planned_journaled=same.planned_journaled if same else False,
            # What has already happened to this pre-heat stays with it as its plan moves.
            dhw_held=same.dhw_held if same else False,
            interrupted=same.interrupted if same else False,
        )
        if not plan.planned_journaled:
            plan = replace(plan, planned_journaled=True)
            self._preheat_event("planned", room_id, self._plan_data(plan))
        if plan != old:
            self._dirty = True
        self.plans[room_id] = plan

    def _retire_plan(self, now, room_id, temp=None):
        """Past its switchpoint a started plan waits for its score; others are dropped.

        The room may still have an earlier pre-heat waiting for its own score (warm-ups
        an hour or less apart): that one is not displaced. It is scored now if its hour
        is up, and otherwise kept alongside until it is. True when anything was scored.
        """
        plan = self.plans.get(room_id)
        if plan is None or now < plan.switch_at:
            return False
        del self.plans[room_id]
        self._dirty = True
        waited = self.waiting_plans.pop(room_id, None)
        if plan.started:
            older = self.finishing.get(room_id)
            if older is not None and older.key != plan.key:
                self.earlier.setdefault(room_id, []).append(older)
            self.finishing[room_id] = plan
            return self._score_due(now, room_id, temp)
        if waited is not None and waited[0] == plan.key:
            # It was due and never began: say so, rather than leave no trace of it.
            data = {**self._plan_data(plan), "outcome": "not_started", "reason": waited[1]}
            self.preheat_last[room_id] = data
            self._preheat_event("not_started", room_id, data)
        return False

    def _track(self, now, room_id, plan, temp, obs):
        """``plan`` with this cycle's arrival, peak and switchpoint capture taken in."""
        updated = plan
        if temp is not None:
            if plan.peak_temp is None or temp > plan.peak_temp:
                updated = replace(updated, peak_temp=temp)
            if plan.arrived_at is None and temp >= plan.goal - self._tolerance():
                updated = replace(updated, arrived_at=now)
        # Only a qualified reading is filed, and under the time its value changed: the
        # same value seen on a later tick, or written again by its integration, is the
        # same evidence, not more of it.
        if obs is not None and obs["qualified"] and obs["changed_at"] is not None:
            updated = note_report(updated, obs["changed_at"], obs["value"])
        if updated.capture is None and now >= plan.switch_at + CAPTURE_WINDOW:
            updated = finalise_capture(updated)
        if updated.arrived_at is not None and plan.arrived_at is None:
            self._preheat_event("arrived", room_id, self._arrival_data(updated, temp))
        return updated

    def _score_due(self, now, room_id, temp):
        """Score every finished pre-heat of the room whose hour is up, oldest first."""
        scored = False
        waiting = [*self.earlier.get(room_id, []), self.finishing.get(room_id)]
        for done in waiting:
            if done is None or now < done.switch_at + PREHEAT_SCORE_AFTER:
                continue
            if self.finishing.get(room_id) is done:
                del self.finishing[room_id]
            else:
                self.earlier[room_id] = [p for p in self.earlier[room_id] if p is not done]
            scored = True
            # Scored once: a copy of a pre-heat already scored (an older save, say) is
            # dropped without a second score, a second lesson or a second journal entry.
            if f"{room_id}|{done.key}" not in self.scored_keys:
                data = self._arrival_data(done, temp)
                data.update(self._learn_lead(room_id, done))
                self.preheat_last[room_id] = data
                self._preheat_event("scored", room_id, data)
        if not self.earlier.get(room_id):
            self.earlier.pop(room_id, None)
        return scored

    def _advance_plan(self, now, room_id, c, temp, records, view):
        """Track arrival, peak and the switchpoint capture of every started plan, flag
        HHO's command, score the ones whose hour is up."""
        changed = False
        obs = self._observation(room_id, now)
        older = self.earlier.get(room_id)
        if older:
            tracked = [self._track(now, room_id, plan, temp, obs) for plan in older]
            changed |= tracked != older
            self.earlier[room_id] = tracked
        for store in (self.finishing, self.plans):
            plan = store.get(room_id)
            if plan is None or not plan.started:
                continue
            updated = self._track(now, room_id, plan, temp, obs)
            if updated != plan:
                store[room_id] = updated
                changed = True
        live = self.plans.get(room_id)
        if live is not None:
            view["plan"] = live
            view["on_plan_k"] = live.on_plan_k(now, temp)
            if live.started and self.preheat_mode == "active" and c.data.preheating:
                for rec in records:
                    if rec["key"] == live.key and not rec.get("hho_commanded"):
                        rec["hho_commanded"] = live.begin.timestamp()
        changed |= self._score_due(now, room_id, temp)
        return changed

    def preheat(self, room_id, now=None):
        """The room's plan for its next upward switchpoint, or None when it has none.

        The plan is latched as ``started`` the first time this is called at or after the
        planned start, unless hot water is charging (a pre-heat never begins mid-charge, and
        one already under way carries on). This is the one place the boiler side should read
        a room's pre-heat from: ``plan.started`` means it is warming now, ``plan.goal`` is
        the target and ``plan.expected_at(now)`` where it should be.
        """
        now = now or dt_util.utcnow()
        plan = self.plans.get(room_id)
        if plan is None or now >= plan.switch_at:
            return None
        if not plan.started and now >= plan.start_at:
            reason = "planned"
            if self._dhw_blocking():
                if not plan.dhw_held:
                    # Remembered from now on: the charge may be over within the minute.
                    plan = replace(plan, dhw_held=True)
                    self.plans[room_id] = plan
                    self._save_soon()
                # A charge is running: wait for it, but not past the grace after its expected
                # end, nor past the latest start that still reaches the goal at boost flow.
                since = self._dhw_since or now
                end = since + timedelta(minutes=charge_length(self.dhw_charges))
                deadline, reason = dhw_wait_deadline(plan, end)
                if now < deadline:
                    return plan
            temp = self._read(room_id, now)[0]
            if temp is None:
                # The plan was made from a reading that can no longer be vouched for:
                # it waits, and begins only if the sensor is heard from before the
                # switchpoint. Otherwise the room follows its schedule.
                why = "no current reading from the room's sensor"
                if self.waiting_plans.get(room_id) != (plan.key, why):
                    self.waiting_plans[room_id] = (plan.key, why)
                    self._preheat_event(
                        "waiting", room_id, {**self._plan_data(plan), "reason": why}
                    )
                return plan
            self.waiting_plans.pop(room_id, None)
            plan = plan.start(now, temp, reason, self.cfg["margin_minutes"])
            self.plans[room_id] = plan
            self._preheat_event("started", room_id, self._plan_data(plan))
            self._save_soon()
        return plan

    @staticmethod
    def _lead_key(room_id, switch_at):
        return f"{room_id}|{dt_util.as_local(switch_at).strftime('%H:%M')}"

    def _lead_bias(self, room_id, switch_at) -> float:
        """Extra lead (minutes) learnt for this room at this time of day; 0 when off."""
        if not self.pcfg.get("arrival_gain"):
            return 0.0
        return float(self.lead_bias.get(self._lead_key(room_id, switch_at), 0.0))

    def set_lead(self, room_id, time_of_day, minutes) -> dict:
        """Set the extra lead for one room's warm-up at one time of day, by hand.

        The learning carries on from the new value: a room that then arrives over its
        goal starts later again. Kept within the same range the learning keeps to.
        """
        key = f"{room_id}|{time_of_day}"
        before = float(self.lead_bias.get(key, 0.0))
        after = round(max(LEAD_BIAS_MIN, min(LEAD_BIAS_MAX, float(minutes))), 1)
        self.lead_bias[key] = after
        data = {"time_of_day": time_of_day, "before_minutes": before, "lead_bias_minutes": after}
        self._preheat_event("lead_set", room_id, data)
        self._save_soon()
        return {"room_id": room_id, **data, "learning": bool(self.pcfg.get("arrival_gain"))}

    def _learn_lead(self, room_id, plan) -> dict:
        """Take a share of this arrival's error into the next start; what was learnt.

        Once per pre-heat: its key is remembered with the bias, in the same save, so a
        restart can neither apply it twice nor lose one that was applied.
        """
        gain = self.pcfg.get("arrival_gain") or 0.0
        finished = finalise_capture(plan)
        error = lead_error_minutes(finished)
        key = self._lead_key(room_id, plan.switch_at)
        scored = f"{room_id}|{plan.key}"
        before = float(self.lead_bias.get(key, 0.0))
        after = before
        if scored not in self.scored_keys:
            if self.preheat_mode == "active":
                after = learn_lead(before, error, gain)
            self.scored_keys = [*self.scored_keys, scored][-MAX_SCORED_KEYS:]
            self._dirty = True
        if after != before:
            self.lead_bias[key] = after
        return {
            "switch_temperature": finished.switch_temp,
            "capture": finished.capture,
            "interrupted": plan.interrupted,
            "lead_error_minutes": error,
            "lead_bias_minutes": after,
        }

    def _plan_data(self, plan):
        return {
            "mode": self.preheat_mode,
            "switchpoint": _iso(plan.switch_at),
            "start_at": _iso(plan.start_at),
            "started_at": _iso(plan.started_at),
            "start_reason": plan.start_reason,
            "dhw_wait_minutes": plan.dhw_wait_minutes,
            "schedule_target": plan.schedule_target,
            "goal": plan.goal,
            "start_temperature": plan.start_temp,
            "planned_minutes": plan.planned_minutes,
            "delay_minutes": plan.delay_minutes,
            "climb_minutes": plan.climb_minutes,
            "late_risk_minutes": plan.late_risk_minutes,
            "plan_flow": self.pcfg["plan_flow"],
            "plan_factor": plan.plan_factor,
            "lead_bias_minutes": plan.lead_bias_minutes,
            "interrupted": plan.interrupted,
            "dhw_held": plan.dhw_held,
            "outcome": "planned" if not plan.started else "started",
        }

    def _arrival_data(self, plan, temp):
        arrived = plan.arrived_at
        return {
            **self._plan_data(plan),
            "arrived_at": _iso(arrived),
            "arrival_minutes_vs_switch": None
            if arrived is None
            else round((arrived - plan.switch_at).total_seconds() / 60, 1),
            "peak_temperature": plan.peak_temp,
            "overshoot_k": None
            if plan.peak_temp is None
            else round(max(0.0, plan.peak_temp - plan.goal), 2),
            "temperature": temp,
            "outcome": (
                "interrupted"
                if plan.interrupted
                else ("arrived" if arrived is not None else "not_arrived")
            ),
        }

    def _preheat_event(self, event, room_id, data):
        self._journal("preheat", event, room_id, data)

    def preheat_report(self):
        """Mode and per-room plan summaries for the pre-heat status sensor."""
        controls = self.heating.controls
        now = dt_util.utcnow()
        rooms = {}
        for room_id, c in controls.rooms.items() if controls else ():
            plan = self.plans.get(room_id)
            view = self.latest.get(room_id) or {}
            temp = self._read(room_id, now)[0]
            obs = self._observation(room_id, now) or {}
            last = self.preheat_last.get(room_id) or {}
            rooms[room_id] = {
                "name": c.room_name,
                "planned_start": _iso(plan.start_at) if plan else None,
                "warm_by": _iso(plan.switch_at) if plan else None,
                "goal": plan.goal if plan else None,
                "started": plan.started if plan else False,
                "dhw_wait_minutes": plan.dhw_wait_minutes if plan else None,
                "on_plan_k": view.get("on_plan_k") if plan else None,
                "late_risk_minutes": plan.late_risk_minutes if plan else None,
                "expected_temperature": round(plan.expected_at(now), 2)
                if plan and plan.started
                else None,
                # What is planned from (None when the sensor has gone quiet), and the
                # reading behind it: where it came from, whether it may teach anything,
                # and how old it is.
                "temperature": temp,
                "reading": obs.get("value"),
                "reading_source": obs.get("source"),
                "reading_qualified": obs.get("qualified", False),
                "reading_quality": obs.get("quality", "no reading"),
                "reading_changed_at": _iso(obs.get("changed_at")),
                "reading_evidence": obs.get("evidence", "none"),
                "reading_evidence_minutes": obs.get("evidence_minutes"),
                "waiting": (self.waiting_plans.get(room_id) or (None, None))[1],
                "interrupted": plan.interrupted if plan else False,
                "lead_bias_minutes": self._lead_bias(room_id, plan.switch_at) if plan else None,
                "last_outcome": last.get("outcome"),
                "last_capture": last.get("capture"),
                "last_lead_error_minutes": last.get("lead_error_minutes"),
                "last_lead_bias_minutes": last.get("lead_bias_minutes"),
                "last_arrival_minutes_vs_switch": last.get("arrival_minutes_vs_switch"),
                "last_overshoot_k": last.get("overshoot_k"),
                "last_switchpoint": last.get("switchpoint"),
            }
        return {
            "plan_flow": self.pcfg["plan_flow"],
            "max_lead_minutes": self.pcfg["max_lead_minutes"],
            "dhw_charge_minutes": charge_length(self.dhw_charges),
            "max_source_age_minutes": MAX_SOURCE_AGE.total_seconds() / 60,
            "rooms": rooms,
        }

    def _score(self, room_id, rec):
        score = score_start(rec, self._tolerance())
        self.scores = [*self.scores, {"room_id": room_id, **score}][-MAX_SCORES:]
        self._event("start_scored", room_id, score)

    def _tolerance(self):
        return self.heating.config.get("comfort_tolerance", DEFAULT_TOLERANCE)

    def _event(self, event, room_id, data, routine=False):
        self._journal("optimum", event, room_id, data, routine)

    def _journal(self, kind, event, room_id, data, routine=False):
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return
        try:
            journal.record(
                kind,
                room_id=room_id,
                scope=room_id,
                origin="controller",
                data={"event": event, "outcome": event, **data},
                routine=routine,
            )
        except Exception:  # noqa: BLE001
            pass

    # -- status ------------------------------------------------------------

    @property
    def status(self):
        if not self.cfg["enabled"]:
            return "disabled"
        if self.analytics is None:
            return "needs_analytics"
        return "ready" if any(f.status == "ready" for f in self.fits.values()) else "learning"

    @property
    def waiting_reason(self):
        return self.waiting

    def room_view(self, room_id):
        """Start and stop predictions for a room sensor: (start_at, stop_at, attributes)."""
        view = self.latest.get(room_id) or {}
        fit = self.fits.get(room_id)
        start, stop = view.get("start"), view.get("stop")
        pred = start.get("prediction") if start else None
        model = fit.model if fit else None
        attrs = {
            "status": fit.status if fit else "learning",
            "confidence": fit.confidence if fit else "none",
            "refit_pending": self.refit_pending and fit is not None,
            "events": fit.events if fit else 0,
            "minutes_per_k": round(model.minutes_per_k, 2) if model else None,
            "outdoor_model": model.outdoor_model if model else False,
            "loo_error_min": model.loo_error_min if model else None,
            "median_supply": model.median_supply if model else None,
            "delay_minutes": model.delay_minutes if model else None,
            "cooldowns": fit.cooldowns if fit else 0,
            "coast_rate": fit.coast_rate if fit else None,
            "coast_up_k": fit.coast_up_k if fit else None,
            "coast_up_samples": fit.coast_up_samples if fit else 0,
            "stop_reason": fit.coast_reason if fit else "insufficient cool-downs",
            "last_events": fit.last_events if fit else [],
            "outdoor_source": self.outdoor_source,
            "last_score": next(
                (s for s in reversed(self.scores) if s.get("room_id") == room_id), None
            ),
        }
        if start and pred:
            attrs.update(
                target=start["schedule_target"],
                goal=start["target"],
                switchpoint=_iso(dt_util.utc_from_timestamp(start["switch_ts"])),
                lead_minutes=pred.lead_minutes,
                late_minutes=pred.late_minutes,
                heating_minutes=pred.heating_minutes,
                delay_minutes=pred.delay_minutes,
                boost_flow=start["boost_flow"],
                boost_lead_minutes=pred.boost_lead_minutes,
                boost_gain_minutes=pred.boost_gain_minutes,
            )
        elif warmup := view.get("warmup"):
            # No prediction yet: say which warm-up is next and when predicting starts.
            at, low, high = warmup
            begins = at - timedelta(minutes=self.max_lead_minutes)
            attrs.update(
                next_warmup=_iso(at),
                next_warmup_from=low,
                next_warmup_to=high,
                predicting_from=_iso(max(begins, dt_util.utcnow())),
            )
        plan = view.get("plan")
        if plan is not None:
            attrs.update(
                planned_start=_iso(plan.start_at),
                planned_minutes=plan.planned_minutes,
                late_risk_minutes=plan.late_risk_minutes,
                preheat_started=plan.started,
                dhw_wait_minutes=plan.dhw_wait_minutes,
                on_plan_k=view.get("on_plan_k"),
            )
        if stop:
            attrs.update(stop_coast_minutes=stop["coast_minutes"], stop_rate=stop["rate"])
        return (pred.start_at if pred else None), (stop["off_at"] if stop else None), attrs

    def report(self):
        controls = self.heating.controls
        rooms = {}
        for room_id, c in controls.rooms.items() if controls else ():
            start_at, stop_at, attrs = self.room_view(room_id)
            rooms[room_id] = {
                "name": c.room_name,
                "start_at": _iso(start_at),
                "stop_at": _iso(stop_at),
                **attrs,
            }
        return {
            "status": self.status,
            "shadow": True,
            "outdoor_source": self.outdoor_source,
            "forecast_entity": self.cfg["forecast_entity"],
            "forecast_fetched_at": _iso(self.forecast.fetched_at),
            "waiting": self.waiting,
            # True while the fits shown were built before duplicate warm-ups were removed
            # from the stored events; the next learning pass rebuilds them.
            "refit_pending": self.refit_pending,
            "learned_at": _iso(self.learned_at),
            # Extra lead learnt from arrivals, by room and switch time, and how many
            # pre-heats have been scored into it.
            "lead_bias": dict(self.lead_bias),
            "scored_preheats": len(self.scored_keys),
            "scores": rolling(self.scores),
            "rooms": rooms,
            "last_error": self.last_error,
        }

    def quality(self):
        """Counts and status only, for diagnostics."""
        return {
            "status": self.status,
            "rooms_ready": sum(f.status == "ready" for f in self.fits.values()),
            "rooms_learning": sum(f.status != "ready" for f in self.fits.values()),
            "scored": len(self.scores),
        }


def _number(value):
    try:
        number = float(value)
    except TypeError, ValueError:
        return None
    return number if number == number and abs(number) != float("inf") else None
