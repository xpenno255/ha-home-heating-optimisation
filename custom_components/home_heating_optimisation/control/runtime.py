"""Own both engines and gate every actuator write against live ownership."""

import asyncio
import logging
from dataclasses import asdict
from datetime import timedelta

from homeassistant.config_entries import ConfigEntryState
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from ..const import DOMAIN
from .boiler.coordinator import BFCCoordinator
from .boiler.hub import BoilerFlowHub
from .comfort.coordinator import OTCoordinator
from .comfort.hub import OTHubData
from .store import ControlStore

_LOGGER = logging.getLogger(__name__)

# How long a user-facing service waits for a coordinator refresh before returning.
# The refresh itself keeps running; only the wait is bounded.
REFRESH_WAIT_TIMEOUT = 30.0

LEGACY = ("ot_thermostat_control", "boiler_flow_control")
ROOM_SENSORS = {
    "state": None,
    "target_ot": "°C",
    "air_setpoint": "°C",
    "air_temp": "°C",
    "operative_temp": "°C",
    "would_write": "°C",
    "schedule_setpoint": "°C",
    "offset_final": "K",
    "flow_temp_used": "°C",
}
BOILER_SENSORS = {
    "mode": None,
    "flow_setpoint": "°C",
    "dhw_status": None,
    "cycling_status": None,
    "cycles_10min": None,
    "return_temperature_used": "°C",
    "burner_power": "%",
}


