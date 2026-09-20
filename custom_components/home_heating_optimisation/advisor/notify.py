"""Opt-in advisor notifications: identifiers and counts by default, never evidence."""

import asyncio

from homeassistant.components import persistent_notification

from .reports import TASK_TITLES, latest_attributes

EVENTS = ("report_ready", "report_failed", "provider_failed")
FAILURE_EVENTS = {
    "timeout": "report_failed",
    "invalid_response": "report_failed",
    "provider_failed": "provider_failed",
}
RETRIEVE = "Retrieve it with action home_heating_optimisation.get_advisor_report_summary"
DELIVERY_TIMEOUT = 30


def report_message(report, include_summary=False):
    attributes = latest_attributes(report)
    body = report.get("report", {})
    title = f"Heating Advisor: {TASK_TITLES.get(report.get('task'), report.get('task'))} ready"
    lines = [
        f"Created {report.get('created_at')} using profile {attributes['profile_name']}.",
        f"Conclusion: {body.get('conclusion')}; {attributes['finding_count']} finding(s),"
        f" {attributes['limitation_count']} limitation(s).",
    ]
    if include_summary and body.get("findings"):
        lines.append("Findings:")
        lines.extend(f"- {f.get('title')}" for f in body["findings"])
    lines.append(f"Report ID: {report.get('id')}. {RETRIEVE} (report_id optional).")
    lines.append("Advisory only; heating settings are unchanged.")
    return title, "\n".join(lines)


def failure_message(task, status, error_type=None):
    title = f"Heating Advisor: {TASK_TITLES.get(task, task)} failed"
    detail = f"Status: {status}" + (f" ({error_type})" if error_type else "") + "."
    return title, "\n".join(
        [
            detail,
            "No automatic retry will be made; heating observation and control continue.",
            "Check the Advisor status sensor and provider profile before running again.",
        ]
    )


async def deliver(hass, targets, title, message, notification_id):
    """Send to configured notify services, or a persistent notification when none."""
    if not targets:
        persistent_notification.async_create(
            hass, message, title=title, notification_id=notification_id
        )
        return
    async with asyncio.timeout(DELIVERY_TIMEOUT):
        for target in targets:
            domain, _, service = str(target).partition(".")
            if domain != "notify" or not service:
                raise ValueError(f"unsupported notify target {target!r}")
            await hass.services.async_call(
                domain, service, {"title": title, "message": message}, blocking=True
            )
