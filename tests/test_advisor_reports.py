"""Advisor report reader, latest-report sensor and opt-in notifications."""

from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_mock_service

from custom_components.home_heating_optimisation.advisor.coordinator import Advisor
from custom_components.home_heating_optimisation.advisor.evidence import encode
from custom_components.home_heating_optimisation.advisor.reports import summarise
from custom_components.home_heating_optimisation.diagnostics import (
    async_get_config_entry_diagnostics,
)
from tests.test_advisor import MODULE, VALID, advisor
from tests.test_integration import setup

NOTIFY = "custom_components.home_heating_optimisation.advisor.notify"
SENSOR = "sensor.home_heating_optimisation_advisor_latest_report"
PRIVATE = ("The source quality limits interpretation.", "Observe the source over a heating cycle.")


async def run(a, task="investigation", response=VALID, question=""):
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=response)):
        return await a.run(task, question)


async def test_summary_renders_latest_and_specific_reports(hass, config, sources, freezer):
    a = await advisor(hass, config)
    with pytest.raises(ServiceValidationError, match="No advisor report"):
        a.report_summary()
    first = await run(a, question="Is the study reporting?")
    freezer.tick(timedelta(minutes=2))
    second = await run(a, "weekly_review")
    latest = a.report_summary()
    assert latest["report_id"] == second["id"]
    assert latest["task"] == "weekly_review"
    assert latest["created_at"] == second["created_at"]
    assert latest["profile_name"] == "Heating"
    assert latest["profile_model"] == "test-model"
    assert latest["prompt_version"] == second["prompt_version"]
    assert latest["evidence_hash"] == second["evidence_hash"]
    assert latest["evidence_references"] == ["quality.history"]
    assert latest["coverage"]["rooms"]["Study"]["coverage_percent"] is not None
    assert latest["limitations"] == ["No metered energy evidence."]
    assert latest["follow_up_actions"] == ["Observe the source over a heating cycle."]
    assert latest["findings"][0]["title"] == "Limited evidence"
    text = latest["text"]
    for fragment in (
        "# Heating Advisor: Weekly review",
        second["id"],
        "## Coverage",
        "Study: coverage",
        "### 1. Limited evidence (observation)",
        "Evidence: quality.history",
        "## Limitations and uncertainty",
        "- No metered energy evidence.",
        "## Follow-up actions",
        "1. Observe the source over a heating cycle.",
    ):
        assert fragment in text
    # Rendering describes evidence by reference; it never dumps evidence values.
    assert encode(second["evidence"]["facts"]["room.1.identity"]) not in text
    specific = a.report_summary(first["id"])
    assert specific["report_id"] == first["id"]
    assert specific["question"] == "Is the study reporting?"
    assert "Question: Is the study reporting?" in specific["text"]
    with pytest.raises(ServiceValidationError, match="Unknown or deleted"):
        a.report_summary("missing")
    response = await hass.services.async_call(
        "home_heating_optimisation",
        "get_advisor_report_summary",
        {},
        blocking=True,
        return_response=True,
    )
    assert response["report_id"] == second["id"]
    assert response["text"] == text


def test_summary_tolerates_reports_without_findings():
    report = {
        "id": "r1",
        "created_at": "2026-09-20T09:00:00+00:00",
        "task": "daily_summary",
        "profile": {"name": "P"},
        "prompt_version": 2,
        "evidence_hash": "abc",
        "evidence": {"facts": {}, "omitted": ["raw_history"]},
        "report": {
            "summary": "Nothing to add.",
            "conclusion": "no_change",
            "findings": [],
            "limitations": [],
        },
    }
    summary = summarise(report)
    assert summary["follow_up_actions"] == []
    assert "No findings were reported." in summary["text"]
    assert "No follow-up actions were proposed." in summary["text"]
    assert "Omitted from evidence: raw_history" in summary["text"]


