"""One sensor carrying every room's live figures, for the HHO rooms card."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.core import callback
from homeassistant.helpers.device_registry import DeviceInfo

from ..const import DOMAIN, NAME, VERSION

ENTITY_ID = f"sensor.{DOMAIN}_rooms_overview"


def _round(value, digits=1):
    return None if value is None else round(float(value), digits)


def _demand(controls, c):
    """The room's Evohome heat demand in %, from its primary thermostat (None if unknown)."""
    state = controls.hass.states.get(c._config.get("primary_climate") or "")
    try:
        value = float(state.attributes.get("heat_demand")) if state else None
    except TypeError, ValueError:
        return None
    return None if value is None or not 0 <= value <= 1 else round(value * 100)


def room_view(controls, room_id, c, preheat):
    """What the card shows for one room."""
    d = c.data
    plan = preheat.get(room_id) or {}
    if d is not None and d.preheating:
        pre = {"status": "active", "warm_by": plan.get("warm_by")}
    elif plan.get("planned_start") and not plan.get("started"):
        pre = {"status": "planned", "starts": plan["planned_start"], "warm_by": plan.get("warm_by")}
    else:
        pre = None
    if d is None:
        return {"id": room_id, "name": c.room_name, "state": "no_data", "preheat": pre}
    target = d.preheat_setpoint if d.preheating else d.air_setpoint
    push = d.preheat_push_k if d.preheating else d.overdrive_k
    demand = _demand(controls, c)
    return {
        "id": room_id,
        "name": c.room_name,
        "state": d.state,
        "air": _round(d.air_temp, 2),
        "target": _round(target, 2),
        "overdrive": _round(push),
        "evohome": _round(d.zone_setpoint),
        "evohome_temp": _round(d.zone_temp),
        "demand": demand,
        "calling": bool(demand),
        "preheat": pre,
    }


def house_view(controls):
    b = controls.boiler.data if controls.boiler else None
    if b is None:
        return {}
    return {
        "outdoor": _round(b.outdoor_temp),
        "flow": _round(b.current_flow),
        "flow_target": _round(b.flow_setpoint),
        "return": _round(b.return_temperature_used),
        "burner": bool(b.burner_power) if b.burner_power is not None else None,
        "mode": b.mode,
    }


class RoomsOverviewSensor(SensorEntity):
    """State: rooms calling for heat. Attributes: every room's figures and the boiler's."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_icon = "mdi:home-thermometer"
    # Live view only: the room lists change every minute and are already recorded elsewhere.
    _unrecorded_attributes = frozenset({"rooms", "house"})

    def __init__(self, controls):
        self.controls = controls
        self.entity_id = ENTITY_ID
        self._attr_unique_id = f"{controls.entry.entry_id}:system:rooms_overview"
        self._attr_name = "Rooms overview"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, controls.entry.entry_id)},
            name=NAME,
            sw_version=VERSION,
            manufacturer=NAME,
            model="Heating control and analytics",
        )
        self._snapshot = None

    async def async_added_to_hass(self):
        sources = [c for c in (self.controls.boiler, *self.controls.rooms.values()) if c]
        for coordinator in sources:
            self.async_on_remove(coordinator.async_add_listener(self._changed))
        self._changed()

    def _preheat(self):
        optimum = getattr(self.controls.heating, "optimum", None)
        try:
            return optimum.preheat_report()["rooms"] if optimum else {}
        except Exception:  # noqa: BLE001 - the overview must never break on the planner
            return {}

    @callback
    def _changed(self):
        preheat = self._preheat()
        rooms = [
            room_view(self.controls, rid, c, preheat) for rid, c in self.controls.rooms.items()
        ]
        snapshot = {"rooms": rooms, "house": house_view(self.controls)}
        if snapshot == self._snapshot:
            return
        self._snapshot = snapshot
        self._attr_native_value = sum(1 for r in rooms if r.get("calling"))
        self._attr_extra_state_attributes = {**snapshot, "card_version": VERSION}
        if self.hass is not None:
            self.async_write_ha_state()
