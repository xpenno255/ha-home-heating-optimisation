"""v2 coordinator: gather inputs from Home Assistant, run model then policy, act once.

All decisions live in `core.model` and `core.policy`. This module is glue: it
reads entities, converts units, loads the room geometry, calls the pure
functions, performs the single service call the policy asked for, and
publishes a snapshot for the entities.
"""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_conversion import TemperatureConverter

from .const import (
    ACTUATOR_CALL_TIMEOUT,
    AFTERNOON_END,
    CONF_ADAPTIVE_ENABLED,
    CONF_ASYMMETRY_ENABLED,
    CONF_BACKUP_CLIMATE,
    CONF_CAP_DOWN,
    CONF_CAP_UP,
    CONF_DHW_ACTIVE_ENTITY,
    CONF_FLOW_TEMP_ENTITY,
    CONF_GROUND_TEMP,
    CONF_HOUSE_DIR,
    CONF_IRRADIANCE_SENSOR,
    CONF_MANUAL_FLOW_TEMP,
    CONF_MANUAL_HOLD,
    CONF_MODE,
    CONF_NAME,
    CONF_OCCUPANCY_SENSOR,
    CONF_OUTDOOR_TEMP_SENSOR,
    CONF_OVERRIDE_DURATION,
    CONF_PREHEAT_RELEASE,
    CONF_PRIMARY_CLIMATE,
    CONF_ROOM_FILE,
    CONF_ROOM_ID,
    CONF_RUN_INTERVAL,
    CONF_TIME_WINDOW_ENABLED,
    CONF_TIME_WINDOW_END,
    CONF_TIME_WINDOW_START,
    CONF_TRUST_K,
    CONF_UNOCCUPIED_DURATION,
    CONF_WEATHER_ENTITY,
    CONF_WEEKDAY_AFTERNOON_OFFSET,
    CONF_WEEKDAY_EVENING_OFFSET,
    CONF_WEEKDAY_MORNING_OFFSET,
    CONF_WEEKEND_AFTERNOON_OFFSET,
    CONF_WEEKEND_EVENING_OFFSET,
    CONF_WEEKEND_MORNING_OFFSET,
    CONF_WINDOW_DELAY,
    CONF_WINDOW_OPEN_DELAY,
    CONF_WINDOW_SETPOINT,
    CONF_ZONE_SETPOINT_MAX,
    CONF_ZONE_SETPOINT_MIN,
    CYCLE_TIMEOUT,
    DEFAULT_CAP,
    DEFAULT_GROUND_TEMP,
    DEFAULT_HOUSE_DIR,
    DEFAULT_MANUAL_FLOW_TEMP,
    DEFAULT_MANUAL_HOLD,
    DEFAULT_MODE,
    DEFAULT_OVERRIDE_DURATION,
    DEFAULT_PREHEAT_RELEASE,
    DEFAULT_RUN_INTERVAL,
    DEFAULT_TIME_WINDOW_ENABLED,
    DEFAULT_TIME_WINDOW_END,
    DEFAULT_TIME_WINDOW_START,
    DEFAULT_TRUST_K,
    DEFAULT_UNOCCUPIED_DURATION,
    DEFAULT_WEEKDAY_AFTERNOON_OFFSET,
    DEFAULT_WEEKDAY_EVENING_OFFSET,
    DEFAULT_WEEKDAY_MORNING_OFFSET,
    DEFAULT_WEEKEND_AFTERNOON_OFFSET,
    DEFAULT_WEEKEND_EVENING_OFFSET,
    DEFAULT_WEEKEND_MORNING_OFFSET,
    DEFAULT_WINDOW_DELAY,
    DEFAULT_WINDOW_OPEN_DELAY,
    DEFAULT_WINDOW_SETPOINT,
    DEFAULT_ZONE_SETPOINT_MAX,
    DEFAULT_ZONE_SETPOINT_MIN,
    DOMAIN,
    ENTITY_AT_HOME_MODE,
    ENTITY_HOLIDAY_MODE,
    EVENING_END,
    MODE_ACTIVE,
    MORNING_END,
    MORNING_START,
    OUTDOOR_CACHE_MAX_AGE_H,
    THERMOSTAT_STEP,
)
from .core.geometry import RoomGeometry, load_house, load_room
from .core.model import (
    Correction,
    Environment,
    ModelParams,
    operative_temperature,
    radiator_output_w,
    required_air_temperature,
    steady_state_mrt,
)
from .core.policy import (
    Action,
    Decision,
    OverrideMemory,
    PolicyInputs,
    PolicyParams,
    State,
    ZoneState,
    decide,
)
from .hub import OTHubData
from .schedule_fetch import ScheduleFetcher
from .store import OTStore

_LOGGER = logging.getLogger(__name__)

UNAVAILABLE = ("unknown", "unavailable", "", None)

# Absolute actuator limits accepted by evohome. Configured policy bounds must
# stay inside this range.
ZONE_SETPOINT_MIN = 5.0
ZONE_SETPOINT_MAX = 35.0

# A service response only tells us that Home Assistant accepted the command.  A
# primary thermostat report newer than the command is required before calling it
# confirmed.  This is deliberately runtime-only: a restart cannot turn an old
# device state into an acknowledgement for a command this coordinator did not
# send.
READBACK_TIMEOUT = timedelta(minutes=3)
STARTUP_SETTLE_TIME = timedelta(minutes=2)
READBACK_MAX_AGE = timedelta(minutes=5)
# Same-temperature renewals stay unverified because RAMSES exposes no inbound
# packet provenance to Home Assistant (docs/renewal-acknowledgement-2026-09-20.md).
# These hints describe what the status means and what to check.  They must not
# claim the command was lost or that a valve did or did not move.
READBACK_HINTS = {
    "matching_readback_unverified": (
        "The thermostat reports the requested target, but no setpoint or mode "
        "transition distinguishes this command from earlier state; an expiry-only "
        "change is not accepted for a same-temperature renewal. This is an "
        "unverified acknowledgement, not evidence of a lost command. If the zone "
        "keeps reporting the target and the override expiry, no action is needed."
    ),
    "readback_timed_out": (
        "No qualifying primary thermostat report arrived within 3 minutes of the "
        "service call. Unconfirmed is not the same as undelivered: Home Assistant "
        "attributes cannot show whether a report came from the controller. Check "
        "the RAMSES gateway status and the zone's reported mode and expiry; HHO "
        "keeps its normal bounded retry and does not clear manual holds."
    ),
}
COMFORT_MODEL_VERSION = "steady_state_ot_v2"


@dataclass
class OTCoordinatorData:
    """Snapshot published to entities after each cycle."""

    state: str = State.NO_DATA.value
    reason: str = ""
    action: str = Action.NONE.value
    mode: str = DEFAULT_MODE
    enabled: bool = True
    last_run: datetime | None = None
    last_write: datetime | None = None
    last_written_setpoint: float | None = None
    # Command lifecycle.  `sent` means HA completed the service call; only
    # `confirmed` means the primary thermostat subsequently reported the
    # requested setpoint.
    requested_target: float | None = None
    sent_target: float | None = None
    pending_target: float | None = None
    confirmed_target: float | None = None
    requested_at: datetime | None = None
    sent_at: datetime | None = None
    pending_since: datetime | None = None
    confirmed_at: datetime | None = None
    readback_at: datetime | None = None
    readback_status: str = "not_attempted"
    readback_timed_out: bool = False
    # Actionable explanation for an unconfirmed outcome.  It never asserts
    # failed delivery or valve actuation; see docs/command-confirmation.md.
    readback_hint: str = ""
    write_status: str = "not_attempted"
    # Target
    schedule_setpoint: float | None = None
    schedule_source: str = "none"
    # Optional RF schedule download diagnostics (never on the control path).
    schedule_fetch_status: str = "not_attempted"
    schedule_fetch_failure_class: str | None = None
    schedule_fetch_attempts: int = 0
    schedule_next_retry_at: datetime | None = None
    model_version: str = COMFORT_MODEL_VERSION
    target_ot: float | None = None
    adaptive_shift: float = 0.0
    occupancy_status: str = "no_sensor"
    occupancy_offset: float = 0.0
    next_switchpoint_at: datetime | None = None
    next_switchpoint_setpoint: float | None = None
    # Room state
    air_temp: float | None = None
    air_temp_source: str = ""
    zone_setpoint: float | None = None
    # Model
    mrt_steady_state: float | None = None
    operative_temp: float | None = None
    offset_physical: float | None = None
    offset_trusted: float | None = None
    offset_asymmetry: float | None = None
    offset_final: float | None = None
    air_setpoint: float | None = None
    would_write: float | None = None
    capped: bool = False
    solar_k: float = 0.0
    sum_l: float | None = None
    # Environment
    outdoor_temp: float | None = None
    outdoor_source: str = ""
    wind_ms: float | None = None
    ghi_wm2: float | None = None
    cloud_fraction: float | None = None
    running_mean_outdoor: float | None = None
    flow_temp_used: float | None = None
    radiator_output_w: float | None = None
    installed_output_dt50_w: float | None = None
    # Overrides
    window_override_active: bool = False
    adjacent_door_open: bool = False
    time_window_active: bool = True
    # Diagnostics
    fallbacks: list[str] = field(default_factory=list)
    geometry_warnings: list[str] = field(default_factory=list)
    glazed_area_m2: float | None = None
    total_area_m2: float | None = None