async def test_latest_sensor_and_diagnostics_expose_identifiers_only(hass, config, sources):
    a = await advisor(hass, config)
    state = hass.states.get(SENSOR)
    assert state.state == "unknown"
    assert state.attributes.get("report_id") is None
    report = await run(a)
    await hass.async_block_till_done()
    state = hass.states.get(SENSOR)
    assert dt_util.parse_datetime(state.state) == dt_util.parse_datetime(
        report["created_at"]
    ).replace(microsecond=0)
    attributes = {
        k: v for k, v in state.attributes.items() if k not in ("device_class", "friendly_name")
    }
    assert attributes == {
        "report_id": report["id"],
        "task": "investigation",
        "profile_name": "Heating",
        "finding_count": 1,
        "limitation_count": 1,
        "headline": "Limited evidence",
    }
    diagnostics = await async_get_config_entry_diagnostics(hass, a.entry)
    for blob in (encode(attributes), encode(diagnostics), encode(a.quality())):
        for text in PRIVATE:
            assert text not in blob
        assert "quality.history" not in blob
        assert "secret-never-export" not in blob
    assert diagnostics["advisor"]["notify_status"] == "disabled"


async def test_headline_is_bounded():
    from custom_components.home_heating_optimisation.advisor.reports import headline

    long = deepcopy(VALID)
    long["findings"][0]["title"] = "x" * 160
    assert len(headline({"report": long})) == 120
    assert headline({"report": {"findings": []}}) is None


async def notifying(hass, config, **options):
    a = await advisor(hass, config)
    a.config.update(
        {"notify_enabled": True, "notify_targets": [], "notify_on": ["report_ready"], **options}
    )
    a.notify_status = "enabled"
    return a


async def test_persistent_fallback_and_once_per_report(hass, config, sources):
    a = await notifying(hass, config)
    with patch(NOTIFY + ".persistent_notification.async_create") as create:
        report = await run(a)
        create.assert_called_once()
        message = create.call_args.args[1]
        assert report["id"] in message
        assert "get_advisor_report_summary" in message
        assert "1 finding(s)" in message
        for text in (*PRIVATE, "Limited evidence", "quality.history"):
            assert text not in message
        assert a.notify_status == "sent"
        assert a.data["notify"]["last_report_id"] == report["id"]
        await a.notify_report(report)
        assert create.call_count == 1
        assert a.notify_status == "skipped_duplicate"
        restored = Advisor(hass, a.entry, a.heating)
        restored.config = a.config
        await restored.initialise()
        assert restored.data["notify"]["last_report_id"] == report["id"]
        await restored.notify_report(report)
        assert create.call_count == 1


async def test_notify_services_receive_titles_only_when_opted_in(hass, config, sources, freezer):
    calls = async_mock_service(hass, "notify", "household")
    a = await notifying(hass, config, notify_targets=["notify.household"])
    report = await run(a)
    assert len(calls) == 1
    assert calls[0].data["title"].startswith("Heating Advisor: Investigation ready")
    assert report["id"] in calls[0].data["message"]
    assert "Limited evidence" not in calls[0].data["message"]
    a.config["notify_include_summary"] = True
    freezer.tick(timedelta(minutes=2))
    await run(a, "daily_summary")
    assert len(calls) == 2
    assert "- Limited evidence" in calls[1].data["message"]
    for text in PRIVATE:
        assert text not in calls[1].data["message"]


async def test_notifications_disabled_by_default_and_event_filter(hass, config, sources):
    a = await advisor(hass, config)
    with patch(NOTIFY + ".persistent_notification.async_create") as create:
        await run(a)
        create.assert_not_called()
    a = await notifying(hass, config, notify_on=["report_failed"])
    a.data["attempts"] = []
    with patch(NOTIFY + ".persistent_notification.async_create") as create:
        await run(a)
        create.assert_not_called()
        assert a.data["notify"]["last_report_id"] is None


