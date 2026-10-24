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
from .learning import RoomFit, fit_all
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
    CHARGE_RANGE,
    MAX_CHARGES,
    PreheatPlan,
    avoid_charge,
    charge_length,
    dhw_wait_deadline,
    next_dhw_on,
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
        self.finishing = {}  # room id -> started plan past its switchpoint, awaiting its score
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
                self.events = {
                    k: [e for e in v if isinstance(e, dict)]
                    for k, v in (data.get("events") or {}).items()
                    if isinstance(v, list)
                }
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

    async def _save(self):
        try:
            await self._store.async_save(
                {
                    "schema": SCHEMA,
                    "scores": self.scores,
                    "fits": {k: asdict(v) for k, v in self.fits.items()},
                    "events": self.events,
                    "plans": {k: v.to_dict() for k, v in self.plans.items()},
                    "dhw_charges": self.dhw_charges,
                    "preheat_last": self.preheat_last,
                }
            )
        except Exception:  # noqa: BLE001
            LOGGER.warning("Optimum store save failed", exc_info=True)

    # -- inputs ------------------------------------------------------------

    @property
    def analytics(self):
        return getattr(self.heating, "analytics", None)

    def _room_config(self, room_id):
        rooms = self.heating.config.get("rooms") or []
        return next((r for r in rooms if r.get("id") == room_id), None)

    def _read(self, room_id):
        """(zone temperature, zone setpoint) as the analytics observe them."""
        room = self._room_config(room_id)
        if room is None:
            return None, None
        climate = self.hass.states.get(room["climate"])
        setpoint = _number(climate.attributes.get("temperature")) if climate else None
        if room.get("air_sensor"):
            state = self.hass.states.get(room["air_sensor"])
            temp = _number(state.state) if state else None
        else:
            temp = _number(climate.attributes.get("current_temperature")) if climate else None
        return temp, setpoint

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
        await self._save()

    def _room(self, now, room_id, c, outdoor_at, source, current):
        ts = now.timestamp()
        temp, setpoint = self._read(room_id)
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
        self._retire_plan(now, room_id)
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
        start_at, late = avoid_charge(
            pred.start_at,
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
            climb_minutes=max(0.0, pred.heating_minutes - pred.delay_minutes),
            planned_minutes=max(0, round((switch_at - start_at).total_seconds() / 60)),
            late_risk_minutes=late,
            plan_factor=pred.plan_factor,
            latest_start=switch_at
            - timedelta(minutes=pred.boost_lead_minutes or pred.lead_minutes),
            planned_journaled=same.planned_journaled if same else False,
        )
        if not plan.planned_journaled:
            plan = replace(plan, planned_journaled=True)
            self._preheat_event("planned", room_id, self._plan_data(plan))
        if plan != old:
            self._dirty = True
        self.plans[room_id] = plan

    def _retire_plan(self, now, room_id):
        """Past its switchpoint a started plan waits for its score; others are dropped."""
        plan = self.plans.get(room_id)
        if plan is None or now < plan.switch_at:
            return
        del self.plans[room_id]
        self._dirty = True
        if plan.started:
            self.finishing[room_id] = plan

    def _advance_plan(self, now, room_id, c, temp, records, view):
        """Track arrival and peak of a started plan, flag HHO's command, score old ones."""
        tol = self._tolerance()
        changed = False
        for plan in (self.finishing.get(room_id), self.plans.get(room_id)):
            if plan is None or not plan.started or temp is None:
                continue
            fields = {}
            if plan.peak_temp is None or temp > plan.peak_temp:
                fields["peak_temp"] = temp
            if plan.arrived_at is None and temp >= plan.goal - tol:
                fields["arrived_at"] = now
            if fields:
                updated = replace(plan, **fields)
                store = self.plans if self.plans.get(room_id) is plan else self.finishing
                store[room_id] = updated
                changed = True
                if "arrived_at" in fields:
                    self._preheat_event("arrived", room_id, self._arrival_data(updated, temp))
        live = self.plans.get(room_id)
        if live is not None:
            view["plan"] = live
            view["on_plan_k"] = live.on_plan_k(now, temp)
            if live.started and self.preheat_mode == "active" and c.data.preheating:
                for rec in records:
                    if rec["key"] == live.key and not rec.get("hho_commanded"):
                        rec["hho_commanded"] = live.begin.timestamp()
        done = self.finishing.get(room_id)
        if done is not None and now >= done.switch_at + PREHEAT_SCORE_AFTER:
            del self.finishing[room_id]
            data = self._arrival_data(done, temp)
            self.preheat_last[room_id] = data
            self._preheat_event("scored", room_id, data)
            changed = True
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
                # A charge is running: wait for it, but not past the grace after its expected
                # end, nor past the latest start that still reaches the goal at boost flow.
                since = self._dhw_since or now
                end = since + timedelta(minutes=charge_length(self.dhw_charges))
                deadline, reason = dhw_wait_deadline(plan, end)
                if now < deadline:
                    return plan
            plan = plan.start(now, self._read(room_id)[0], reason, self.cfg["margin_minutes"])
            self.plans[room_id] = plan
            self._dirty = True
            self._preheat_event("started", room_id, self._plan_data(plan))
        return plan

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
            "outcome": "arrived" if arrived is not None else "not_arrived",
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
            temp = self._read(room_id)[0]
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
                "temperature": temp,
                "last_arrival_minutes_vs_switch": last.get("arrival_minutes_vs_switch"),
                "last_overshoot_k": last.get("overshoot_k"),
                "last_switchpoint": last.get("switchpoint"),
            }
        return {
            "plan_flow": self.pcfg["plan_flow"],
            "max_lead_minutes": self.pcfg["max_lead_minutes"],
            "dhw_charge_minutes": charge_length(self.dhw_charges),
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
