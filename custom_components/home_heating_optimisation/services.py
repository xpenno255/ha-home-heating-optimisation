"""Explicit journal and structured evidence actions; never actuator commands."""

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .advisor.evidence import MAX_QUESTION_CHARS, TASKS
from .analytics.const import MAX_ADJUSTMENTS
from .const import DOMAIN
from .journal.const import KINDS, QUERY_DEFAULT_HOURS, QUERY_MAX_EVENTS, QUERY_MAX_HOURS


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
        journal = getattr(heating(), "journal", None)
        if journal is not None:
            # Mirror into the event journal; the note text stays under a private key.
            journal.record(
                "adjustment_note",
                room_id=room,
                scope=room or "system",
                origin="user",
                data={
                    "adjustment_kind": call.data["kind"],
                    "private_note": note,
                    "outcome": "reported",
                },
            )
        coordinator.capture()
        await coordinator.refresh()
        if coordinator.store.status != "ready":
            raise HomeAssistantError("Adjustment is held in memory but could not be saved")

    async def get_journal(call):
        journal = getattr(heating(), "journal", None)
        if journal is None or not journal.enabled:
            return {"events": [], "count": 0, "truncated": False, "status": "disabled"}
        kinds = call.data.get("kinds") or None
        room = call.data.get("room_id")
        since = dt_util.utcnow().timestamp() - call.data["hours"] * 3600
        events = journal.export(
            include_private=call.data["include_private"],
            kinds=kinds,
            room_id=room,
            since=since,
        )
        truncated = len(events) > QUERY_MAX_EVENTS
        if truncated:
            events = events[-QUERY_MAX_EVENTS:]
        return {
            "events": events,
            "count": len(events),
            "truncated": truncated,
            "status": journal.status,
        }

    async def report(call):
        coordinator = analytics()
        await coordinator.refresh()
        return {**coordinator.report(), "house_model": heating().house_report()}

    async def energy_report(call):
        energy = heating().energy
        if energy is None:
            raise ServiceValidationError("Configure at least one energy meter first")
        return energy.report(call.data["days"])

    hass.services.async_register(
        DOMAIN,
        "get_energy_report",
        energy_report,
        schema=vol.Schema(
            {vol.Optional("days", default=7): vol.All(vol.Coerce(int), vol.Range(min=1, max=90))}
        ),
        supports_response=SupportsResponse.ONLY,
    )

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
    hass.services.async_register(
        DOMAIN,
        "get_journal",
        get_journal,
        schema=vol.Schema(
            {
                vol.Optional("kinds"): vol.All(cv.ensure_list, [vol.In(KINDS)]),
                vol.Optional("room_id"): cv.string,
                vol.Optional("hours", default=QUERY_DEFAULT_HOURS): vol.All(
                    vol.Coerce(float), vol.Range(min=0, max=QUERY_MAX_HOURS)
                ),
                vol.Optional("include_private", default=False): cv.boolean,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )

    async def run_review(call):
        return await heating().advisor.run(call.data["task"], call.data.get("question", ""))

    async def advisor_reports(call):
        return heating().advisor.report_list(call.data.get("report_id"))

    async def advisor_report_summary(call):
        return heating().advisor.report_summary(call.data.get("report_id"))

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
    hass.services.async_register(
        DOMAIN,
        "get_advisor_report_summary",
        advisor_report_summary,
        schema=vol.Schema({vol.Optional("report_id"): cv.string}),
        supports_response=SupportsResponse.ONLY,
    )

    async def advisor_followup(call):
        return await heating().advisor.followup(
            call.data["report_id"], call.data["question"], call.data.get("conversation_id")
        )

    async def advisor_followup_conversation(call):
        return heating().advisor.followup_conversation(call.data["conversation_id"])

    hass.services.async_register(
        DOMAIN,
        "ask_advisor_followup",
        advisor_followup,
        schema=vol.Schema(
            {
                vol.Required("report_id"): cv.string,
                vol.Required("question"): vol.All(
                    cv.string, vol.Length(min=1, max=MAX_QUESTION_CHARS)
                ),
                vol.Optional("conversation_id"): cv.string,
            }
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "get_advisor_followup",
        advisor_followup_conversation,
        schema=vol.Schema({vol.Required("conversation_id"): cv.string}),
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

    from .analytics import legacy_import

    MAPPING = vol.Schema({cv.string: vol.Any(None, cv.string)})

    async def history_preview(call):
        coordinator = analytics()
        return await legacy_import.preview(hass, coordinator, call.data.get("mapping"))

    async def history_import(call):
        coordinator = analytics()
        return await legacy_import.execute(
            hass, heating(), coordinator, call.data.get("mapping"), call.data.get("confirm")
        )

    async def history_retire(call):
        coordinator = analytics()
        return await legacy_import.retire_legacy_store(
            hass, heating(), coordinator, call.data.get("confirm")
        )

    hass.services.async_register(
        DOMAIN,
        "preview_history_import",
        history_preview,
        schema=vol.Schema({vol.Optional("mapping"): MAPPING}),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "import_history",
        history_import,
        schema=vol.Schema(
            {vol.Optional("mapping"): MAPPING, vol.Optional("confirm", default=False): cv.boolean}
        ),
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        "retire_legacy_store",
        history_retire,
        schema=vol.Schema({vol.Optional("confirm", default=False): cv.boolean}),
        supports_response=SupportsResponse.ONLY,
    )

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
