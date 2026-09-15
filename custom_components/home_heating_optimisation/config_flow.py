"""One system with explicit room and measured-system mappings."""

from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .analytics.observations import ROOM_INTENT
from .const import DOMAIN, NAME, SYSTEM_SOURCES, effective_config

ANALYTICS_DEFAULTS = {
    "analytics_enabled": False,
    "analysis_window_days": 7,
    "update_interval_minutes": 15,
    "comfort_tolerance": 0.3,
    "recovery_minutes": 120,
}
ANALYTICS_VALIDATORS = {
    "analytics_enabled": bool,
    "analysis_window_days": vol.All(vol.Coerce(int), vol.Range(min=3, max=14)),
    "update_interval_minutes": vol.All(vol.Coerce(int), vol.Range(min=5, max=60)),
    "comfort_tolerance": vol.All(vol.Coerce(float), vol.Range(min=0.1, max=2)),
    "recovery_minutes": vol.All(vol.Coerce(int), vol.Range(min=15, max=360)),
}


def entity_selector(domains, multiple=False):
    return selector.EntitySelector(
        selector.EntitySelectorConfig(domain=list(domains), multiple=multiple)
    )


def optional(key, current):
    return (
        vol.Optional(key, description={"suggested_value": current[key]})
        if current.get(key)
        else vol.Optional(key)
    )


class MappingFlow:
    """Shared setup/options steps; submitting options replaces all mappings."""

    async def choose_rooms(self, step_id, user_input):
        errors = {}
        if user_input is not None:
            zones = user_input.get("zones", [])
            if not zones:
                errors["base"] = "no_rooms"
            elif len(zones) != len(set(zones)) or any(not z.startswith("climate.") for z in zones):
                errors["base"] = "invalid_source"
            else:
                self.pending = {"rooms": []}
                self.zones = list(zones)
                self.room_index = 0
                return await self.async_step_room()
        return self.async_show_form(
            step_id=step_id,
            data_schema=vol.Schema(
                {
                    vol.Required(
                        "zones", default=[r["climate"] for r in self.current.get("rooms", [])]
                    ): entity_selector(("climate",), multiple=True),
                }
            ),
            errors=errors,
        )

    def invalid_sources(self, values):
        registry = er.async_get(self.hass)
        # Never allow our outputs to feed their own observer.
        return any(
            e and (registered := registry.async_get(e)) and registered.platform == DOMAIN
            for e in values
        )

    async def async_step_room(self, user_input=None):
        zone = self.zones[self.room_index]
        old = next((r for r in self.current.get("rooms", []) if r["climate"] == zone), {})
        state = self.hass.states.get(zone)
        name = old.get("name") or (state.name if state else zone)
        errors = {}
        if user_input is not None:
            if not user_input.get("name", "").strip():
                errors["base"] = "invalid_name"
            elif self.invalid_sources(
                user_input.get(k) for k in ("air_sensor", "demand_sensor", *ROOM_INTENT)
            ):
                errors["base"] = "invalid_source"
            else:
                self.pending["rooms"].append(
                    {
                        "id": old.get("id", uuid4().hex),
                        "climate": zone,
                        "name": user_input["name"].strip(),
                        "air_sensor": user_input.get("air_sensor") or None,
                        "demand_sensor": user_input.get("demand_sensor") or None,
                        **{k: user_input.get(k) or None for k in ROOM_INTENT},
                    }
                )
                self.room_index += 1
                if self.room_index < len(self.zones):
                    return await self.async_step_room()
                return await self.async_step_system()
        return self.async_show_form(
            step_id="room",
            description_placeholders={"room": name},
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Required("name", default=name): str,
                    optional("air_sensor", old): entity_selector(("sensor",)),
                    optional("demand_sensor", old): entity_selector(("sensor",)),
                    **{optional(k, old): entity_selector(("sensor",)) for k in ROOM_INTENT},
                }
            ),
        )

    async def async_step_system(self, user_input=None):
        errors = {}
        if user_input is not None:
            if self.invalid_sources(
                user_input.get(k) for k in (*SYSTEM_SOURCES, "boiler_decision_sensor")
            ):
                errors["base"] = "invalid_source"
            else:
                self.pending.update({k: user_input.get(k) or None for k in SYSTEM_SOURCES})
                self.pending["boiler_decision_sensor"] = (
                    user_input.get("boiler_decision_sensor") or None
                )
                self.pending.update(
                    {k: user_input.get(k, default) for k, default in ANALYTICS_DEFAULTS.items()}
                )
                return self.async_create_entry(title=NAME, data=self.pending)
        return self.async_show_form(
            step_id="system",
            errors=errors,
            data_schema=vol.Schema(
                {
                    **{
                        optional(key, self.current): entity_selector(spec.domains)
                        for key, spec in SYSTEM_SOURCES.items()
                    },
                    optional("boiler_decision_sensor", self.current): entity_selector(("sensor",)),
                    **{
                        vol.Optional(k, default=self.current.get(k, default)): ANALYTICS_VALIDATORS[
                            k
                        ]
                        for k, default in ANALYTICS_DEFAULTS.items()
                    },
                }
            ),
        )


class HeatingConfigFlow(MappingFlow, ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        await self.async_set_unique_id(DOMAIN)
        self._abort_if_unique_id_configured()
        self.current = {}
        return await self.choose_rooms("user", user_input)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry):
        return HeatingOptionsFlow()


class HeatingOptionsFlow(MappingFlow, OptionsFlow):
    async def async_step_init(self, user_input=None):
        self.current = effective_config(self.config_entry)
        return await self.choose_rooms("init", user_input)
