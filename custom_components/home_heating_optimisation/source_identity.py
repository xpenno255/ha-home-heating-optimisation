"""Keep configured HHO entity references aligned with HA registry renames.

Home Assistant changes an entity ID in the entity registry without changing
the string stored in a config entry.  This module handles the registry's
``entity_registry_updated`` rename event and updates only fields which are
defined as entity references by HHO's observation and imported control
schemas.

The rewrite is deliberately schema-shaped rather than a recursive string
replacement.  Configurations contain names, notes, templates, history and a
rollback snapshot which must retain their original values.
"""

from __future__ import annotations

from copy import deepcopy
from functools import partial
from typing import Any, Mapping

from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import entity_registry as er

from .analytics.observations import ROOM_INTENT
from .const import DOMAIN, SYSTEM_SOURCES

# The key is intentionally outside hass.data[DOMAIN].  Controls temporarily
# own that value during setup and replace it with their runtime namespace.
_UNSUB_KEY = f"{DOMAIN}.source_identity_unsubscribe"

OBSERVATION_FIELDS = (*SYSTEM_SOURCES, "boiler_decision_sensor")
OBSERVATION_ROOM_FIELDS = ("climate", "air_sensor", "demand_sensor", *ROOM_INTENT)

# These are the entity-bearing fields used by the legacy controller schemas
# copied into control.{hub,rooms,boiler}.  Keeping this list explicit avoids
# rewriting free text or arbitrary template values.
HUB_FIELDS = (
    "weather_entity",
    "outdoor_temp_sensor",
    "outdoor_temperature_entity",
    "outdoor_humidity_sensor",
    "wind_speed_sensor",
    "solar_sensor",
    "solar_radiation_entity",
    "apparent_temp_entity",
    "irradiance_sensor",
    "flow_temp_entity",
    "dhw_active_entity",
)
ROOM_CONTROL_FIELDS = (
    "primary_climate",
    "backup_climate",
    "air_temp_sensor",
    "weather_entity",
    "outdoor_temp_sensor",
    "outdoor_humidity_sensor",
    "wind_speed_sensor",
    "solar_sensor",
    "apparent_temp_entity",
    "irradiance_sensor",
    "flow_temp_entity",
    "dhw_active_entity",
    "occupancy_sensor",
    "window_sensors",
    "adjacent_sensors",
)
BOILER_FIELDS = (
    "flow_setpoint_entity",
    "outdoor_temp_entity",
    "current_flow_entity",
    "return_temp_entity",
    "heating_active_entity",
    "burner_power_entity",
    "heat_demand_entity",
    "hw_relay_demand_entity",
    "cylinder_temp_entity",
    "max_flow_entity",
    "cylinder_target_entity",
    "boiler_relay_entity",
    "dhw_active_entity",
    "outdoor_temperature_entity",
    "zone_demand_entities",
    "room_climate_entities",
)


def _replace_field(mapping: dict[str, Any], key: str, old: str, new: str) -> bool:
    """Replace an entity ID in one allowlisted scalar or list field."""
    value = mapping.get(key)
    if isinstance(value, str):
        if value == old:
            mapping[key] = new
            return True
        return False
    if isinstance(value, list):
        changed = False
        for index, item in enumerate(value):
            if item == old:
                value[index] = new
                changed = True
        return changed
    return False


def _replace_fields(mapping: Any, fields: tuple[str, ...], old: str, new: str) -> bool:
    if not isinstance(mapping, dict):
        return False
    changed = False
    for key in fields:
        changed = _replace_field(mapping, key, old, new) or changed
    return changed


def update_source_references(
    config: Mapping[str, Any], old_entity_id: str, new_entity_id: str
) -> dict[str, Any]:
    """Return a copy of *config* with declared entity references rewritten.

    Only exact matches in known entity-reference fields are changed.  In
    particular, this function does not inspect ``control["observer_before"]``
    or survey files and does not replace substrings inside templates or text.
    """
    updated = deepcopy(dict(config))
    _replace_fields(updated, OBSERVATION_FIELDS, old_entity_id, new_entity_id)

    for room in updated.get("rooms", []):
        _replace_fields(room, OBSERVATION_ROOM_FIELDS, old_entity_id, new_entity_id)

    # ``control`` is an imported snapshot.  observer_before is deliberately
    # not traversed: it is the old observer configuration used by rollback.
    control = updated.get("control")
    if not isinstance(control, dict):
        control = None
    if control is not None:
        _replace_fields(control.get("hub"), HUB_FIELDS, old_entity_id, new_entity_id)
        for room in (control.get("rooms") or {}).values():
            if not isinstance(room, dict):
                continue
            _replace_field(room, "air_source", old_entity_id, new_entity_id)
            _replace_fields(room.get("config"), ROOM_CONTROL_FIELDS, old_entity_id, new_entity_id)
        boiler = control.get("boiler")
        if isinstance(boiler, dict):
            _replace_fields(boiler.get("config"), BOILER_FIELDS, old_entity_id, new_entity_id)

    # mqtt_sources contains discovered bindings.  The topic and field are
    # retained because a registry rename does not change MQTT transport.
    bindings = updated.get("mqtt_sources")
    if isinstance(bindings, list):
        for binding in bindings:
            if isinstance(binding, dict):
                _replace_field(binding, "entity_id", old_entity_id, new_entity_id)

    return updated


# A descriptive alias for callers that prefer an imperative name.
replace_entity_id_references = update_source_references


def _rename_from_event(event: Event) -> tuple[str, str] | None:
    data = event.data
    if data.get("action") != "update":
        return None
    old = data.get("old_entity_id")
    new = data.get("entity_id")
    if not isinstance(old, str) or not isinstance(new, str) or old == new:
        return None
    return old, new


@callback
def async_register_source_identity(hass: HomeAssistant):
    """Register the idempotent process-wide registry rename listener.

    The integration can call this from ``async_setup`` or setup-entry.  The
    returned unsubscribe callback may be attached to an entry when desired;
    repeated calls reuse the existing listener.
    """
    if _UNSUB_KEY not in hass.data:
        hass.data[_UNSUB_KEY] = hass.bus.async_listen(
            er.EVENT_ENTITY_REGISTRY_UPDATED,
            partial(async_handle_entity_registry_updated, hass),
        )
    return hass.data[_UNSUB_KEY]


async def async_handle_entity_registry_updated(hass: HomeAssistant, event: Event) -> int:
    """Apply one registry rename to every HHO entry and return entries changed."""
    rename = _rename_from_event(event)
    if rename is None:
        return 0
    old, new = rename
    changed_entries = 0
    for entry in hass.config_entries.async_entries(DOMAIN):
        data = dict(entry.data)
        options = dict(entry.options)
        new_data = update_source_references(data, old, new)
        new_options = update_source_references(options, old, new)
        data_changed = new_data != data
        options_changed = new_options != options
        if not data_changed and not options_changed:
            continue
        kwargs = {}
        if data_changed:
            kwargs["data"] = new_data
        if options_changed:
            kwargs["options"] = new_options
        hass.config_entries.async_update_entry(entry, **kwargs)
        changed_entries += 1
    return changed_entries
