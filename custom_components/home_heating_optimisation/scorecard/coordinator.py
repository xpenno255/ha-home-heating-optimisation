"""Collects the daily scorecard from the running controllers; never writes to a device.

Optional evidence: every entry point swallows its own errors so a fault here cannot
disturb heating control.
"""

import logging
from datetime import datetime, time, timedelta

from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.storage import Store
from homeassistant.util import dt as dt_util

from ..energy.meter import meter_specs, to_kwh
from .core import (
    BASE_C,
    MAX_DAYS,
    MAX_STEP_MINUTES,
    METER_FRESH_MINUTES,
    METER_MAX_SILENCE_MINUTES,
    SHORT_K,
    TOLERANCE_K,
    add_gas,
    add_meter_gas,
    add_sample,
    add_write,
    burner_summary,
    empty_day,
    finalise,
    gas_delta,
    restore_day,
)

LOGGER = logging.getLogger(__name__)
SCHEMA = 1
SAMPLE_SECONDS = 60
SAVE_EVERY_SAMPLES = 10
SENSOR_DAYS = 14


class Scorecard:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.store = Store(hass, SCHEMA, f"home_heating_optimisation.{entry.entry_id}.scorecard")
        self.days: list[dict] = []
        self.current: dict | None = None
        self.meter: dict | None = None  # last fuel reading: {"kwh", "at"}
        self.listeners = []
        self.status = "starting"
        self._last_sample: datetime | None = None
        self._last_write: dict = {}
        self._unsub = None
        self._samples = 0
        self._failed = False
        specs = [s for s in meter_specs(heating.config) if s["kind"] == "fuel_input"]
        self.meter_spec = specs[0] if specs else None
        # The supplier's meter, when one is configured: slow, but it is what is billed.
        self.billing_entity = (heating.config.get("energy") or {}).get("billing_meter") or None
        self.billing: dict | None = None  # last reading: {"kwh", "changed", "seen"}

    async def initialise(self):
        data = await self.store.async_load()
        if isinstance(data, dict):
            days = data.get("days")
            if isinstance(days, list):
                self.days = [d for d in days if isinstance(d, dict) and d.get("date")]
            self.current = restore_day(data.get("current"))
            meter = data.get("meter")
            if isinstance(meter, dict) and "kwh" in meter and "at" in meter:
                self.meter = meter
            billing = data.get("billing")
            if isinstance(billing, dict) and all(k in billing for k in ("kwh", "changed", "seen")):
                self.billing = billing
        self.status = "ready"

    def start(self):
        self._unsub = async_track_time_interval(
            self.hass, self.sample, timedelta(seconds=SAMPLE_SECONDS)
        )

    async def stop(self):
        if self._unsub:
            self._unsub()
            self._unsub = None
        await self._save()

    async def _save(self):
        try:
            await self.store.async_save(
                {
                    "days": self.days,
                    "current": self.current,
                    "meter": self.meter,
                    "billing": self.billing,
                }
            )
        except Exception:  # noqa: BLE001 - evidence only
            self.status = "save_failed"
            LOGGER.warning("Daily scorecard could not be saved")

    async def sample(self, now=None):
        """One sample; rolls the day over at local midnight. Never raises."""
        try:
            rolled = self._sample(dt_util.utcnow())
            self._samples += 1
            if rolled or self._samples % SAVE_EVERY_SAMPLES == 0:
                await self._save()
                for listener in list(self.listeners):
                    listener()
            self._failed = False
        except Exception:  # noqa: BLE001 - evidence only
            if not self._failed:
                LOGGER.exception("Daily scorecard sample failed")
            self._failed = True

    def _bounds(self, date: str):
        day = datetime.fromisoformat(date).date()
        zone = dt_util.get_default_time_zone()
        start = datetime.combine(day, time.min, tzinfo=zone)
        end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
        return start.timestamp(), end.timestamp()

    def _burner(self, date: str):
        boiler = getattr(self.heating.controls, "boiler", None)
        if boiler is None:
            return None
        return burner_summary(boiler.burn_rows(), *self._bounds(date))

    def _sample(self, now: datetime) -> bool:
        controls = self.heating.controls
        date = dt_util.as_local(now).date().isoformat()
        rolled = False
        if self.current is not None and self.current["date"] != date:
            row = finalise(self.current, self._burner(self.current["date"]))
            self.days = [d for d in self.days if d["date"] != row["date"]]
            self.days = sorted([*self.days, row], key=lambda d: d["date"])[-MAX_DAYS:]
            self.current, rolled = None, True
        if self.current is None:
            self.current = empty_day(date)
        day = self.current
        minutes = SAMPLE_SECONDS / 60
        if self._last_sample is not None:
            minutes = (now - self._last_sample).total_seconds() / 60
        self._last_sample = now
        boiler = getattr(controls, "boiler", None)
        bdata = boiler.data if boiler is not None else None
        dhw = bool(bdata.dhw_active) if bdata is not None else False
        self._gas(day, now, dhw)
        meter_live = self._billing(day, now)
        rooms = {}
        for room_id, c in (getattr(controls, "rooms", None) or {}).items():
            d = c.data
            if d is None:
                continue
            rooms[room_id] = {
                "name": c.room_name,
                "state": d.state,
                "in_window": d.time_window_active,
                "air": d.air_temp,
                "target": d.air_setpoint,
            }
            stamp = d.last_write.isoformat() if d.last_write else None
            if room_id in self._last_write and stamp and stamp != self._last_write[room_id]:
                add_write(day, room_id, c.room_name)
            self._last_write[room_id] = stamp
        add_sample(
            day,
            minutes,
            boiler=None
            if bdata is None
            else {
                "outdoor": bdata.outdoor_temp,
                "dhw": dhw,
                "power": bdata.burner_power,
                "return_c": bdata.return_temperature_used if bdata.return_fresh else None,
            },
            rooms=rooms,
            meter_live=meter_live,
        )
        return rolled

    def _billing(self, day, now) -> bool:
        """Read the billing meter; True while it is reporting."""
        if not self.billing_entity:
            return False
        state = self.hass.states.get(self.billing_entity)
        if state is None:
            return False
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return False
        spec = self.meter_spec or {}
        kwh = to_kwh(
            value,
            state.attributes.get("unit_of_measurement"),
            **{k: spec[k] for k in ("calorific_value_mj_m3", "volume_correction") if spec.get(k)},
        )
        if kwh is None:
            return False
        reported = getattr(state, "last_reported", None) or state.last_updated
        if (now - reported).total_seconds() / 60 > METER_FRESH_MINUTES:
            return False
        last = self.billing
        changed = state.last_changed.isoformat()
        if last is None:
            self.billing = {"kwh": kwh, "changed": changed, "seen": now.isoformat()}
            return True
        if kwh != last["kwh"]:
            try:
                silence = (now - datetime.fromisoformat(last["seen"])).total_seconds() / 60
                since = (now - datetime.fromisoformat(last["changed"])).total_seconds() / 60
            except TypeError, ValueError:
                silence = since = None
            # After a long silence the jump covers time nobody watched: leave it out.
            if silence is not None and silence <= METER_MAX_SILENCE_MINUTES:
                delta = gas_delta(last["kwh"], kwh, since)
                if delta is not None:
                    add_meter_gas(day, delta)
            last = {"kwh": kwh, "changed": changed}
        self.billing = {**last, "seen": now.isoformat()}
        return True

    def _gas(self, day, now, dhw):
        spec = self.meter_spec
        if spec is None:
            return
        state = self.heating.telemetry.get(spec["entity"])
        if state is None:
            return
        try:
            value = float(state.state)
        except TypeError, ValueError:
            return
        kwh = to_kwh(
            value,
            spec["unit"] or state.attributes.get("unit_of_measurement"),
            calorific_value_mj_m3=spec["calorific_value_mj_m3"],
            volume_correction=spec["volume_correction"],
        )
        if kwh is None:
            return
        if self.meter is not None:
            try:
                elapsed = (now - datetime.fromisoformat(self.meter["at"])).total_seconds() / 60
            except TypeError, ValueError:
                elapsed = None
            delta = gas_delta(self.meter["kwh"], kwh, elapsed)
            if delta is not None:
                add_gas(day, delta, dhw=dhw, split=elapsed <= MAX_STEP_MINUTES)
        self.meter = {"kwh": kwh, "at": now.isoformat()}

    def today(self):
        if self.current is None:
            return None
        return finalise(self.current, self._burner(self.current["date"]))

    def latest(self):
        return self.days[-1] if self.days else None

    def report(self, days=30):
        return {
            "status": self.status,
            "definitions": {
                "degree_day_base_c": BASE_C,
                "on_target_band_k": TOLERANCE_K,
                "short_below_k": SHORT_K,
                "comfort": "room air against its air target before overdrive, only while the "
                "room is in normal control",
                "gas": "billing meter for the amount when it covered the day, else the boiler "
                "counter; the boiler counter always gives the heating / hot water split",
                "boiler_counter": spec["entity"] if (spec := self.meter_spec) else None,
                "billing_meter": self.billing_entity,
            },
            "today": self.today(),
            "days": self.days[-days:],
        }

    def sensor_view(self):
        compact = [{k: v for k, v in d.items() if k != "rooms"} for d in self.days[-SENSOR_DAYS:]]
        return {"today": self.today(), "days": compact}