class Controls:
    def __init__(self, hass, entry, heating):
        self.hass, self.entry, self.heating = hass, entry, heating
        self.config = heating.config.get("control")
        self.rooms = {}
        self.boiler = None
        self.closed = False
        self.ready = False
        self.lock = asyncio.Lock()
        self.stores = []
        self.settings = ControlStore(hass, entry.entry_id + ".settings")
        self.status = "not_imported"
        self.entities = {}

    def registry_id(self, scope, key, domain="sensor"):
        registry = er.async_get(self.hass)
        entity = registry.async_get_or_create(
            domain,
            DOMAIN,
            f"{self.entry.entry_id}:control:{scope}:{key}",
            suggested_object_id=f"home_heating_optimisation_control_{scope}_{key}",
            config_entry=self.entry,
        )
        self.entities[(scope, key)] = entity.entity_id
        return entity.entity_id

    def wire_config(self):
        if not self.config:
            return
        cfg = self.heating.config
        cfg["dhw_active"] = self.registry_id("boiler", "dhw", "binary_sensor")
        cfg["boiler_decision_sensor"] = self.registry_id("boiler", "flow_setpoint")
        for room in cfg["rooms"]:
            scope = room["id"]
            if scope not in self.config["rooms"]:
                continue
            for key, field in (
                ("comfort_target_sensor", "target_ot"),
                ("corrected_air_target_sensor", "air_setpoint"),
                ("estimated_operative_sensor", "operative_temp"),
                ("decision_sensor", "state"),
            ):
                room[key] = self.registry_id(scope, field)
        self.config["hub"]["dhw_active_entity"] = cfg["dhw_active"]

    async def store(self, scope, seed=None):
        store = ControlStore(self.hass, self.entry.entry_id + "." + scope, seed)
        await store.async_load()
        self.stores.append(store)
        return store

    async def initialise(self):
        if not self.config:
            return
        if self.config.get("schema") != 1:
            raise ValueError("Unsupported control import")
        await self.settings.async_load()
        hub_store = await self.store("hub", self.config.get("hub_seed"))
        self.hub = OTHubData(store=hub_store)
        self.hub.load()
        self.hub.global_enabled = self.settings.get(
            "global_enabled", self.config.get("global_enabled", True)
        )
        self.hass.data[DOMAIN] = {
            "hub": {"config": self.config["hub"], "data": self.hub},
            "rooms": {},
        }
        boiler_spec = self.config["boiler"]
        boiler_seed = dict(boiler_spec.get("seed", {}))
        boiler_seed.setdefault("enabled", boiler_spec.get("enabled", True))
        store = await self.store("boiler", boiler_seed)
        hub = BoilerFlowHub(store=store)
        hub.load()
        self.boiler = BFCCoordinator(
            self.hass,
            self.entry,
            store,
            hub,
            config=self.config["boiler"]["config"],
            state_reader=self.heating.telemetry.get,
            write_guard=lambda: self.can_write("boiler"),
            journal=getattr(self.heating, "journal", None),
        )
        # Do not act while configuration and all entities are still restoring.
        self.boiler.override = "shadow"
        await self.boiler.async_config_entry_first_refresh()
        for room_id, spec in self.config["rooms"].items():
            store = await self.store("room." + room_id, spec.get("seed"))
            c = OTCoordinator(
                self.hass,
                self.entry,
                store,
                config=spec["config"],
                state_reader=self.heating.telemetry.get,
                write_guard=lambda rid=room_id: self.can_write(rid),
                journal=getattr(self.heating, "journal", None),
            )
            c.mode = "shadow"
            c.journal_room_id = room_id
            c.enabled = spec.get("enabled", True)
            c.occupancy_enabled = spec.get("occupancy_enabled", True)
            await c.async_load_geometry()
            self.rooms[room_id] = c
            self.hass.data[DOMAIN]["rooms"][c.room_id] = c
        for c in self.rooms.values():
            await c.async_config_entry_first_refresh()
        self.status = "shadow"

    def conflicts(self):
        # A loaded legacy controller is a potential writer even if currently shadow.
        return [
            e.entry_id
            for domain in LEGACY
            for e in self.hass.config_entries.async_entries(domain)
            if e.disabled_by is None or e.state not in (ConfigEntryState.NOT_LOADED,)
        ]

    def guard_reason(self, scope):
        if not self.ready or self.closed:
            return "control setup or shutdown in progress"
        if not self.settings.ready or any(not s.ready for s in self.stores):
            return "control storage unavailable"
        if self.settings.get("ownership") != "ready":
            return "complete legacy handover first"
        if self.conflicts():
            return "legacy controllers are still enabled"
        if self.automation_conflicts(scope):
            return "an enabled automation or running script may write this actuator"
        if scope != "boiler":
            c = self.rooms[scope]
            if c.geometry is None:
                return "room survey unavailable"
            now = dt_util.utcnow()
            st = c._state(c._config.get("primary_climate"))
            if (
                st is None
                or st.state in ("unavailable", "unknown")
                or not timedelta(0) <= now - st.last_reported <= timedelta(minutes=30)
            ):
                return "thermostat missing or stale"
            air, air_source = c._air_temperature(c.geometry, [])
            if air is None:
                return "room temperature unavailable"
            air_entity = air_source.removesuffix(".current_temperature")
            air_state = c._state(air_entity)
            if air_state is None or not timedelta(0) <= now - air_state.last_reported <= timedelta(
                minutes=30
            ):
                return "room temperature stale"
            if c._schedule().schedule_setpoint is None:
                return "schedule unavailable"
        return None

    def automation_conflicts(self, scope):
        """Conservatively inspect loaded automation/script actions for competing writes."""
        target = (
            self.boiler._config.get("flow_setpoint_entity")
            if scope == "boiler"
            else self.rooms[scope]._config.get("primary_climate")
        )
        services = (
            {"number.set_value"}
            if scope == "boiler"
            else {"climate.set_temperature", "ramses_cc.set_zone_mode"}
        )

        def writes(value):
            if isinstance(value, list):
                return any(writes(v) for v in value)
            if not isinstance(value, dict):
                return False
            service = value.get("action", value.get("service"))
            if isinstance(service, str) and service in services:
                data = {**value.get("data", {}), **value.get("target", {})}
                entities = data.get("entity_id")
                if (
                    entities is None
                    or target in (entities if isinstance(entities, list) else [entities])
                    or (isinstance(entities, str) and "{{" in entities)
                ):
                    return True
            return any(writes(v) for v in value.values() if isinstance(v, (dict, list)))

        found = []
        for domain in ("automation", "script"):
            component = self.hass.data.get(domain)
            for entity in getattr(component, "entities", []):
                if not entity.is_on:
                    continue
                if writes(getattr(entity, "raw_config", {})):
                    found.append(entity.entity_id)
        return found

    def can_write(self, scope):
        return self.guard_reason(scope) is None

    async def start(self):
        if not self.config:
            return
        self.hub.restore_complete = True
        for c in self.rooms.values():
            c.mark_restore_complete()
        for subscribe in (
            self.boiler.async_subscribe_heating_active,
            self.boiler.async_subscribe_dhw,
        ):
            unsub = subscribe()
            if unsub:
                self.entry.async_on_unload(unsub)
        for scope, c in [("boiler", self.boiler), *self.rooms.items()]:
            for key in ("enabled", "occupancy_enabled"):
                value = self.settings.get("flags", {}).get(f"{scope}:{key}")
                if value is not None and hasattr(c, key):
                    setattr(c, key, value)
        self.ready = True
        # Only persisted, completed ownership can restore active modes.
        if self.settings.get("ownership") == "ready" and not self.conflicts():
            for scope, mode in self.settings.get("modes", {}).items():
                if scope == "boiler":
                    self.boiler.override = mode
                elif scope in self.rooms:
                    self.rooms[scope].mode = mode
        await self.refresh()

    async def refresh(self):
        if self.boiler:
            await self._bounded_refresh(self.boiler, "boiler")
        for scope, c in self.rooms.items():
            await self._bounded_refresh(c, scope)

    async def _bounded_refresh(self, coordinator, scope):
        """Refresh without letting a wedged cycle hang the caller.

        The refresh continues in the background (shielded) so a slow actuator is
        still bounded by the coordinator's own timeouts; the caller stops waiting.
        """
        task = self.hass.async_create_task(coordinator.async_refresh())
        try:
            await asyncio.wait_for(asyncio.shield(task), REFRESH_WAIT_TIMEOUT)
        except TimeoutError:
            _LOGGER.warning(
                "%s: refresh did not complete within %.0f s; continuing without waiting",
                scope,
                REFRESH_WAIT_TIMEOUT,
            )

    async def set_mode(self, scope, mode):
        async with self.lock:
            allowed = ("shadow", "auto", "hold") if scope == "boiler" else ("shadow", "active")
            if scope != "boiler" and scope not in self.rooms or mode not in allowed:
                raise ServiceValidationError("Unknown controller or mode")
            if mode in ("active", "auto") and (reason := self.guard_reason(scope)):
                raise ServiceValidationError(reason)
            modes = dict(self.settings.get("modes", {}))
            modes[scope] = mode
            self.settings.set("modes", modes)
            await self.settings.async_save()
            c = self.boiler if scope == "boiler" else self.rooms[scope]
            previous = c.override if scope == "boiler" else c.mode
            if scope == "boiler":
                c.override = mode
            else:
                c.mode = mode
            self.journal_event(
                "mode_change",
                scope,
                {"from": previous, "to": mode, "outcome": "applied"},
                origin="user",
            )
            await self._bounded_refresh(c, scope)

    def journal_event(self, kind, scope, data, origin="controller"):
        """Record a control-level event; failures are logged and never propagate."""
        journal = getattr(self.heating, "journal", None)
        if journal is None:
            return None
        try:
            return journal.record(
                kind,
                room_id=None if scope in ("boiler", "system") else scope,
                scope=scope,
                origin=origin,
                data=data,
            )
        except Exception:  # noqa: BLE001
            return None

    def report(self):
        return {
            "status": "active"
            if self.boiler
            and (
                self.boiler.override == "auto"
                or any(c.mode == "active" for c in self.rooms.values())
            )
            else self.status,
            "ownership": self.settings.get("ownership", "unclaimed"),
            "legacy_conflicts": self.conflicts(),
            "boiler": asdict(self.boiler.data) if self.boiler and self.boiler.data else None,
            "rooms": {
                rid: {
                    **asdict(c.data),
                    "write_status": c.write_status,
                    "activation_block": self.guard_reason(rid),
                }
                for rid, c in self.rooms.items()
                if c.data
            },
            "gateways": self.gateway_report(),
        }

    def gateway_report(self):
        monitor = getattr(self.heating, "gateways", None)
        if monitor is None:
            return {"status": "unconfigured"}
        try:
            return monitor.report()
        except Exception as err:  # noqa: BLE001 - monitoring must not break the report
            return {"status": "error", "last_error": f"{type(err).__name__}: {err}"}

    async def stop(self):
        self.closed = True
        self.ready = False
        # Stop callbacks before cancelling in-flight updates; no later call may acquire ownership.
        for c in [self.boiler, *self.rooms.values()]:
            if c:
                await c.async_shutdown()
                async with c._cycle_lock:
                    pass
        if self.config:
            self.hass.data.pop(DOMAIN, None)
