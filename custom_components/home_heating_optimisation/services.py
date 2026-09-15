"""Explicit journal and structured evidence actions; never actuator commands."""

import voluptuous as vol
from homeassistant.core import SupportsResponse, callback
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import config_validation as cv
from homeassistant.util import dt as dt_util

from .analytics.const import MAX_ADJUSTMENTS
from .const import DOMAIN


@callback
def async_register_services(hass):
    def analytics():
        entries = hass.config_entries.async_entries(DOMAIN)
        for entry in entries:
            if (
                entry.state.value == "loaded"
                and (coordinator := getattr(entry, "runtime_data", None))
                and coordinator.analytics
            ):
                return coordinator.analytics
        raise ServiceValidationError(
            "Enable historical analytics on a loaded heating integration first"
        )

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
        return coordinator.report()

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