class OTCoordinator(DataUpdateCoordinator[OTCoordinatorData]):
    """Coordinator for one room."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        store: OTStore,
        *,
        config=None,
        state_reader=None,
        write_guard=None,
        journal=None,
    ) -> None:
        self._cycle_lock = asyncio.Lock()
        self._entry = entry
        self._store = store
        self.journal = journal
        # Stable HHO room id (the control config key); Controls sets it after construction.
        self.journal_room_id: str | None = None
        self._journal_last: dict[str, Any] = {}
        self._config: dict[str, Any] = (
            dict(config) if config is not None else {**entry.data, **entry.options}
        )
        self._state_reader = state_reader or hass.states.get
        self._write_guard = write_guard or (lambda: False)
        self.write_status = "not_attempted"
        # Command/readback state is intentionally not persisted.  Policy memory
        # remains persistent exactly as before, but it is not an acknowledgement
        # from a device after an integration restart.
        self._requested_target: float | None = None
        self._sent_target: float | None = None
        self._pending_target: float | None = None
        self._confirmed_target: float | None = None
        self._requested_at: datetime | None = None
        self._sent_at: datetime | None = None
        self._pending_since: datetime | None = None
        self._confirmed_at: datetime | None = None
        self._readback_at: datetime | None = None
        self._readback_status = "not_attempted"
        self._readback_timed_out = False
        self._pre_send_setpoint: float | None = None
        self._pre_send_mode: str | None = None
        self._pre_send_until: datetime | str | None = None
        self._pre_send_owned = False
        self._service_echo_state: tuple[float, str | None, datetime | str | None] | None = None
        self._owned_command_state: tuple[float, str | None, datetime | str | None] | None = None
        self._service_completed_at: datetime | None = None
        self._command_reverted = False
        self._reversion_retry_used = False
        self._pending_action: Action | None = None
        self._enabled: bool = True
        self._occupancy_enabled: bool = True
        self._mode: str = str(self._config.get(CONF_MODE, DEFAULT_MODE))
        self._geometry: RoomGeometry | None = None
        self._geometry_error: str | None = None
        self._retry_cancel = None
        self._schedule_fetcher = ScheduleFetcher(hass, store)
        self._schedule_source = "none"
        self._restore_complete: bool = False  # set by setup once restored entity states are in
        self._startup_reconciliation_pending = any(
            store.get(key) is not None
            for key in (
                "last_written_setpoint",
                "last_written_at",
                "manual_detected_at",
                "manual_release_at",
                "manual_setpoint",
            )
        )
        self._startup_reconcile_after: datetime | None = None
        self._tunables: dict[str, float] = {}
        hub_cfg = (hass.data.get(DOMAIN, {}).get("hub") or {}).get("config") or {}
        for key, default in (
            (CONF_TRUST_K, DEFAULT_TRUST_K),
            (CONF_CAP_UP, DEFAULT_CAP),
            (CONF_CAP_DOWN, DEFAULT_CAP),
        ):
            stored = store.get(key)
            fallback = self._config.get(
                key, hub_cfg.get(key, default)
            )  # room override > hub default > built-in
            self._tunables[key] = float(stored if stored is not None else fallback)
        interval = timedelta(
            minutes=float(self._config.get(CONF_RUN_INTERVAL, DEFAULT_RUN_INTERVAL))
        )
        super().__init__(
            hass, _LOGGER, config_entry=entry, name=f"OT {self.room_name}", update_interval=interval
        )

    # ------------------------------------------------------------------
    # Properties used by entities
    # ------------------------------------------------------------------

    @property
    def room_name(self) -> str:
        return str(self._config.get(CONF_NAME, "Room"))

    @property
    def room_id(self) -> str:
        rid = self._config.get(CONF_ROOM_ID)
        return str(rid) if rid else self.room_name.lower().replace(" ", "_")

    @property
    def enabled(self) -> bool:
        return self._enabled

    @enabled.setter
    def enabled(self, value: bool) -> None:
        self._enabled = value

    @property
    def occupancy_enabled(self) -> bool:
        return self._occupancy_enabled

    @occupancy_enabled.setter
    def occupancy_enabled(self, value: bool) -> None:
        self._occupancy_enabled = value

    @property
    def mode(self) -> str:
        return self._mode

    @mode.setter
    def mode(self, value: str) -> None:
        self._mode = value

    @property
    def geometry(self) -> RoomGeometry | None:
        return self._geometry

    def get_tunable(self, key: str) -> float:
        return self._tunables[key]

    def set_tunable(self, key: str, value: float) -> None:
        self._tunables[key] = float(value)
        self._store.set(key, float(value))

    # ------------------------------------------------------------------
    # Geometry
    # ------------------------------------------------------------------

    def _house_dir(self) -> Path:
        hub = self._hub_config()
        raw = hub.get(CONF_HOUSE_DIR)
        if not raw:
            return Path(__file__).parent / DEFAULT_HOUSE_DIR  # shipped with the integration
        p = Path(str(raw))
        return p if p.is_absolute() else Path(self.hass.config.path(str(raw)))

    def _load_geometry_sync(self) -> RoomGeometry:
        house_dir = self._house_dir()
        house = load_house(house_dir / "house.yaml")
        room_file = self._config.get(CONF_ROOM_FILE)
        path = Path(room_file) if room_file else house_dir / "rooms" / f"{self.room_id}.yaml"
        return load_room(path, house)

    async def async_load_geometry(self) -> None:
        """(Re)load the room's survey file. Errors are kept, not raised."""
        try:
            self._geometry = await self.hass.async_add_executor_job(self._load_geometry_sync)
            self._geometry_error = None
            for w in self._geometry.warnings:
                _LOGGER.warning("OT %s geometry: %s", self.room_name, w)
        except Exception as exc:  # noqa: BLE001
            self._geometry = None
            self._geometry_error = f"{type(exc).__name__}: {exc}"
            _LOGGER.error(
                "OT %s: cannot load room geometry: %s", self.room_name, self._geometry_error
            )

    # ------------------------------------------------------------------
    # Small HA helpers
    # ------------------------------------------------------------------

    def _state(self, entity_id: str | None):
        return self._state_reader(entity_id) if entity_id else None

    def _float_state(self, entity_id: str | None) -> float | None:
        st = self._state(entity_id)
        if st is None or st.state in UNAVAILABLE:
            return None
        try:
            value = float(st.state)
            return value if math.isfinite(value) else None
        except ValueError, TypeError:
            return None

    def _float_attr(self, entity_id: str | None, attr: str) -> float | None:
        st = self._state(entity_id)
        # An unavailable entity retains its last attributes; they are stale, not data.
        if st is None or st.state in UNAVAILABLE:
            return None
        try:
            v = st.attributes.get(attr)
            value = float(v) if v is not None else None
            return value if value is not None and math.isfinite(value) else None
        except ValueError, TypeError:
            return None

    def _is_on(self, entity_id: str | None) -> bool | None:
        """True for binary 'on', or for a numeric sensor reading above zero (e.g. a relay demand %)."""
        st = self._state(entity_id)
        if st is None or st.state in UNAVAILABLE:
            return None
        if st.state in ("on", "off"):
            return st.state == "on"
        try:
            return float(st.state) > 0.0
        except TypeError, ValueError:
            return None

    def _temperature_state(self, entity_id: str | None) -> float | None:
        value, state = self._float_state(entity_id), self._state(entity_id)
        unit = state.attributes.get("unit_of_measurement", "°C") if state else "°C"
        return self._celsius(value, unit)

    def _temperature_attr(self, entity_id: str | None, attr: str) -> float | None:
        value, state = self._float_attr(entity_id, attr), self._state(entity_id)
        unit = (
            state.attributes.get("temperature_unit", self.hass.config.units.temperature_unit)
            if state
            else "°C"
        )
        return self._celsius(value, unit)

    @staticmethod
    def _celsius(value: float | None, unit: str) -> float | None:
        if value is None:
            return None
        try:
            converted = TemperatureConverter.convert(value, unit, "°C")
            return converted if math.isfinite(converted) else None
        except ValueError, TypeError, HomeAssistantError:
            return None

    def _hub(self) -> OTHubData | None:
        info = self.hass.data.get(DOMAIN, {}).get("hub")
        return info["data"] if info else None

    def _hub_config(self) -> dict[str, Any]:
        info = self.hass.data.get(DOMAIN, {}).get("hub")
        return dict(info["config"]) if info else {}

    # ------------------------------------------------------------------
    # Time window and occupancy (carried over from v1)
    # ------------------------------------------------------------------

    def _within_time_window(self, now_local: datetime) -> bool:
        if not self._config.get(CONF_TIME_WINDOW_ENABLED, DEFAULT_TIME_WINDOW_ENABLED):
            return True
        cur = now_local.hour * 60 + now_local.minute
        s = str(self._config.get(CONF_TIME_WINDOW_START, DEFAULT_TIME_WINDOW_START)).split(":")
        e = str(self._config.get(CONF_TIME_WINDOW_END, DEFAULT_TIME_WINDOW_END)).split(":")
        start, end = int(s[0]) * 60 + int(s[1]), int(e[0]) * 60 + int(e[1])
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end

    def _occupancy(self, now_local: datetime) -> tuple[str, float]:
        """Return (status, offset applied to the target)."""
        if not self._occupancy_enabled:
            return "disabled", 0.0
        sensor = self._config.get(CONF_OCCUPANCY_SENSOR)
        st = self._state(sensor)
        if st is None or st.state in UNAVAILABLE:
            return "no_sensor", 0.0
        if st.state != "off":
            return "occupied", 0.0
        elapsed = (dt_util.utcnow() - st.last_changed).total_seconds() / 60.0
        if elapsed < float(self._config.get(CONF_UNOCCUPIED_DURATION, DEFAULT_UNOCCUPIED_DURATION)):
            return "occupied", 0.0
        cur = now_local.hour * 60 + now_local.minute
        weekend = now_local.weekday() >= 5 or (self._is_on(ENTITY_AT_HOME_MODE) is True)
        if MORNING_START <= cur < MORNING_END:
            keys = (
                (CONF_WEEKEND_MORNING_OFFSET, DEFAULT_WEEKEND_MORNING_OFFSET)
                if weekend
                else (CONF_WEEKDAY_MORNING_OFFSET, DEFAULT_WEEKDAY_MORNING_OFFSET)
            )
        elif MORNING_END <= cur < AFTERNOON_END:
            keys = (
                (CONF_WEEKEND_AFTERNOON_OFFSET, DEFAULT_WEEKEND_AFTERNOON_OFFSET)
                if weekend
                else (CONF_WEEKDAY_AFTERNOON_OFFSET, DEFAULT_WEEKDAY_AFTERNOON_OFFSET)
            )
        elif AFTERNOON_END <= cur < EVENING_END:
            keys = (
                (CONF_WEEKEND_EVENING_OFFSET, DEFAULT_WEEKEND_EVENING_OFFSET)
                if weekend
                else (CONF_WEEKDAY_EVENING_OFFSET, DEFAULT_WEEKDAY_EVENING_OFFSET)
            )
        else:
            return "unoccupied", 0.0
        return "unoccupied", float(self._config.get(keys[0], keys[1]))

    # ------------------------------------------------------------------
    # Inputs
    # ------------------------------------------------------------------

    def _schedule(self) -> ZoneState:
        """Scheduled target from the evohome cloud entity, else the ramses schedule attribute."""
        primary = self._config.get(CONF_PRIMARY_CLIMATE)
        backup = self._config.get(CONF_BACKUP_CLIMATE)
        current = self._temperature_attr(primary, "temperature")
        st = self._state(backup)
        sched: float | None = None
        nxt_at: datetime | None = None
        nxt_sp: float | None = None
        # An unavailable cloud entity retains its last attributes; stale switchpoints
        # must not outrank a live RF fallback.
        if st is not None and st.state in UNAVAILABLE:
            st = None
        if st is not None:
            status = st.attributes.get("status")
            sp = (status.get("setpoints") if isinstance(status, dict) else None) or {}
            # Each field parsed on its own: a bad next-switchpoint must not erase the current target.
            try:
                sched = float(sp["this_sp_temp"]) if sp.get("this_sp_temp") is not None else None
            except (TypeError, ValueError) as exc:
                _LOGGER.warning(
                    "OT %s: cannot parse this_sp_temp %r: %s",
                    self.room_name,
                    sp.get("this_sp_temp"),
                    exc,
                )
            try:
                nxt_sp = float(sp["next_sp_temp"]) if sp.get("next_sp_temp") is not None else None
            except (TypeError, ValueError) as exc:
                _LOGGER.warning(
                    "OT %s: cannot parse next_sp_temp %r: %s",
                    self.room_name,
                    sp.get("next_sp_temp"),
                    exc,
                )
            try:
                raw_at = sp.get("next_sp_from")
                nxt_at = dt_util.parse_datetime(str(raw_at)) if raw_at else None
                if nxt_at is not None:
                    nxt_at = dt_util.as_utc(nxt_at)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "OT %s: cannot parse next_sp_from %r: %s",
                    self.room_name,
                    sp.get("next_sp_from"),
                    exc,
                )
                nxt_at = None
            if sched is None and not sp:
                _LOGGER.debug(
                    "OT %s: %s has no status.setpoints (attributes: %s)",
                    self.room_name,
                    backup,
                    list(st.attributes),
                )
        if sched is None:
            sched = self._schedule_from_ramses(primary)
            if sched is not None:
                self._schedule_source = "ramses"
        else:
            self._schedule_source = "evohome"
            # Cloud lag: if the next switchpoint time has passed but evohome still reports the
            # previous period, the zone is already on the new value. Use it.
            if nxt_at is not None and nxt_sp is not None and dt_util.utcnow() >= nxt_at:
                sched = nxt_sp
                self._schedule_source = "evohome (next switchpoint, cloud lagging)"
        # Track schedule value changes so the policy can excuse a zone briefly lagging a switchpoint.
        last = self._store.get("last_schedule_setpoint")
        if sched is not None:
            if last is not None and abs(float(last) - sched) > 1e-6:
                self._store.set("prev_schedule_setpoint", float(last))
                self._store.set("schedule_changed_at", dt_util.utcnow().isoformat())
            if last is None or abs(float(last) - sched) > 1e-6:
                self._store.set("last_schedule_setpoint", sched)
        prev_sp = self._store.get("prev_schedule_setpoint")
        changed_raw = self._store.get("schedule_changed_at")
        changed_at = dt_util.parse_datetime(str(changed_raw)) if changed_raw else None
        return ZoneState(
            current_setpoint=current,
            schedule_setpoint=sched,
            next_switchpoint_at=nxt_at,
            next_switchpoint_setpoint=nxt_sp,
            previous_schedule_setpoint=float(prev_sp) if prev_sp is not None else None,
            schedule_changed_at=dt_util.as_utc(changed_at) if changed_at is not None else None,
        )

    def _ramses_schedule(self, entity_id: str | None) -> list | None:
        """Live ramses schedule if present (and cache it), else the cached copy from the store."""
        st = self._state(entity_id)
        schedule = st.attributes.get("schedule") if st and st.state not in UNAVAILABLE else None
        if isinstance(schedule, list) and schedule:
            if self._store.get("ramses_schedule") != schedule:
                self._store.set("ramses_schedule", schedule)
                self._store.set("ramses_schedule_saved_at", dt_util.utcnow().isoformat())
            return schedule
        cached = self._store.get("ramses_schedule")
        saved = dt_util.parse_datetime(str(self._store.get("ramses_schedule_saved_at", "")))
        fresh = saved is not None and timedelta(0) <= dt_util.utcnow() - dt_util.as_utc(
            saved
        ) <= timedelta(hours=48)
        return cached if fresh and isinstance(cached, list) and cached else None

    async def _maybe_fetch_ramses_schedule(self, entity_id: str | None) -> None:
        """Refresh the fallback without blocking control or flooding a failing radio."""
        hub = self._hub()
        if entity_id and hub is not None:
            self._schedule_fetcher.request(
                entity_id,
                hub.schedule_fetch_lock,
                lambda: self._ramses_schedule(entity_id) is not None,
            )

    def _schedule_from_ramses(self, entity_id: str | None) -> float | None:
        schedule = self._ramses_schedule(entity_id)
        if not schedule:
            return None
        now = dt_util.now()
        dow, hhmm = now.weekday(), now.strftime("%H:%M")
        for day in schedule:
            if day.get("day_of_week") == dow:
                active = None
                for sp in day.get("switchpoints", []):
                    if (
                        str(sp.get("time_of_day", "")) <= hhmm
                        and sp.get("heat_setpoint") is not None
                    ):
                        active = float(sp["heat_setpoint"])
                if active is not None:
                    return active
        for day in schedule:
            if day.get("day_of_week") == (dow - 1) % 7 and day.get("switchpoints"):
                last = day["switchpoints"][-1]
                return (
                    float(last.get("heat_setpoint"))
                    if last.get("heat_setpoint") is not None
                    else None
                )
        return None

    def _air_temperature(
        self, geometry: RoomGeometry | None, fallbacks: list[str]
    ) -> tuple[float | None, str]:
        preferred = self._config.get("air_temp_sensor") or (
            geometry.preferred_air_temperature_entity if geometry else None
        )
        val = self._temperature_state(preferred)
        if val is not None:
            return val, preferred or ""
        if preferred:
            fallbacks.append(f"preferred air sensor {preferred} unavailable")
        primary = self._config.get(CONF_PRIMARY_CLIMATE)
        val = self._temperature_attr(primary, "current_temperature")
        if val is not None:
            return val, f"{primary}.current_temperature"
        backup = self._config.get(CONF_BACKUP_CLIMATE)
        val = self._temperature_attr(backup, "current_temperature")
        if val is not None:
            fallbacks.append("air temperature from backup climate entity")
            return val, f"{backup}.current_temperature"
        return None, ""

    def _environment(
        self, geometry: RoomGeometry | None, fallbacks: list[str]
    ) -> tuple[Environment | None, dict[str, Any]]:
        hub = self._hub_config()
        weather = hub.get(CONF_WEATHER_ENTITY) or self._config.get(CONF_WEATHER_ENTITY)
        # Outdoor temperature
        t_out = self._temperature_state(hub.get(CONF_OUTDOOR_TEMP_SENSOR))
        src = str(hub.get(CONF_OUTDOOR_TEMP_SENSOR) or "")
        if t_out is None:
            t_out = self._temperature_attr(weather, "temperature")
            src = f"{weather}.temperature"
            if t_out is not None and hub.get(CONF_OUTDOOR_TEMP_SENSOR):
                fallbacks.append("outdoor temperature from weather entity")
        hub_data = self._hub()
        if t_out is not None and hub_data is not None and hub_data.store is not None:
            hub_data.store.set("outdoor_cache", t_out)
            hub_data.store.set("outdoor_cache_at", dt_util.utcnow().isoformat())
        if t_out is None and hub_data is not None and hub_data.store is not None:
            cached, at = hub_data.store.get("outdoor_cache"), hub_data.store.get("outdoor_cache_at")
            parsed = dt_util.parse_datetime(str(at)) if at else None
            if cached is not None and parsed is not None:
                age = dt_util.utcnow() - dt_util.as_utc(parsed)
                if age < timedelta(hours=OUTDOOR_CACHE_MAX_AGE_H):
                    t_out = float(cached)
                    src = f"cache ({int(age.total_seconds() // 60)} min old)"
                    fallbacks.append(
                        f"outdoor temperature from cache, {int(age.total_seconds() // 60)} min old"
                    )
        if t_out is None:
            return None, {"outdoor_source": "none"}
        # Wind: use the weather entity's declared unit
        wind = self._float_attr(weather, "wind_speed")
        unit = (
            self._state(weather).attributes.get("wind_speed_unit") if self._state(weather) else None
        ) or "km/h"
        if wind is None:
            wind_ms = 0.0
            fallbacks.append("wind unavailable, 0 m/s")
        elif unit in ("km/h", "km/hr"):
            wind_ms = wind / 3.6
        elif unit == "mph":
            wind_ms = wind * 0.44704
        elif unit == "kn":
            wind_ms = wind * 0.514444
        else:
            wind_ms = wind
        # Irradiance / cloud
        ghi = self._float_state(hub.get(CONF_IRRADIANCE_SENSOR))
        irradiance_state = self._state(hub.get(CONF_IRRADIANCE_SENSOR))
        irradiance_unit = (
            irradiance_state.attributes.get("unit_of_measurement", "W/m²")
            if irradiance_state
            else "W/m²"
        )
        if ghi is not None:
            if irradiance_unit in ("kW/m²", "kW/m2"):
                ghi *= 1000
            elif irradiance_unit not in ("W/m²", "W/m2"):
                fallbacks.append(
                    f"unsupported irradiance unit {irradiance_unit}; solar correction withheld"
                )
                ghi = None
        cloud = self._float_attr(weather, "cloud_coverage")
        cloud_fraction = None if cloud is None else max(0.0, min(1.0, cloud / 100.0))
        if ghi is None and hub.get(CONF_IRRADIANCE_SENSOR):
            fallbacks.append("irradiance sensor unavailable; solar correction withheld")
        if ghi is None and cloud_fraction is None:
            fallbacks.append("cloud cover unavailable, assuming 50%")
        # Sun
        elev = self._float_attr("sun.sun", "elevation")
        az = self._float_attr("sun.sun", "azimuth")
        if ghi is not None and (elev is None or az is None):
            fallbacks.append("sun position unavailable; solar correction withheld")
            ghi = None
        # Adjacent rooms: other coordinators' air temperatures
        adjacent: dict[str, float] = {}
        rooms = self.hass.data.get(DOMAIN, {}).get("rooms", {})
        if geometry:
            for s in geometry.surfaces:
                neighbours = s.adjacent_fractions or ({s.adjacent: 1.0} if s.adjacent else {})
                for room_id in neighbours:
                    neighbour = rooms.get(room_id)
                    # Read the sensor now: coordinator snapshots can retain stale data
                    # after an update failure and depend on room polling order.
                    if neighbour is not None:
                        value, _ = neighbour._air_temperature(neighbour.geometry, [])
                        if value is not None:
                            adjacent[room_id] = value
        env = Environment(
            t_out=t_out,
            wind_ms=wind_ms,
            ghi_wm2=ghi,
            cloud_fraction=cloud_fraction,
            sun_elevation_deg=elev if elev is not None else -10.0,
            sun_azimuth_deg=az if az is not None else 180.0,
            day_of_year=dt_util.now().timetuple().tm_yday,
            t_ground=float(hub.get(CONF_GROUND_TEMP, DEFAULT_GROUND_TEMP)),
            adjacent_temps=adjacent,
        )
        return env, {"outdoor_source": src, "wind_ms": wind_ms, "ghi": ghi, "cloud": cloud_fraction}

    def _flow_temperature(self) -> float | None:
        hub_data, hub = self._hub(), self._hub_config()
        value = self._temperature_state(hub.get(CONF_FLOW_TEMP_ENTITY))
        dhw = self._is_on(hub.get(CONF_DHW_ACTIVE_ENTITY))
        manual = float(hub.get(CONF_MANUAL_FLOW_TEMP, DEFAULT_MANUAL_FLOW_TEMP))
        if hub_data is None:
            # DHW state unknown counts as possibly-active: the reading may be DHW-elevated.
            return value if (value is not None and dhw is False) else manual
        return hub_data.sample_flow_temp(value, dhw, manual)

    def _model_params(self, geometry: RoomGeometry | None) -> ModelParams:
        asym_on = bool(
            self._config.get(
                CONF_ASYMMETRY_ENABLED, geometry.asymmetry_enabled if geometry else False
            )
        )
        return ModelParams(
            trust_k=self._tunables[CONF_TRUST_K],
            cap_up=self._tunables[CONF_CAP_UP],
            cap_down=self._tunables[CONF_CAP_DOWN],
            step=THERMOSTAT_STEP,
            asymmetry_a=0.5 if asym_on else 0.0,
        )

    def _zone_setpoint_bounds(self) -> tuple[float, float]:
        """Validate explicit policy bounds; legacy min/max keys remain ignored."""
        try:
            low = float(self._config.get(CONF_ZONE_SETPOINT_MIN, DEFAULT_ZONE_SETPOINT_MIN))
            high = float(self._config.get(CONF_ZONE_SETPOINT_MAX, DEFAULT_ZONE_SETPOINT_MAX))
        except (TypeError, ValueError) as exc:
            raise ValueError("invalid zone setpoint bounds") from exc
        if not (
            math.isfinite(low)
            and math.isfinite(high)
            and ZONE_SETPOINT_MIN <= low < high <= ZONE_SETPOINT_MAX
        ):
            raise ValueError(
                f"zone setpoint bounds must satisfy {ZONE_SETPOINT_MIN} <= min < max <= "
                f"{ZONE_SETPOINT_MAX}"
            )
        return low, high

    def _policy_params(self) -> PolicyParams:
        zone_min, zone_max = self._zone_setpoint_bounds()
        return PolicyParams(
            step=THERMOSTAT_STEP,
            override_minutes=int(
                self._config.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
            ),
            manual_hold_minutes=int(self._config.get(CONF_MANUAL_HOLD, DEFAULT_MANUAL_HOLD)),
            window_open_delay_minutes=int(
                self._config.get(CONF_WINDOW_OPEN_DELAY, DEFAULT_WINDOW_OPEN_DELAY)
            ),
            window_close_delay_minutes=int(
                self._config.get(CONF_WINDOW_DELAY, DEFAULT_WINDOW_DELAY)
            ),
            preheat_release_minutes=int(
                self._config.get(CONF_PREHEAT_RELEASE, DEFAULT_PREHEAT_RELEASE)
            ),
            window_setpoint=float(self._config.get(CONF_WINDOW_SETPOINT, DEFAULT_WINDOW_SETPOINT)),
            zone_setpoint_min=zone_min,
            zone_setpoint_max=zone_max,
        )

    def _memory(self) -> OverrideMemory:
        def dt(key: str) -> datetime | None:
            raw = self._store.get(key)
            if not raw:
                return None
            parsed = dt_util.parse_datetime(str(raw))
            return dt_util.as_utc(parsed) if parsed else None

        sp = self._store.get("last_written_setpoint")
        msp = self._store.get("manual_setpoint")
        return OverrideMemory(
            last_written_setpoint=float(sp) if sp is not None else None,
            last_written_at=dt("last_written_at"),
            manual_detected_at=dt("manual_detected_at"),
            manual_release_at=dt("manual_release_at"),
            manual_setpoint=float(msp) if msp is not None else None,
            window_open_since=dt("window_open_since"),
            window_closed_at=dt("window_closed_at"),
        )

    def _save_memory(self, m: OverrideMemory) -> None:
        self._store.set("last_written_setpoint", m.last_written_setpoint)
        self._store.set(
            "last_written_at", m.last_written_at.isoformat() if m.last_written_at else None
        )
        self._store.set(
            "manual_detected_at", m.manual_detected_at.isoformat() if m.manual_detected_at else None
        )
        self._store.set(
            "manual_release_at", m.manual_release_at.isoformat() if m.manual_release_at else None
        )
        self._store.set("manual_setpoint", m.manual_setpoint)
        self._store.set(
            "window_open_since", m.window_open_since.isoformat() if m.window_open_since else None
        )
        self._store.set(
            "window_closed_at", m.window_closed_at.isoformat() if m.window_closed_at else None
        )

    def _any_on(self, entity_ids: list[str]) -> bool:
        return any(self._is_on(e) is True for e in entity_ids)

    def _schedule_retry(self, delay_s: float = 60.0) -> None:
        """Inputs were missing (typically right after a restart): try again soon, once."""
        if self._retry_cancel is not None:
            return

        @callback
        def _fire(_now) -> None:  # @callback: run in the event loop, not the executor
            self._retry_cancel = None
            self.hass.async_create_task(self.async_request_refresh())

        from homeassistant.helpers.event import async_call_later

        self._retry_cancel = async_call_later(self.hass, delay_s, _fire)

    async def async_shutdown(self) -> None:
        if self._retry_cancel is not None:
            self._retry_cancel()
            self._retry_cancel = None
        await self._schedule_fetcher.stop()
        await super().async_shutdown()

    # ------------------------------------------------------------------
    # Action
    # ------------------------------------------------------------------

    def _command_target(self, decision: Decision, zone: ZoneState) -> float | None:
        """The thermostat setpoint expected after a command.

        A follow-schedule command can only be compared when the current schedule
        is available.  It is still sent when the policy asks for it; the missing
        comparison target is reported as a readback error, never a confirmation.
        """
        if decision.action is Action.WRITE:
            return float(decision.setpoint) if decision.setpoint is not None else None
        if decision.action is Action.RELEASE:
            return zone.schedule_setpoint
        return None

    @staticmethod
    def _canonical_until(value: Any) -> datetime | str | None:
        """Make an exposed RAMSES expiry comparable without guessing its timezone."""
        parsed = value if isinstance(value, datetime) else None
        if parsed is None and isinstance(value, str):
            parsed = dt_util.parse_datetime(value)
        if parsed is not None and parsed.tzinfo is not None:
            return dt_util.as_utc(parsed)
        if isinstance(value, datetime):
            return value.isoformat()
        return value if isinstance(value, str) else None

    @classmethod
    def _primary_status(cls, state) -> tuple[str | None, datetime | str | None]:
        """Return explicit RAMSES command mode and expiry, never generic climate state."""
        if state is None:
            return None, None
        attrs = state.attributes
        # RAMSES zones expose `mode: {mode, setpoint, until}`.  Some source
        # versions expose the equivalent field under `status`; both are
        # device-reported command state, unlike HA's generic climate state.
        for container in (attrs.get("mode"), attrs.get("status")):
            mode = container.get("mode") if isinstance(container, dict) else None
            if isinstance(mode, str):
                return mode, cls._canonical_until(container.get("until"))
        params = attrs.get("params")
        nested = params.get("mode") if isinstance(params, dict) else None
        mode = nested.get("mode") if isinstance(nested, dict) else None
        if isinstance(mode, str):
            return mode, cls._canonical_until(nested.get("until"))
        return (nested, None) if isinstance(nested, str) else (None, None)

    @classmethod
    def _primary_status_mode(cls, state) -> str | None:
        """Return the explicit RAMSES command mode for compatibility callers."""
        return cls._primary_status(state)[0]

    def _primary_setpoint_observation(
        self,
    ) -> tuple[float | None, datetime | None, str, str | None, datetime | str | None]:
        """Read the current primary setpoint and its source-specific mode evidence."""
        primary = self._config.get(CONF_PRIMARY_CLIMATE)
        st = self._state(primary)
        if st is None or st.state in UNAVAILABLE:
            return None, None, "readback_unavailable", None, None
        reported = getattr(st, "last_reported", None)
        if not isinstance(reported, datetime):
            return None, None, "readback_error", None, None
        try:
            reported = dt_util.as_utc(reported)
            age = dt_util.utcnow() - reported
        except TypeError, ValueError:
            return None, None, "readback_error", None, None
        mode, until = self._primary_status(st)
        if age < timedelta(0) or age > READBACK_MAX_AGE:
            return None, reported, "readback_stale", mode, until
        value = self._temperature_attr(primary, "temperature")
        if value is None:
            return None, reported, "readback_error", mode, until
        return value, reported, "readback_observed", mode, until

    def _read_primary_setpoint(
        self, sent_at: datetime
    ) -> tuple[float | None, datetime | None, str, str | None, datetime | str | None]:
        """Read a fresh primary-thermostat setpoint for command confirmation."""
        value, reported, status, mode, until = self._primary_setpoint_observation()
        if status == "readback_observed" and reported <= sent_at:
            return None, reported, "readback_no_echo", mode, until
        return value, reported, status, mode, until

    def _has_command_evidence(self, value: float, mode: str | None) -> bool:
        """Require a compatible transition after sending, not just a heartbeat."""
        if self._pending_action is Action.RELEASE:
            return (
                self._pre_send_mode is not None
                and mode == "follow_schedule"
                and mode != self._pre_send_mode
            )
        if self._pending_action is Action.WRITE:
            changed_target = (
                self._pre_send_setpoint is not None
                and abs(value - self._pre_send_setpoint) > THERMOSTAT_STEP / 2
            )
            changed_mode = (
                self._pre_send_mode is not None
                and mode == "temporary_override"
                and mode != self._pre_send_mode
            )
            return changed_target or changed_mode
        return False

    @staticmethod
    def _same_command_state(
        left: tuple[float, str | None, datetime | str | None] | None,
        right: tuple[float, str | None, datetime | str | None] | None,
    ) -> bool:
        return bool(
            left is not None
            and right is not None
            and abs(left[0] - right[0]) <= THERMOSTAT_STEP / 2
            and left[1:] == right[1:]
        )

    @staticmethod
    def _override_is_in_force(
        state: tuple[float, str | None, datetime | str | None] | None, now: datetime
    ) -> bool:
        return bool(
            state is not None
            and state[1] == "temporary_override"
            and isinstance(state[2], datetime)
            and state[2] > now
        )

    def _is_command_reversion(
        self,
        value: float,
        mode: str | None,
        until: datetime | str | None,
        now: datetime,
    ) -> bool:
        """Recognise only an exact return to a still-valid command HHO had confirmed."""
        current = (value, mode, until)
        pre_send = (
            (self._pre_send_setpoint, self._pre_send_mode, self._pre_send_until)
            if self._pre_send_setpoint is not None
            else None
        )
        return (
            self._pre_send_owned
            and self._service_echo_state is not None
            and self._override_is_in_force(pre_send, now)
            and not self._same_command_state(self._service_echo_state, pre_send)
            and self._same_command_state(current, pre_send)
        )

    # ------------------------------------------------------------------
    # Journal hooks: optional, defensive, never affect control
    # ------------------------------------------------------------------

    def _journal(self, kind: str, data: dict[str, Any], origin: str = "controller") -> None:
        journal = self.journal
        if journal is None:
            return
        try:
            room_id = self.journal_room_id or self.room_id
            journal.record(
                kind,
                room_id=room_id,
                scope=room_id,
                origin=origin,
                data=data,
                provenance={
                    "model_version": COMFORT_MODEL_VERSION,
                    "schedule_source": self._schedule_source,
                },
            )
        except Exception:  # noqa: BLE001
            _LOGGER.debug("OT %s: journal hook failed", self.room_name, exc_info=True)

    def _journal_changed(self, key: str, value: Any) -> bool:
        """True the first time and whenever the tracked value differs from last record."""
        marker = object()
        if self._journal_last.get(key, marker) == value:
            return False
        self._journal_last[key] = value
        return True

    def _journal_readback(self) -> None:
        if self._sent_at is None:
            return  # nothing has been commanded yet; there is no readback to journal
        state = (
            self._readback_status,
            self._readback_timed_out,
            self._pending_target,
            self._confirmed_target,
        )
        if self._journal_changed("readback", state):
            self._journal(
                "readback",
                {
                    "status": self._readback_status,
                    "timed_out": self._readback_timed_out,
                    "pending_target": self._pending_target,
                    "confirmed_target": self._confirmed_target,
                    "sent_target": self._sent_target,
                    "readback_at": self._readback_at,
                    "outcome": self._readback_status,
                },
                origin="source",
            )

    def _refresh_readback(self, now: datetime) -> None:
        """Advance only a command already accepted by the HA service layer."""
        self._advance_readback(now)
        self._journal_readback()

    def _advance_readback(self, now: datetime) -> None:
        if self._sent_at is None:
            return
        boundary = self._service_completed_at or self._sent_at
        value, reported, status, mode, until = self._read_primary_setpoint(boundary)
        self._readback_at = reported
        if status == "readback_observed":
            if self._is_command_reversion(value, mode, until, now):
                self._command_reverted = True
                self._confirmed_target = None
                self._confirmed_at = None
                self._pending_target = self._sent_target
                self._pending_since = self._sent_at
                pre_send = (
                    self._pre_send_setpoint,
                    self._pre_send_mode,
                    self._pre_send_until,
                )
                self._owned_command_state = pre_send
                self._readback_timed_out = now - self._sent_at >= READBACK_TIMEOUT
                self._readback_status = "readback_reverted"
                self.write_status = "readback_reverted"
                return
            self._command_reverted = False
        if self._pending_target is None:
            # Confirmation remains a fact unless the exact owned pre-send state
            # returns after a different service-local projection (handled above).
            if self._confirmed_target is not None:
                self._readback_status = "confirmed"
                self.write_status = "confirmed"
                return
            self._readback_status = "readback_error"
            return
        self._readback_timed_out = bool(
            self._pending_since is not None and now - self._pending_since >= READBACK_TIMEOUT
        )
        if status == "readback_observed":
            if abs(value - self._pending_target) <= THERMOSTAT_STEP / 2:
                if self._has_command_evidence(value, mode):
                    self._confirmed_target = self._pending_target
                    self._confirmed_at = now
                    self._owned_command_state = (value, mode, until)
                    self._pending_target = None
                    self._pending_since = None
                    self._command_reverted = False
                    self._reversion_retry_used = False
                    self._readback_timed_out = False
                    self._readback_status = "confirmed"
                    self.write_status = "confirmed"
                    return
                status = "matching_readback_unverified"
            else:
                status = "readback_different"
        self._readback_status = status
        self.write_status = status

    def _command_data(self, d: OTCoordinatorData) -> None:
        """Copy runtime-only command provenance into the published snapshot."""
        d.requested_target = self._requested_target
        d.sent_target = self._sent_target
        d.pending_target = self._pending_target
        d.confirmed_target = self._confirmed_target
        d.requested_at = self._requested_at
        d.sent_at = self._sent_at
        d.pending_since = self._pending_since
        d.confirmed_at = self._confirmed_at
        d.readback_at = self._readback_at
        d.readback_status = self._readback_status
        d.readback_timed_out = self._readback_timed_out
        d.readback_hint = self.readback_hint(self._readback_status, self._readback_timed_out)
        d.write_status = self.write_status

    @staticmethod
    def readback_hint(status: str, timed_out: bool) -> str:
        """Explain an unconfirmed readback without asserting delivery or actuation."""
        if status == "confirmed":
            return ""
        hint = READBACK_HINTS.get(status, "")
        if timed_out and status != "readback_reverted":
            timeout_hint = READBACK_HINTS["readback_timed_out"]
            hint = f"{hint} {timeout_hint}" if hint else timeout_hint
        return hint

    async def _perform(self, decision: Decision) -> bool:
        """Carry out the decision's action. Returns False when the service call did not
        complete, so the caller must not record the action as done."""
        if decision.action is Action.NONE:
            return True
        command = {
            "action": decision.action.value,
            "setpoint": decision.setpoint,
            "reason": decision.reason,
            "service": "ramses_cc.set_zone_mode",
        }
        self._journal("command_requested", {**command, "outcome": "requested"})
        if not self._write_guard():
            self.write_status = "blocked"
            self._journal("command_result", {**command, "outcome": "blocked"})
            return False
        primary = self._config.get(CONF_PRIMARY_CLIMATE)
        if not primary:
            _LOGGER.warning(
                "OT %s: no primary climate entity; cannot %s", self.room_name, decision.action.value
            )
            self._journal("command_result", {**command, "outcome": "no_actuator"})
            return False
        if decision.action is Action.WRITE:
            # Bounds are applied in the policy's _write so memory matches the wire; this
            # is a last-resort refusal for anything non-finite or out of range anyway.
            setpoint = float(decision.setpoint)
            zone_min, zone_max = self._zone_setpoint_bounds()
            if not math.isfinite(setpoint) or not (zone_min <= setpoint <= zone_max):
                _LOGGER.error(
                    "OT %s: refusing to write invalid setpoint %s",
                    self.room_name,
                    decision.setpoint,
                )
                self._journal(
                    "command_result",
                    {**command, "outcome": "refused_out_of_bounds", "bounds": [zone_min, zone_max]},
                )
                return False
            data = {
                "entity_id": primary,
                "mode": "temporary_override",
                "setpoint": setpoint,
                "duration": {
                    "minutes": int(
                        self._config.get(CONF_OVERRIDE_DURATION, DEFAULT_OVERRIDE_DURATION)
                    )
                },
            }
        else:
            data = {"entity_id": primary, "mode": "follow_schedule"}
        try:
            self.write_status = "attempted"
            async with asyncio.timeout(ACTUATOR_CALL_TIMEOUT):
                await self.hass.services.async_call(
                    "ramses_cc", "set_zone_mode", data, blocking=True
                )
            self.write_status = "service_succeeded"
        except TimeoutError:
            self.write_status = "service_timeout"
            _LOGGER.warning(
                "OT %s: ramses_cc.set_zone_mode did not return within %.0f s; treating as failed",
                self.room_name,
                ACTUATOR_CALL_TIMEOUT,
            )
            self._journal("command_result", {**command, "outcome": "service_timeout"})
            return False
        except Exception as exc:  # noqa: BLE001
            self.write_status = "failed"
            _LOGGER.warning("OT %s: ramses_cc.set_zone_mode failed: %s", self.room_name, exc)
            self._journal(
                "command_result",
                {**command, "outcome": "service_failed", "error": type(exc).__name__},
            )
            return False
        self._journal("command_sent", {**command, "outcome": "service_succeeded"})
        self._journal("command_result", {**command, "outcome": "service_succeeded"})
        _LOGGER.info(
            "OT %s: %s %s (%s)",
            self.room_name,
            decision.action.value,
            decision.setpoint,
            decision.reason,
        )
        return True

    # ------------------------------------------------------------------
    # Main cycle
    # ------------------------------------------------------------------

    def mark_restore_complete(self) -> None:
        """Called by setup after entity platforms (and their restored states) are loaded."""
        if not self._restore_complete and self._startup_reconciliation_pending:
            self._startup_reconcile_after = dt_util.utcnow() + STARTUP_SETTLE_TIME
        self._restore_complete = True

    def _startup_ready(self, now: datetime, zone: ZoneState) -> bool:
        """Wait for settled source data before reconciling persisted policy ownership."""
        if not self._startup_reconciliation_pending:
            return True
        deadline = self._startup_reconcile_after
        if deadline is None or now < deadline or zone.schedule_setpoint is None:
            return False
        _, reported, status, _, _ = self._primary_setpoint_observation()
        # A recently restored HA state can contain a partial/cached RAMSES target.
        # Require a primary report after the settling interval, not just fresh age.
        if status != "readback_observed" or reported is None or reported < deadline:
            return False
        self._startup_reconciliation_pending = False
        return True

    async def _async_update_data(self) -> OTCoordinatorData:
        try:
            # The lock is released by ``async with`` whichever way the cycle ends,
            # including the cancellation that asyncio.timeout uses to enforce the bound.
            async with self._cycle_lock:
                async with asyncio.timeout(CYCLE_TIMEOUT):
                    return await self._cycle()
        except TimeoutError:
            _LOGGER.error(
                "OT %s: control cycle exceeded %.0f s and was abandoned",
                self.room_name,
                CYCLE_TIMEOUT,
            )
            self._journal("decision", {"outcome": "cycle_timeout", "timeout_s": CYCLE_TIMEOUT})
            if self.data is not None:
                note = "cycle timed out; showing last good data"
                if note not in self.data.fallbacks:
                    self.data.fallbacks = [*self.data.fallbacks, note]
                return self.data
            raise UpdateFailed("control cycle timed out") from None
        except Exception as exc:  # noqa: BLE001
            _LOGGER.exception("OT %s: update failed", self.room_name)
            if self.data is not None:
                note = f"update failed ({type(exc).__name__}); showing last good data"
                if note not in self.data.fallbacks:
                    self.data.fallbacks = [*self.data.fallbacks, note]
                return self.data
            raise UpdateFailed(str(exc)) from exc

    async def _cycle(self) -> OTCoordinatorData:
        now = dt_util.utcnow()
        now_local = dt_util.as_local(now)
        fallbacks: list[str] = []
        geometry = self._geometry
        hub_data = self._hub()
        d = OTCoordinatorData(mode=self._mode, enabled=self._enabled, last_run=now)
        # A prior service completion remains pending until a *newer* primary
        # thermostat report is seen.  This does not affect policy inputs or
        # persisted write memory.
        self._refresh_readback(now)
        if geometry is not None:
            d.geometry_warnings = list(geometry.warnings)
            d.glazed_area_m2 = round(geometry.glazed_area_m2, 2)
            d.total_area_m2 = round(geometry.total_area_m2, 1)
            d.installed_output_dt50_w = sum(e.output_dt50_w for e in geometry.emitters) or None
        elif self._geometry_error:
            fallbacks.append(f"geometry: {self._geometry_error}")

        # --- target -------------------------------------------------------
        await self._maybe_fetch_ramses_schedule(self._config.get(CONF_PRIMARY_CLIMATE))
        zone = self._schedule()
        d.schedule_source = self._schedule_source
        if self._journal_changed("schedule", (zone.schedule_setpoint, self._schedule_source)):
            self._journal(
                "schedule_change",
                {
                    "schedule_setpoint": zone.schedule_setpoint,
                    "previous_schedule_setpoint": zone.previous_schedule_setpoint,
                    "schedule_source": self._schedule_source,
                    "next_switchpoint_at": zone.next_switchpoint_at,
                    "next_switchpoint_setpoint": zone.next_switchpoint_setpoint,
                    "outcome": "observed",
                },
                origin="source",
            )
        fetch = self._schedule_fetcher.snapshot()
        d.schedule_fetch_status = fetch["status"]
        d.schedule_fetch_failure_class = fetch["failure_class"]
        d.schedule_fetch_attempts = fetch["attempts"]
        d.schedule_next_retry_at = fetch["next_retry_at"]
        d.schedule_setpoint, d.zone_setpoint = zone.schedule_setpoint, zone.current_setpoint
        d.next_switchpoint_at, d.next_switchpoint_setpoint = (
            zone.next_switchpoint_at,
            zone.next_switchpoint_setpoint,
        )
        if zone.schedule_setpoint is None:
            fallbacks.append("schedule setpoint unavailable")
            self._schedule_retry()

        d.occupancy_status, d.occupancy_offset = self._occupancy(now_local)
        d.time_window_active = self._within_time_window(now_local)

        # --- environment and model -------------------------------------
        env, env_info = self._environment(geometry, fallbacks)
        d.outdoor_source = env_info.get("outdoor_source", "")
        if env is not None:
            d.outdoor_temp, d.wind_ms, d.ghi_wm2, d.cloud_fraction = (
                env.t_out,
                env.wind_ms,
                env.ghi_wm2,
                env.cloud_fraction,
            )
            if hub_data is not None:
                d.running_mean_outdoor = hub_data.sample_outdoor(env.t_out, now_local)
        d.air_temp, d.air_temp_source = self._air_temperature(geometry, fallbacks)

        hub_cfg = self._hub_config()
        if zone.schedule_setpoint is not None:
            target = zone.schedule_setpoint + d.occupancy_offset
            # Existing entries may still store adaptive_enabled=True. Retire the
            # subtraction in the control path, not merely in defaults for new rooms.
            if hub_cfg.get(CONF_ADAPTIVE_ENABLED):
                fallbacks.append(
                    "legacy adaptive setback ignored; scheduled comfort target preserved"
                )
            d.target_ot = round(target, 2)

        correction: Correction | None = None
        if (
            geometry is not None
            and env is not None
            and d.target_ot is not None
            and geometry.surfaces
        ):
            try:
                correction = required_air_temperature(
                    geometry.surfaces, env, d.target_ot, self._model_params(geometry)
                )
            except ValueError as exc:
                fallbacks.append(f"model: {exc}")
        if correction is not None:
            d.mrt_steady_state = round(correction.mrt_at_setpoint, 2)
            d.offset_physical = round(correction.offset_physical, 3)
            d.offset_trusted = round(correction.offset_trusted, 3)
            d.offset_asymmetry = round(correction.offset_asymmetry, 3)
            d.offset_final = round(correction.offset_final, 3)
            d.air_setpoint = correction.air_setpoint
            if d.air_setpoint is not None and not math.isfinite(d.air_setpoint):
                fallbacks.append("model produced a non-finite setpoint; discarded")
                d.air_setpoint = None
            d.capped = correction.capped
            d.solar_k = round(correction.solar_k, 3)
            d.sum_l = round(correction.sum_l, 4)
            if d.air_temp is not None:
                # Current-condition estimate: MRT must be evaluated at the measured air
                # temperature, not at the hypothetical setpoint air (both remain
                # steady-state approximations, not measurements).
                try:
                    mrt_now = steady_state_mrt(
                        geometry.surfaces, env, d.air_temp, self._model_params(geometry)
                    )
                    d.operative_temp = round(operative_temperature(d.air_temp, mrt_now.mrt), 2)
                except ValueError:
                    pass

        d.flow_temp_used = self._flow_temperature()
        if geometry is not None and d.flow_temp_used is not None and d.air_temp is not None:
            d.radiator_output_w = round(
                radiator_output_w(geometry.emitters, d.flow_temp_used, d.air_temp)
            )

        # --- policy -----------------------------------------------------------
        window_ids = geometry.window_contacts if geometry else []
        door_ids = geometry.adjacent_door_contacts if geometry else []
        inputs = PolicyInputs(
            now=now,
            room_enabled=self._enabled,
            hub_enabled=hub_data.global_enabled if hub_data else True,
            holiday_mode=self._is_on(ENTITY_HOLIDAY_MODE) is True,
            within_time_window=d.time_window_active,
            shadow_mode=self._mode != MODE_ACTIVE,
            computed_setpoint=d.air_setpoint,
            zone=zone,
            memory=self._memory(),
            any_window_open=self._any_on(window_ids),
            any_adjacent_door_open=self._any_on(door_ids),
            command_reverted=self._command_reverted,
            retry_reverted_command=(self._command_reverted and not self._reversion_retry_used),
            params=self._policy_params(),
        )
        restored = self._restore_complete and hub_data is not None and hub_data.restore_complete
        if not restored:
            decision = Decision(
                State.NO_DATA,
                Action.NONE,
                None,
                "deferred: awaiting hub/restored state",
                inputs.memory,
            )
        elif not self._startup_ready(now, zone):
            decision = Decision(
                State.NO_DATA,
                Action.NONE,
                None,
                "deferred: awaiting startup thermostat reconciliation",
                inputs.memory,
            )
            self._schedule_retry()
        else:
            decision = decide(inputs)
        d.state, d.reason, d.action = decision.state.value, decision.reason, decision.action.value
        d.would_write = (
            decision.would_write if decision.state is State.SHADOW else decision.setpoint
        )
        if self._journal_changed("decision", (d.state, d.action, d.reason, d.would_write)):
            self._journal(
                "decision",
                {
                    "state": d.state,
                    "action": d.action,
                    "reason": d.reason,
                    "target": d.would_write,
                    "mode": self._mode,
                    "bounds": list(self._zone_setpoint_bounds()),
                    "schedule_setpoint": zone.schedule_setpoint,
                    "air_setpoint": d.air_setpoint,
                    "target_ot": d.target_ot,
                    "air_temp": d.air_temp,
                    "outcome": "shadow"
                    if d.state == State.SHADOW.value
                    else "command_requested"
                    if d.action != Action.NONE.value
                    else "no_action",
                },
            )
        d.window_override_active = decision.state is State.WINDOW_OPEN or (
            decision.state is State.SHADOW and inputs.any_window_open
        )
        d.adjacent_door_open = inputs.any_adjacent_door_open
        d.fallbacks = fallbacks

        memory = decision.memory
        if decision.action is not Action.NONE:
            target = self._command_target(decision, zone)
            self._requested_target = target
            self._requested_at = now
            self.write_status = "requested"
            # Keep the actual send/start time for provenance, but require a
            # report newer than service completion.  RAMSES may project its
            # transmitted packet into HA state while this call is still running;
            # that local projection is transport evidence, not a readback.
            sent_at = dt_util.utcnow()
            pre_value, _, pre_status, pre_mode, pre_until = self._primary_setpoint_observation()
            pre_state = (
                (pre_value, pre_mode, pre_until)
                if pre_status == "readback_observed" and pre_value is not None
                else None
            )
            policy_memory = inputs.memory
            manual_standing = bool(
                policy_memory.manual_detected_at is not None
                and policy_memory.manual_release_at is not None
                and sent_at < policy_memory.manual_release_at
            )
            memory_owned = bool(
                policy_memory.last_written_setpoint is not None
                and policy_memory.last_written_at is not None
                and timedelta(0)
                <= sent_at - policy_memory.last_written_at
                < timedelta(minutes=inputs.params.override_minutes)
                and pre_state is not None
                and abs(pre_state[0] - policy_memory.last_written_setpoint) <= THERMOSTAT_STEP / 2
            )
            pre_owned = bool(
                not manual_standing
                and (memory_owned or inputs.retry_reverted_command)
                and self._same_command_state(pre_state, self._owned_command_state)
                and self._override_is_in_force(pre_state, sent_at)
            )
            if await self._perform(decision):
                service_completed_at = dt_util.utcnow()
                echo_value, _, echo_status, echo_mode, echo_until = (
                    self._primary_setpoint_observation()
                )
                service_echo = (
                    (echo_value, echo_mode, echo_until)
                    if echo_status == "readback_observed" and echo_value is not None
                    else None
                )
                # A successful service call is only a send.  Discard any older
                # acknowledgement, including one for an identical retry, until
                # this command has a thermostat report newer than completion.
                self._sent_target = target
                self._sent_at = sent_at
                self._service_completed_at = service_completed_at
                self._pending_target = target
                self._pending_since = sent_at
                self._pending_action = decision.action
                self._pre_send_setpoint = pre_value if pre_status == "readback_observed" else None
                self._pre_send_mode = pre_mode if pre_status == "readback_observed" else None
                self._pre_send_until = pre_until if pre_status == "readback_observed" else None
                self._pre_send_owned = pre_owned
                self._service_echo_state = service_echo
                self._command_reverted = False
                if inputs.retry_reverted_command and decision.action is Action.WRITE:
                    self._reversion_retry_used = True
                self._confirmed_target = None
                self._confirmed_at = None
                self._readback_at = None
                self._readback_status = "pending_readback"
                self._readback_timed_out = False
                self.write_status = "pending_readback"
                self._refresh_readback(dt_util.utcnow())
            else:
                # The service never completed, so do not create a pending device
                # acknowledgement.  Preserve any older pending command for a
                # later observation, while this attempted command is explicit.
                self._readback_status = (
                    self.write_status
                    if self.write_status in ("blocked", "service_timeout")
                    else "service_failed"
                )
                self.write_status = self._readback_status
                memory = replace(
                    inputs.memory,
                    window_open_since=memory.window_open_since,
                    window_closed_at=memory.window_closed_at,
                )
                d.reason = decision.reason + " (service call failed; will retry)"
        manual = (memory.manual_detected_at, memory.manual_release_at, memory.manual_setpoint)
        if self._journal_changed("manual", manual):
            self._journal(
                "manual_override",
                {
                    "status": "set" if memory.manual_detected_at is not None else "cleared",
                    "detected_at": memory.manual_detected_at,
                    "release_at": memory.manual_release_at,
                    "held_setpoint": memory.manual_setpoint,
                    "zone_setpoint": zone.current_setpoint,
                    "schedule_setpoint": zone.schedule_setpoint,
                    "outcome": "holding" if memory.manual_detected_at is not None else "released",
                },
                origin="user" if memory.manual_detected_at is not None else "controller",
            )
        self._save_memory(memory)
        d.last_written_setpoint = memory.last_written_setpoint
        d.last_write = memory.last_written_at
        self._command_data(d)
        await self._store.async_save()
        if hub_data is not None:
            await hub_data.async_save()
        return d