async def test_failure_notified_once_per_task_per_day_across_restart(
    hass, config, sources, freezer
):
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-09-20T10:00:00+00:00")
    a = await notifying(hass, config, notify_on=["provider_failed", "report_failed"])
    a.config["max_calls_per_day"] = 12
    with (
        patch(NOTIFY + ".persistent_notification.async_create") as create,
        patch(MODULE + ".generate", side_effect=RuntimeError("provider secret")) as generate,
    ):
        with pytest.raises(HomeAssistantError):
            await a.run("weekly_review")
        create.assert_called_once()
        assert "provider secret" not in create.call_args.args[1]
        assert "No automatic retry" in create.call_args.args[1]
        freezer.tick(timedelta(minutes=2))
        with pytest.raises(HomeAssistantError):
            await a.run("weekly_review")
        assert create.call_count == 1
        assert a.notify_status == "skipped_duplicate"
        restored = Advisor(hass, a.entry, a.heating)
        restored.config = a.config
        await restored.initialise()
        assert restored.data["notify"]["failures"]["weekly_review"]["day"] == "2026-09-20"
        freezer.tick(timedelta(minutes=2))
        with pytest.raises(HomeAssistantError):
            await restored.run("weekly_review")
        assert create.call_count == 1
        # A different task is a separate failure category for the day.
        freezer.tick(timedelta(minutes=2))
        with pytest.raises(HomeAssistantError):
            await restored.run("investigation")
        assert create.call_count == 2
        # Timeouts share the once-per-day rule with provider failures for that task.
        generate.side_effect = TimeoutError()
        freezer.tick(timedelta(minutes=2))
        with pytest.raises(HomeAssistantError):
            await restored.run("weekly_review")
        assert create.call_count == 2
        freezer.move_to("2026-09-21T10:00:00+00:00")
        restored.data["attempts"] = []
        with pytest.raises(HomeAssistantError):
            await restored.run("weekly_review")
        assert create.call_count == 3
        assert generate.await_count == 6


async def test_delivery_failure_never_breaks_run_or_heating(hass, config, sources):
    async def broken(call):
        raise HomeAssistantError("notify service unavailable")

    hass.services.async_register("notify", "broken", broken)
    a = await notifying(hass, config, notify_targets=["notify.broken"])
    with patch(MODULE + ".LOGGER.warning") as warning:
        report = await run(a)
    assert "delivery failed" in warning.call_args.args[0]
    assert report["id"] == a.data["reports"][-1]["id"]
    assert a.status == "ready"
    assert a.notify_status == "delivery_failed"
    assert a.data["notify"]["last_report_id"] == report["id"]
    assert a.heating.snapshot().rooms[0].air.value == 18
    # Deduplication still holds after a failed delivery: no second attempt.
    calls = async_mock_service(hass, "notify", "household")
    a.config["notify_targets"] = ["notify.household"]
    await a.notify_report(report)
    assert calls == []


async def test_notify_helper_exception_is_isolated(hass, config, sources):
    a = await notifying(hass, config)
    with patch(MODULE + ".deliver", side_effect=RuntimeError("boom")):
        report = await run(a)
    assert report["id"] == a.data["reports"][-1]["id"]
    assert a.notify_status == "delivery_failed"
    with patch(MODULE + ".report_message", side_effect=KeyError("bad")):
        a.data["notify"]["last_report_id"] = None
        await a.notify_report(report)
    assert a.notify_status == "error"


async def test_report_is_journaled_defensively(hass, config, sources):
    a = await advisor(hass, config)
    recorded = []
    a.heating.journal = SimpleNamespace(record=lambda kind, **kw: recorded.append((kind, kw)))
    report = await run(a)
    assert recorded == [
        (
            "advisor_report",
            {
                "scope": "system",
                "origin": "advisor",
                "data": {
                    "report_id": report["id"],
                    "task": "investigation",
                    "profile_name": "Heating",
                    "finding_count": 1,
                },
            },
        )
    ]
    a.heating.journal = SimpleNamespace(record=lambda *a, **k: 1 / 0)
    a.data["attempts"] = []
    assert (await run(a))["task"] == "investigation"


async def test_options_flow_saves_notification_settings(hass, config, sources):
    from tests.test_advisor import profile

    async_mock_service(hass, "notify", "household")
    entity = profile(hass)
    entry = await setup(hass, {**config, "analytics_enabled": True})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "advisor"}
    )
    assert flow["type"] == "form"
    base = {"enabled": True, "investigation": entity, "notify_enabled": True}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**base, "notify_on": []}
    )
    assert flow["errors"] == {"base": "missing_notify_event"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {**base, "notify_on": ["report_ready"], "notify_targets": ["light.x"]}
    )
    assert flow["errors"] == {"base": "invalid_notify_target"}
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {
            **base,
            "notify_on": ["report_ready", "provider_failed"],
            "notify_targets": ["notify.household"],
            "notify_include_summary": True,
        },
    )
    assert flow["type"] == "create_entry"
    saved = flow["data"]["advisor"]
    assert saved["notify_targets"] == ["notify.household"]
    assert saved["notify_on"] == ["report_ready", "provider_failed"]
    assert saved["notify_include_summary"] is True
    await hass.async_block_till_done()
    assert entry.runtime_data.advisor.notify_status == "enabled"
