"""One system with explicit room and measured-system mappings."""

from pathlib import Path
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
from .control.boiler.core.efficiency import (
    CONF_EFFICIENCY_PROFILE,
    PROFILE_BG430I_NATURAL_GAS,
    PROFILE_DISABLED,
)
from .control.configuration import (
    BOILER_DEFAULTS,
    HUB_DEFAULTS,
    ROOM_DEFAULTS,
    ControlConfigError,
    actuator_changed,
    add_observation_rooms,
    boiler_values,
    editable_control,
    hub_values,
    room_values,
    update_boiler,
    update_hub,
    update_room,
    validate_control_rooms,
)
from .control.store import ControlStore
from .survey import async_load_survey, suggest_mappings

ANALYTICS_DEFAULTS = {
    "analytics_enabled": False,
    "journal_enabled": True,
    "history_state_policy": "recorded_state",
    "analysis_window_days": 7,
    "update_interval_minutes": 15,
    "comfort_tolerance": 0.3,
    "recovery_minutes": 120,
}
ANALYTICS_VALIDATORS = {
    "analytics_enabled": bool,
    "journal_enabled": bool,
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


def required(key, current, validator):
    marker = (
        vol.Required(key, default=current[key])
        if current.get(key) not in (None, "")
        else vol.Required(key)
    )
    return marker, validator


def number_field(key, current, default, low, high):
    return (
        vol.Required(key, default=current.get(key, default)),
        vol.All(vol.Coerce(float), vol.Range(min=low, max=high)),
    )


class MappingFlow:
    """Shared setup/options steps; submitting options replaces all mappings."""

    async def choose_rooms(self, step_id, user_input):
        errors = {}
        if user_input is not None:
            zones = user_input.get("zones", [])
            if not zones:
                errors["base"] = "no_rooms"
            elif self.current.get("control") and not {
                spec["config"]["primary_climate"]
                for spec in self.current["control"].get("rooms", {}).values()
            }.issubset(zones):
                errors["base"] = "controlled_rooms_locked"
            elif len(zones) != len(set(zones)) or any(not z.startswith("climate.") for z in zones):
                errors["base"] = "invalid_source"
            else:
                self.pending = {
                    "rooms": [],
                    **{
                        k: self.current[k]
                        for k in ("advisor", "control", "mqtt_sources")
                        if k in self.current
                    },
                }
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
        controlled = next(
            (
                spec.get("config", {})
                for spec in self.current.get("control", {}).get("rooms", {}).values()
                if spec.get("config", {}).get("primary_climate") == zone
            ),
            None,
        )
        control_source = "No comfort controller is configured for this thermostat."
        if controlled is not None:
            control_source = controlled.get("air_temp_sensor") or (
                "survey preferred air source, then thermostat fallbacks"
            )
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
            description_placeholders={"room": name, "control_source": control_source},
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
                control_dir = self.current.get("control", {}).get("hub", {}).get("house_dir")
                if control_dir and self.pending["survey_directory"] != control_dir:
                    errors["base"] = "controlled_survey_locked"
                    return self.async_show_form(
                        step_id="system",
                        errors=errors,
                        data_schema=self.system_schema(),
                    )
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
            step_id="system", errors=errors, data_schema=self.system_schema()
        )

    def system_schema(self):
        return vol.Schema(
            {
                **{
                    optional(key, self.current): entity_selector(spec.domains)
                    for key, spec in SYSTEM_SOURCES.items()
                },
                optional("boiler_decision_sensor", self.current): entity_selector(("sensor",)),
                optional("survey_directory", self.current): str,
                **{
                    vol.Optional(k, default=self.current.get(k, default)): ANALYTICS_VALIDATORS[k]
                    for k, default in ANALYTICS_DEFAULTS.items()
                },
            }
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
        return self.async_show_menu(step_id="init", menu_options=["mapping", "control", "advisor"])

    async def async_step_mapping(self, user_input=None):
        self.current = effective_config(self.config_entry)
        return await self.choose_rooms("mapping", user_input)

    def control_active(self):
        runtime = getattr(self.config_entry, "runtime_data", None)
        controls = getattr(runtime, "controls", None)
        if controls is None:
            return False
        if controls.boiler and controls.boiler.override == "auto":
            return True
        if any(room.mode == "active" for room in controls.rooms.values()):
            return True
        modes = controls.settings.get("modes", {}) if controls.settings.ready else {}
        return any(mode in ("active", "auto") for mode in modes.values())

    def hydrate_control_state(self):
        """Preserve live switches that are outside the section being edited."""
        runtime = getattr(self.config_entry, "runtime_data", None)
        controls = getattr(runtime, "controls", None)
        if controls is None:
            return
        edited = self.control_edited_sections
        hub = getattr(controls, "hub", None)
        boiler = getattr(controls, "boiler", None)
        if "hub" not in edited and hub is not None:
            self.control_pending["global_enabled"] = hub.global_enabled
            self.control_pending["hub"]["global_enabled"] = hub.global_enabled
        if "boiler" not in edited and boiler is not None:
            self.control_pending["boiler"]["enabled"] = boiler.enabled
        for room_id, controller in getattr(controls, "rooms", {}).items():
            if room_id in self.control_pending["rooms"] and f"room:{room_id}" not in edited:
                self.control_pending["rooms"][room_id]["enabled"] = controller.enabled
                self.control_pending["rooms"][room_id]["occupancy_enabled"] = (
                    controller.occupancy_enabled
                )

    def begin_control_edit(self, *, full=False):
        self.current = effective_config(self.config_entry)
        self.control_pending = editable_control(self.current)
        self.control_full_setup = full
        self.control_edited_sections = set()
        self.hydrate_control_state()

    async def load_control_survey(self):
        directory = self.control_pending.get("hub", {}).get("house_dir", "")
        base = Path(self.hass.config.path()).resolve()
        path = Path(directory)
        resolved = (path if path.is_absolute() else base / path).resolve()
        if not resolved.is_relative_to(base):
            return {"status": "error", "error": "outside_config"}
        return await async_load_survey(self.hass, {"survey_directory": directory})

    async def async_step_control(self, user_input=None):
        self.current = effective_config(self.config_entry)
        if self.control_active():
            return self.async_abort(reason="control_active")
        if not self.current.get("control"):
            self.begin_control_edit(full=True)
            return await self.async_step_control_hub()
        return self.async_show_menu(
            step_id="control",
            menu_options=["control_hub", "control_boiler", "control_rooms"],
        )

    async def async_step_control_hub(self, user_input=None):
        if not hasattr(self, "control_pending"):
            self.begin_control_edit()
        current = hub_values(self.control_pending)
        errors = {}
        if user_input is not None:
            if self.control_active():
                errors["base"] = "control_active"
            elif self.invalid_sources(
                user_input.get(key)
                for key in ("outdoor_temp_sensor", "flow_temp_entity", "irradiance_sensor")
            ):
                errors["base"] = "invalid_source"
            else:
                try:
                    update_hub(self.control_pending, user_input)
                except ControlConfigError as err:
                    errors["base"] = err.code
                else:
                    self.control_survey = await self.load_control_survey()
                    if self.control_survey["status"] == "error":
                        errors["base"] = "control_invalid_survey"
                    else:
                        self.control_edited_sections.add("hub")
                        if self.control_full_setup:
                            return await self.async_step_control_boiler()
                        return await self.finish_control_edit()
        hub_schema = dict(
            [
                required(
                    "house_dir",
                    current,
                    selector.TextSelector(selector.TextSelectorConfig()),
                ),
                (
                    vol.Required("global_enabled", default=current.get("global_enabled", True)),
                    bool,
                ),
                required("outdoor_temp_sensor", current, entity_selector(("sensor",))),
                required("flow_temp_entity", current, entity_selector(("sensor",))),
                (optional("weather_entity", current), entity_selector(("weather",))),
                (optional("irradiance_sensor", current), entity_selector(("sensor",))),
                number_field("manual_flow_temp", current, HUB_DEFAULTS["manual_flow_temp"], 20, 90),
                number_field("ground_temp", current, HUB_DEFAULTS["ground_temp"], -10, 30),
            ]
        )
        return self.async_show_form(
            step_id="control_hub", errors=errors, data_schema=vol.Schema(hub_schema)
        )

    async def async_step_control_boiler(self, user_input=None):
        if not hasattr(self, "control_pending"):
            self.begin_control_edit()
        current = boiler_values(self.control_pending)
        errors = {}
        if user_input is not None:
            entity_keys = (
                "flow_setpoint_entity",
                "outdoor_temp_entity",
                "current_flow_entity",
                "return_temp_entity",
                "heating_active_entity",
                "burner_power_entity",
                "heat_demand_entity",
                "hw_relay_demand_entity",
                "cylinder_temp_entity",
                "cylinder_target_entity",
                "max_flow_entity",
                "boiler_relay_entity",
            )
            selected_entities = [user_input.get(key) for key in entity_keys]
            selected_entities.extend(user_input.get("zone_demand_entities") or [])
            selected_entities.extend(user_input.get("room_climate_entities") or [])
            if self.control_active():
                errors["base"] = "control_active"
            elif self.invalid_sources(selected_entities):
                errors["base"] = "invalid_source"
            else:
                try:
                    update_boiler(self.control_pending, user_input)
                except ControlConfigError as err:
                    errors["base"] = err.code
                else:
                    self.control_edited_sections.add("boiler")
                    if self.control_full_setup:
                        if not self.current.get("rooms"):
                            errors["base"] = "control_no_rooms"
                        else:
                            return await self.async_step_control_rooms()
                    else:
                        return await self.finish_control_edit()
        fields = {
            vol.Required("enabled", default=current.get("enabled", True)): bool,
            **dict(
                [
                    required("flow_setpoint_entity", current, entity_selector(("number",))),
                    required("outdoor_temp_entity", current, entity_selector(("sensor",))),
                ]
            ),
        }
        for key, domains in {
            "current_flow_entity": ("sensor",),
            "return_temp_entity": ("sensor",),
            "heating_active_entity": ("binary_sensor",),
            "burner_power_entity": ("sensor",),
            "heat_demand_entity": ("sensor",),
            "hw_relay_demand_entity": ("sensor",),
            "cylinder_temp_entity": ("sensor",),
            "cylinder_target_entity": ("number", "sensor", "climate", "water_heater"),
            "max_flow_entity": ("number", "sensor"),
            "boiler_relay_entity": ("switch", "binary_sensor"),
        }.items():
            fields[optional(key, current)] = entity_selector(domains)
        fields[
            vol.Optional(
                "zone_demand_entities",
                default=current.get("zone_demand_entities", []),
            )
        ] = entity_selector(("sensor",), multiple=True)
        fields[
            vol.Optional(
                "room_climate_entities",
                default=current.get("room_climate_entities", []),
            )
        ] = entity_selector(("climate",), multiple=True)
        for key, default, low, high in (
            ("flow_min", BOILER_DEFAULTS["flow_min"], 20, 80),
            ("flow_max", BOILER_DEFAULTS["flow_max"], 25, 90),
            ("dhw_flow_min", BOILER_DEFAULTS["dhw_flow_min"], 30, 90),
            ("dhw_flow_max", BOILER_DEFAULTS["dhw_flow_max"], 35, 90),
            ("dhw_return_ceiling", BOILER_DEFAULTS["dhw_return_ceiling"], 20, 80),
            ("min_hold_minutes", BOILER_DEFAULTS["min_hold_minutes"], 0, 120),
            ("manual_hold_minutes", BOILER_DEFAULTS["manual_hold_minutes"], 0, 1440),
            ("dhw_target", BOILER_DEFAULTS["dhw_target"], 35, 80),
            ("dhw_progress_minutes", BOILER_DEFAULTS["dhw_progress_minutes"], 5, 180),
            ("dhw_timeout_minutes", BOILER_DEFAULTS["dhw_timeout_minutes"], 15, 360),
            (
                "input_freshness_minutes",
                BOILER_DEFAULTS["input_freshness_minutes"],
                1,
                120,
            ),
            (
                "outdoor_freshness_minutes",
                BOILER_DEFAULTS["outdoor_freshness_minutes"],
                5,
                360,
            ),
            ("dhw_fallback_flow", BOILER_DEFAULTS["dhw_fallback_flow"], 35, 90),
        ):
            marker, validator = number_field(key, current, default, low, high)
            fields[marker] = validator
        fields[
            vol.Optional(
                CONF_EFFICIENCY_PROFILE,
                default=current.get(CONF_EFFICIENCY_PROFILE, PROFILE_DISABLED),
            )
        ] = selector.SelectSelector(
            selector.SelectSelectorConfig(
                options=[
                    {"value": PROFILE_DISABLED, "label": "Disabled"},
                    {
                        "value": PROFILE_BG430I_NATURAL_GAS,
                        "label": "British Gas 430/i - natural gas (estimated)",
                    },
                ]
            )
        )
        return self.async_show_form(
            step_id="control_boiler", errors=errors, data_schema=vol.Schema(fields)
        )

    async def async_step_control_rooms(self, user_input=None):
        if not hasattr(self, "control_pending"):
            self.begin_control_edit()
        if not hasattr(self, "control_room_index"):
            add_observation_rooms(self.control_pending, self.current)
            self.control_room_index = 0
        if not self.current.get("rooms"):
            return self.async_abort(reason="control_no_rooms")
        if not hasattr(self, "control_survey"):
            self.control_survey = await self.load_control_survey()
        room = self.current["rooms"][self.control_room_index]
        current = room_values(self.control_pending, room)
        errors = {}
        if user_input is not None:
            if self.control_active():
                errors["base"] = "control_active"
            elif self.invalid_sources(
                user_input.get(key)
                for key in (
                    "primary_climate",
                    "backup_climate",
                    "air_temp_sensor",
                    "occupancy_sensor",
                )
            ):
                errors["base"] = "invalid_source"
            else:
                try:
                    update_room(self.control_pending, room, user_input)
                    if self.control_survey["status"] == "error":
                        raise ControlConfigError("control_invalid_survey")
                    if (
                        self.control_pending["rooms"][room["id"]]["config"]["room_id"]
                        not in self.control_survey["rooms"]
                    ):
                        raise ControlConfigError("control_required_survey_room")
                    validate_control_rooms(self.control_pending)
                except ControlConfigError as err:
                    errors["base"] = err.code
                else:
                    self.control_edited_sections.add(f"room:{room['id']}")
                    self.control_room_index += 1
                    if self.control_room_index < len(self.current["rooms"]):
                        return await self.async_step_control_rooms()
                    return await self.finish_control_edit()
        fields = {
            vol.Required("enabled", default=current.get("enabled", True)): bool,
            vol.Required("occupancy_enabled", default=current.get("occupancy_enabled", True)): bool,
            **dict(
                [
                    required("primary_climate", current, entity_selector(("climate",))),
                    required(
                        "room_id",
                        current,
                        selector.TextSelector(selector.TextSelectorConfig()),
                    ),
                ]
            ),
            optional("backup_climate", current): entity_selector(("climate",)),
            optional("air_temp_sensor", current): entity_selector(("sensor",)),
            optional("occupancy_sensor", current): entity_selector(
                ("binary_sensor", "input_boolean")
            ),
            vol.Required(
                "asymmetry_mode", default=current.get("asymmetry_mode", "survey_default")
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        {"value": "survey_default", "label": "Use survey default"},
                        {"value": "enabled", "label": "Enabled"},
                        {"value": "disabled", "label": "Disabled"},
                    ]
                )
            ),
            vol.Required(
                "time_window_enabled", default=current.get("time_window_enabled", False)
            ): bool,
            vol.Required(
                "time_window_start", default=current.get("time_window_start", "06:30:00")
            ): str,
            vol.Required(
                "time_window_end", default=current.get("time_window_end", "22:30:00")
            ): str,
        }
        for key, default, low, high in (
            ("zone_setpoint_min", ROOM_DEFAULTS["zone_setpoint_min"], 5, 34.9),
            ("zone_setpoint_max", ROOM_DEFAULTS["zone_setpoint_max"], 5.1, 35),
            ("manual_hold_minutes", ROOM_DEFAULTS["manual_hold_minutes"], 0, 1440),
            (
                "preheat_release_minutes",
                ROOM_DEFAULTS["preheat_release_minutes"],
                0,
                360,
            ),
            ("override_duration", ROOM_DEFAULTS["override_duration"], 0, 1440),
            ("window_open_delay", ROOM_DEFAULTS["window_open_delay"], 0, 120),
            ("window_delay", ROOM_DEFAULTS["window_delay"], 0, 240),
            ("window_setpoint", ROOM_DEFAULTS["window_setpoint"], 5, 25),
            ("unoccupied_duration", ROOM_DEFAULTS["unoccupied_duration"], 0, 1440),
            (
                "weekday_morning_offset",
                ROOM_DEFAULTS["weekday_morning_offset"],
                -5,
                5,
            ),
            (
                "weekday_afternoon_offset",
                ROOM_DEFAULTS["weekday_afternoon_offset"],
                -5,
                5,
            ),
            (
                "weekday_evening_offset",
                ROOM_DEFAULTS["weekday_evening_offset"],
                -5,
                5,
            ),
            (
                "weekend_morning_offset",
                ROOM_DEFAULTS["weekend_morning_offset"],
                -5,
                5,
            ),
            (
                "weekend_afternoon_offset",
                ROOM_DEFAULTS["weekend_afternoon_offset"],
                -5,
                5,
            ),
            (
                "weekend_evening_offset",
                ROOM_DEFAULTS["weekend_evening_offset"],
                -5,
                5,
            ),
        ):
            marker, validator = number_field(key, current, default, low, high)
            fields[marker] = validator
        return self.async_show_form(
            step_id="control_rooms",
            description_placeholders={
                "room": room["name"],
                "observer_source": room.get("air_sensor")
                or f"{room['climate']} current_temperature attribute",
            },
            errors=errors,
            data_schema=vol.Schema(fields),
        )

    async def finish_control_edit(self):
        old = self.current.get("control")
        runtime = getattr(self.config_entry, "runtime_data", None)
        controls = getattr(runtime, "controls", None)
        lock = controls.lock if controls is not None else None

        async def save():
            self.hydrate_control_state()
            if self.control_active():
                return self.async_abort(reason="control_active")
            settings = (
                controls.settings
                if controls is not None
                else ControlStore(self.hass, self.config_entry.entry_id + ".settings")
            )
            try:
                if not settings.ready:
                    await settings.async_load()
                persisted = settings.get("modes", {})
                if any(mode in ("active", "auto") for mode in persisted.values()):
                    return self.async_abort(reason="control_active")
                settings.set("modes", {})
                settings.set("global_enabled", self.control_pending.get("global_enabled", True))
                flags = dict(settings.get("flags", {}))
                flags["boiler:enabled"] = self.control_pending["boiler"].get("enabled", True)
                for room_id, spec in self.control_pending["rooms"].items():
                    flags[f"{room_id}:enabled"] = spec.get("enabled", True)
                    flags[f"{room_id}:occupancy_enabled"] = spec.get("occupancy_enabled", True)
                settings.set("flags", flags)
                if actuator_changed(old, self.control_pending):
                    settings.set("ownership", "unclaimed")
                await settings.async_save()
            except Exception:
                return self.async_abort(reason="control_state_save_failed")
            return self.async_create_entry(
                title=NAME, data={**self.current, "control": self.control_pending}
            )

        if lock is None:
            return await save()
        async with lock:
            return await save()

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
