"""Explicit journal and structured evidence actions; never actuator commands."""

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .advisor.evidence import TASKS
from .analytics.const import MAX_ADJUSTMENTS
from .const import DOMAIN


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
