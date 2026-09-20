"""Explicit journal and structured evidence actions; never actuator commands."""

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .advisor.evidence import TASKS
from .advisor.recommendations import DECISIONS, OUTCOMES, STATES
from .analytics.const import MAX_ADJUSTMENTS
from .const import DOMAIN
from .trials.const import MAX_DURATION_HOURS, MIN_DURATION_HOURS
from .trials.const import OUTCOMES as TRIAL_OUTCOMES
from .trials.const import STATES as TRIAL_STATES


@callback
def async_register_services(hass):
    def heating():
        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if entry.state.value == "loaded" and (
                coordinator := getattr(entry, "runtime_data", None)
            ):
                return coordinator
        raise ServiceValidationError("Load the heating integration first")

    def analytics():
        coordinator = heating()
        if coordinator.analytics is None:
            raise ServiceValidationError("Enable historical analytics first")
        return coordinator.analytics

    async def record(call):
        coordinator = analytics()
        note = call.data["note"].strip()
        if not note:
            raise ServiceValidationError("Enter a non-empty adjustment note")
        room = call.data.get("room_id")
        if room and room not in {r["id"] for r in coordinator.config["rooms"]}:
            raise ServiceValidationError("room_id must identify a configured heating room")
        if coordinator.store.status == "storage_read_only":
            raise HomeAssistantError(
                "History storage is read-only; the adjustment cannot be persisted"
            )
        if len(coordinator.store.adjustments) >= MAX_ADJUSTMENTS:
            raise ServiceValidationError(
                "Adjustment journal is full; export and archive it before adding notes"
            )
        coordinator.store.adjustments.append(
            {
                "time": dt_util.utcnow().timestamp(),
                "note": note,
                "kind": call.data["kind"],
                "room_id": room,
            }
        )
        coordinator.capture()
        await coordinator.refresh()
        if coordinator.store.status != "ready":
            raise HomeAssistantError("Adjustment is held in memory but could not be saved")

    async def report(call):
        coordinator = analytics()
        await coordinator.refresh()
        return {**coordinator.report(), "house_model": heating().house_report()}

    async def house_report(call):
        return heating().house_report()

    async def reload_house(call):
        coordinator = heating()
        await coordinator.load_house()
        if coordinator.survey["status"] == "error":
            raise HomeAssistantError(
                f"House survey reload failed: {coordinator.survey['error_code']}"
            )

    hass.services.async_register(
        DOMAIN,
        "get_house_model",
        house_report,
        schema=vol.Schema({}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(DOMAIN, "reload_house_model", reload_house, schema=vol.Schema({}))
    hass.services.async_register(
        DOMAIN,
        "record_adjustment",
        record,
        schema=vol.Schema(
            {
                vol.Required("note"): vol.All(cv.string, vol.Length(min=1, max=500)),
                vol.Optional("kind", default="other"): vol.In(
                    ("lockshield", "boiler_setting", "sensor_change", "other")
                ),
                vol.Optional("room_id"): cv.string,
            }
        ),
    )
    hass.services.async_register(
        DOMAIN, "get_report", report, schema=vol.Schema({}), supports_response=SupportsResponse.ONLY
    )

    async def run_review(call):
        return await heating().advisor.run(call.data["task"], call.data.get("question", ""))

    async def advisor_reports(call):
        return heating().advisor.report_list(call.data.get("report_id"))

    hass.services.async_register(
        DOMAIN,
        "run_review",
        run_review,
        schema=vol.Schema(
            {
                vol.Required("task"): vol.In(TASKS),
                vol.Optional("question", default=""): vol.All(cv.string, vol.Length(max=1000)),
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "get_advisor_reports",
        advisor_reports,
        schema=vol.Schema({vol.Optional("report_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    def recommendations():
        return heating().advisor.recommendations

    async def decide(call):
        return await recommendations().decide(
            call.data["recommendation_id"],
            call.data["decision"],
            note=call.data.get("note"),
            defer_until=call.data.get("defer_until"),
        )

    async def applied(call):
        return await recommendations().mark_applied(
            call.data["recommendation_id"],
            journal_event_id=call.data.get("journal_event_id"),
            intervention_note=call.data.get("intervention_note"),
        )

    async def evaluate(call):
        return await recommendations().evaluate(
            call.data["recommendation_id"],
            call.data["outcome"],
            call.data["window_start"],
            call.data["window_end"],
            note=call.data.get("note"),
        )

    async def list_recommendations(call):
        return recommendations().report_list(
            state=call.data.get("state"),
            report_id=call.data.get("report_id"),
            room_id=call.data.get("room_id"),
            include_private=call.data["include_private"],
        )

    note = vol.All(cv.string, vol.Length(min=1, max=500))
    hass.services.async_register(
        DOMAIN,
        "decide_recommendation",
        decide,
        schema=vol.Schema(
            {
                vol.Required("recommendation_id"): cv.string,
                vol.Required("decision"): vol.In(DECISIONS),
                vol.Optional("note"): note,
                vol.Optional("defer_until"): cv.datetime,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "mark_recommendation_applied",
        applied,
        schema=vol.Schema(
            {
                vol.Required("recommendation_id"): cv.string,
                vol.Optional("journal_event_id"): cv.string,
                vol.Optional("intervention_note"): note,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "evaluate_recommendation",
        evaluate,
        schema=vol.Schema(
            {
                vol.Required("recommendation_id"): cv.string,
                vol.Required("outcome"): vol.In(OUTCOMES),
                vol.Required("window_start"): cv.datetime,
                vol.Required("window_end"): cv.datetime,
                vol.Optional("note"): note,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "get_recommendations",
        list_recommendations,
        schema=vol.Schema(
            {
                vol.Optional("state"): vol.In(STATES),
                vol.Optional("report_id"): cv.string,
                vol.Optional("room_id"): cv.string,
                vol.Optional("include_private", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    def trials():
        coordinator = heating()
        if coordinator.trials is None:
            raise ServiceValidationError("Trials are unavailable until setup completes")
        return coordinator.trials

    def confirmed(call):
        if call.data.get("confirm") is not True:
            raise ServiceValidationError("Set confirm: true to perform this trial action")

    async def propose_trial(call):
        return await trials().propose(
            call.data["scope"],
            call.data["parameter"],
            call.data["target_value"],
            call.data["rationale"],
            call.data["duration_hours"],
            comfort_floor_c=call.data.get("comfort_floor_c"),
            recommendation_id=call.data.get("recommendation_id"),
        )

    async def approve_trial(call):
        confirmed(call)
        return await trials().approve(call.data["trial_id"])

    async def reject_trial(call):
        return await trials().reject(call.data["trial_id"], note=call.data.get("note"))

    async def start_trial(call):
        confirmed(call)
        return await trials().start_trial(call.data["trial_id"])

    async def stop_trial(call):
        return await trials().stop_trial(
            call.data["trial_id"], complete=call.data["complete"], note=call.data.get("note")
        )

    async def evaluate_trial(call):
        return await trials().evaluate(
            call.data["trial_id"], call.data["outcome"], note=call.data.get("note")
        )

    async def get_trials(call):
        return trials().report_list(
            state=call.data.get("state"),
            scope=call.data.get("scope"),
            include_private=call.data["include_private"],
        )

    trial_id = {vol.Required("trial_id"): cv.string}
    hass.services.async_register(
        DOMAIN,
        "propose_trial",
        propose_trial,
        schema=vol.Schema(
            {
                vol.Required("scope"): cv.string,
                vol.Required("parameter"): cv.string,
                vol.Required("target_value"): vol.Coerce(float),
                vol.Required("rationale"): note,
                vol.Required("duration_hours"): vol.All(
                    vol.Coerce(int), vol.Range(min=MIN_DURATION_HOURS, max=MAX_DURATION_HOURS)
                ),
                vol.Optional("comfort_floor_c"): vol.Coerce(float),
                vol.Optional("recommendation_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "approve_trial",
        approve_trial,
        schema=vol.Schema({**trial_id, vol.Required("confirm"): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "reject_trial",
        reject_trial,
        schema=vol.Schema({**trial_id, vol.Optional("note"): note}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "start_trial",
        start_trial,
        schema=vol.Schema({**trial_id, vol.Required("confirm"): cv.boolean}),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "stop_trial",
        stop_trial,
        schema=vol.Schema(
            {
                **trial_id,
                vol.Optional("complete", default=False): cv.boolean,
                vol.Optional("note"): note,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "evaluate_trial",
        evaluate_trial,
        schema=vol.Schema(
            {
                **trial_id,
                vol.Required("outcome"): vol.In(TRIAL_OUTCOMES),
                vol.Optional("note"): note,
            }
        ),
        supports_response=SupportsResponse.OPTIONAL,
    )
    hass.services.async_register(
        DOMAIN,
        "get_trials",
        get_trials,
        schema=vol.Schema(
            {
                vol.Optional("state"): vol.In(TRIAL_STATES),
                vol.Optional("scope"): cv.string,
                vol.Optional("include_private", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    from .control.migration import handover, import_controls, preview, rollback

    async def control_preview(call):
        return preview(hass, heating().config_entry)

    async def control_import(call):
        return await import_controls(hass, heating().config_entry)

    async def control_report(call):
        return heating().controls.report()

    async def control_handover(call):
        return await handover(heating().controls)

    async def control_rollback(call):
        return await rollback(heating().controls)

    for name, handler in (
        ("preview_control_import", control_preview),
        ("import_controls", control_import),
        ("get_control_report", control_report),
        ("handover_controls", control_handover),
        ("rollback_controls", control_rollback),
    ):
        hass.services.async_register(
            DOMAIN, name, handler, schema=vol.Schema({}), supports_response=SupportsResponse.ONLY
        )
