"""One system with explicit room and measured-system mappings."""

from uuid import uuid4

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers import selector

from .advisor.evidence import TASKS
from .advisor.profiles import profile_info
from .analytics.observations import ROOM_INTENT
from .const import DOMAIN, NAME, SYSTEM_SOURCES, effective_config
from .survey import async_load_survey, suggest_mappings

ANALYTICS_DEFAULTS = {
    "analytics_enabled": False,
    "history_state_policy": "recorded_state",
    "analysis_window_days": 7,
    "update_interval_minutes": 15,
    "comfort_tolerance": 0.3,
    "recovery_minutes": 120,
}
ANALYTICS_VALIDATORS = {
    "analytics_enabled": bool,
    "history_state_policy": selector.SelectSelector(
        selector.SelectSelectorConfig(
            options=[
                {"value": "recorded_state", "label": "Recorded state availability"},
                {"value": "recent_change", "label": "Require recent state changes"},
            ]
        )
    ),
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
                self.pending = {"rooms": [], "advisor": self.current.get("advisor", {})}
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
                self.pending["survey_directory"] = user_input.get("survey_directory", "").strip()
                if self.pending["survey_directory"]:
                    self.survey = await async_load_survey(self.hass, self.pending)
                    if self.survey["status"] == "error":
                        errors["base"] = "invalid_survey"
                    else:
                        self.survey_index = 0
                        self.pending["survey_rooms"] = {}
                        return await self.async_step_survey_rooms()
                else:
                    self.pending["survey_rooms"] = {}
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
                    optional("survey_directory", self.current): str,
                    **{
                        vol.Optional(k, default=self.current.get(k, default)): ANALYTICS_VALIDATORS[
                            k
                        ]
                        for k, default in ANALYTICS_DEFAULTS.items()
                    },
                }
            ),
        )

    async def async_step_survey_rooms(self, user_input=None):
        room = self.pending["rooms"][self.survey_index]
        errors = {}
        if user_input is not None:
            chosen = user_input.get("survey_room", "")
            if chosen and (
                chosen not in self.survey["rooms"]
                or chosen in self.pending["survey_rooms"].values()
            ):
                errors["base"] = "invalid_survey_mapping"
            else:
                if chosen:
                    self.pending["survey_rooms"][room["id"]] = chosen
                self.survey_index += 1
                if self.survey_index < len(self.pending["rooms"]):
                    return await self.async_step_survey_rooms()
                return self.async_create_entry(title=NAME, data=self.pending)
        suggestions = suggest_mappings(self.survey, self.pending)
        existing = self.current.get("survey_rooms", {})
        current = existing.get(room["id"], suggestions.get(room["id"], ""))
        choices = [{"value": "", "label": "Not mapped"}] + [
            {"value": rid, "label": f"{r['name']} ({rid})"}
            for rid, r in self.survey["rooms"].items()
        ]
        return self.async_show_form(
            step_id="survey_rooms",
            errors=errors,
            data_schema=vol.Schema(
                {
                    vol.Optional(
                        "survey_room", default=current if current in self.survey["rooms"] else ""
                    ): selector.SelectSelector(selector.SelectSelectorConfig(options=choices))
                }
            ),
            description_placeholders={
                "room": room["name"],
                "thermostat": room["climate"],
                "warnings": str(len(self.survey["warnings"])),
            },
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
        return self.async_show_menu(step_id="init", menu_options=["mapping", "advisor"])

    async def async_step_mapping(self, user_input=None):
        self.current = effective_config(self.config_entry)
        return await self.choose_rooms("mapping", user_input)

    async def async_step_advisor(self, user_input=None):
        self.current = effective_config(self.config_entry)
        old = self.current.get("advisor", {})
        errors = {}
        if user_input is not None:
            if user_input.get("enabled") and not self.current.get("analytics_enabled"):
                errors["base"] = "advisor_needs_analytics"
            elif any(
                profile_info(self.hass, user_input[t])["status"] != "ready"
                for t in TASKS
                if user_input.get(t)
            ):
                errors["base"] = "invalid_ai_profile"
            elif any(
                user_input.get(f"schedule_{t}") and not user_input.get(t)
                for t in ("daily_summary", "weekly_review")
            ):
                errors["base"] = "missing_ai_profile"
            else:
                return self.async_create_entry(
                    title=NAME, data={**self.current, "advisor": user_input}
                )
        defaults = {
            "enabled": False,
            "schedule_daily_summary": False,
            "schedule_weekly_review": False,
            "schedule_hour": 9,
            "max_calls_per_day": 4,
            "timeout_seconds": 120,
        }
        schema = {
            vol.Optional(k, default=old.get(k, default)): bool
            for k, default in defaults.items()
            if isinstance(default, bool)
        }
        schema.update({optional(t, old): entity_selector(("ai_task",)) for t in TASKS})
        schema.update(
            {
                vol.Optional(k, default=old.get(k, defaults[k])): vol.All(
                    vol.Coerce(int), vol.Range(min=low, max=high)
                )
                for k, low, high in (
                    ("schedule_hour", 0, 23),
                    ("max_calls_per_day", 1, 12),
                    ("timeout_seconds", 30, 300),
                )
            }
        )
        return self.async_show_form(
            step_id="advisor", errors=errors, data_schema=vol.Schema(schema)
        )
