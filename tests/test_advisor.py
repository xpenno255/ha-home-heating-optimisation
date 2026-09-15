"""Advisor isolation, evidence validation, profiles, budgets and lifecycle."""

import asyncio
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.advisor.coordinator import Advisor
from custom_components.home_heating_optimisation.advisor.evidence import (
    FIELD_DESCRIPTIONS,
    INSTRUCTIONS,
    ReportValidationError,
    build_evidence,
    encode,
    validate_response,
)
from custom_components.home_heating_optimisation.advisor.profiles import (
    profile_info,
    safe_extra_body,
)
from custom_components.home_heating_optimisation.analytics.store import source_signature
from tests.test_integration import setup

MODULE = "custom_components.home_heating_optimisation.advisor.coordinator"
VALID = {
    "summary": "Check source reporting before changing settings.",
    "conclusion": "insufficient_evidence",
    "findings": [
        {
            "title": "Limited evidence",
            "detail": "The source quality limits interpretation.",
            "kind": "observation",
            "evidence_ids": ["quality.history"],
            "next_check": "Observe the source over a heating cycle.",
        }
    ],
    "limitations": ["No metered energy evidence."],
}


def profile(hass, tools="[]", domain="extended_openai_conversation", **extra):
    entry = MockConfigEntry(
        domain=domain,
        data={"api_key": "secret-never-export"},
        subentries_data=[
            {
                "subentry_id": "heating",
                "subentry_type": "ai_task_data",
                "title": "Heating",
                "unique_id": None,
                "data": {
                    "chat_model": "test-model",
                    "functions": tools,
                    "llm_hass_api": [],
                    **extra,
                },
            }
        ],
    )
    entry.add_to_hass(hass)
    registered = er.async_get(hass).async_get_or_create(
        "ai_task", domain, entry.entry_id, config_entry=entry, config_subentry_id="heating"
    )
    hass.states.async_set(registered.entity_id, "unknown")
    return registered.entity_id


async def advisor(hass, config):
    entity = profile(hass)
    config = {
        **config,
        "analytics_enabled": True,
        "advisor": {
            "enabled": True,
            "investigation": entity,
            "daily_summary": entity,
            "weekly_review": entity,
            "max_calls_per_day": 4,
        },
    }
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        return_value=([], False, "complete"),
    ):
        entry = await setup(hass, config)
    return entry.runtime_data.advisor


async def test_profiles_require_explicit_empty_functions_and_hide_credentials(hass):
    assert profile_info(hass, profile(hass))["status"] == "ready"
    assert profile_info(hass, profile(hass, tools=""))["status"] == "tools_enabled"
    assert profile_info(hass, profile(hass, llm_hass_api=["assist"]))["status"] == "tools_enabled"
    info = profile_info(hass, profile(hass, domain="anthropic", thinking_effort="high"))
    assert info["effort"] == "high"
    assert "secret-never-export" not in encode(info)
    assert (
        profile_info(hass, profile(hass, domain="anthropic", web_search=True))["status"]
        == "tools_enabled"
    )
    assert not safe_extra_body('{"tools": []}')
    assert not safe_extra_body("{{ states }}")
    assert safe_extra_body('{"chat_template_kwargs": {"enable_thinking": false}}')


async def test_evidence_excludes_notes_and_preserves_unknown_metrics(hass, config, sources):
    a = await advisor(hass, config)
    r = a.heating.analytics.report()
    r["adjustments"] = [{"time": 1, "kind": "other", "note": "private household routine"}]
    r["recent_decision_context"] = [{"secret": "raw context"}]
    e = build_evidence(r, a.heating.house_report(), {}, "weekly_review")
    assert "private household routine" not in encode(e)
    assert "raw context" not in encode(e)
    assert e["facts"]["room.1.within_band"] is None
    assert validate_response(VALID, e) == VALID
    for mutation in (
        lambda v: v["findings"][0].update(evidence_ids=["invented"]),
        lambda v: v.update(service="climate.set_temperature"),
        lambda v: v.update(summary="x" * 1001),
    ):
        bad = deepcopy(VALID)
        mutation(bad)
        with pytest.raises((ValueError, vol.Invalid)):
            validate_response(bad, e)


async def test_run_retains_evidence_and_call_budget_without_device_calls(hass, config, sources):
    a = await advisor(hass, config)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=VALID)) as generate:
        report = await a.run("investigation", "What should I check?")
        generate.assert_awaited_once()
        assert generate.call_args.kwargs["entity_id"] == a.config["investigation"]
        assert report["evidence"]["question"] == "What should I check?"
        assert len(a.data["attempts"]) == 1
        assert a.report_list(report["id"]) == report
        assert a.status == "ready"
        with pytest.raises(HomeAssistantError, match="one minute"):
            await a.run("investigation")
    restored = Advisor(hass, a.entry, a.heating)
    await restored.initialise()
    assert restored.report_list(report["id"]) == report
    assert restored.data["attempts"] == a.data["attempts"]
    assert "summary" not in a.quality()


@pytest.mark.parametrize(
    "failure,status",
    [(TimeoutError(), "timeout"), (RuntimeError("provider secret"), "provider_failed")],
)
async def test_provider_failures_consume_attempt_and_do_not_stop_observer(
    hass, config, sources, failure, status
):
    a = await advisor(hass, config)
    with patch(MODULE + ".generate", side_effect=failure):
        with pytest.raises(HomeAssistantError) as error:
            await a.run("weekly_review")
    assert "provider secret" not in str(error.value)
    assert a.status == status
    assert len(a.data["attempts"]) == 1
    assert not a.data["reports"]
    assert a.heating.snapshot().rooms[0].air.value == 18


async def test_invalid_response_rejected_and_no_fallback(hass, config, sources):
    a = await advisor(hass, config)
    bad = deepcopy(VALID)
    bad["findings"][0]["evidence_ids"] = ["invented"]
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=bad)) as generate:
        with pytest.raises(HomeAssistantError):
            await a.run("weekly_review")
        generate.assert_awaited_once()
    assert a.status == "invalid_response"
    assert not a.data["reports"]


async def test_budget_persists_failure_and_schedule_does_not_repeat(hass, config, sources, freezer):
    a = await advisor(hass, config)
    a.config.update(schedule_daily_summary=True, schedule_hour=9, max_calls_per_day=1)
    await hass.config.async_set_time_zone("UTC")
    freezer.move_to("2026-09-16T09:05:00+00:00")
    with patch(MODULE + ".generate", side_effect=RuntimeError) as generate:
        a.schedule(dt_util.utcnow())
        await hass.async_block_till_done(wait_background_tasks=True)
        a.schedule(dt_util.utcnow() + timedelta(minutes=2))
        await hass.async_block_till_done(wait_background_tasks=True)
        generate.assert_awaited_once()
    freezer.tick(timedelta(minutes=2))
    with pytest.raises(HomeAssistantError, match="24-hour"):
        await a.run("investigation")


async def test_unload_cancels_inflight_call_and_blocks_overlap(hass, config, sources):
    a = await advisor(hass, config)
    started = asyncio.Event()

    async def slow(*args, **kwargs):
        started.set()
        await asyncio.Event().wait()

    with patch(MODULE + ".generate", side_effect=slow):
        running = asyncio.create_task(a.run("investigation"))
        await started.wait()
        with pytest.raises(HomeAssistantError, match="already running"):
            await a.run("weekly_review")
        await a.stop()
        assert running.cancelled()
    assert not a.data["reports"]


async def test_storage_failure_prevents_provider_call(hass, config, sources):
    a = await advisor(hass, config)
    with (
        patch.object(a.backend, "async_save", side_effect=OSError),
        patch(MODULE + ".generate") as generate,
    ):
        with pytest.raises(HomeAssistantError):
            await a.run("investigation")
        generate.assert_not_called()
    assert a.status == "save_failed"


async def test_options_configure_advisor_without_changing_room_mappings(hass, config, sources):
    entity = profile(hass)
    entry = await setup(hass, {**config, "analytics_enabled": True})
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert flow["type"] == "menu"
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "advisor"}
    )
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"enabled": True, "investigation": entity}
    )
    assert flow["type"] == "create_entry"
    assert flow["data"]["rooms"] == config["rooms"]
    assert source_signature(config, "°C") == source_signature(
        {**config, "advisor": {"enabled": True}}, "°C"
    )
    await hass.async_block_till_done()


async def test_disabled_advisor_makes_no_calls(hass, config, sources):
    entry = await setup(hass, config)
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(HomeAssistantError, match="Enable"):
            await entry.runtime_data.advisor.run("investigation")
        generate.assert_not_called()
    assert entry.runtime_data.advisor.status == "disabled"


def test_generation_schema_constrains_evidence_ids():
    from custom_components.home_heating_optimisation.advisor.evidence import report_schema

    schema = report_schema(vol.In(("quality.history",)))
    assert schema(VALID) == VALID
    bad = deepcopy(VALID)
    bad["findings"][0]["evidence_ids"] = ["quality.history.invented"]
    with pytest.raises(vol.Invalid):
        schema(bad)


def test_provider_schema_describes_bounds_without_unsupported_keywords():
    from probatio import to_openapi

    from custom_components.home_heating_optimisation.advisor.evidence import report_schema

    schema = to_openapi(report_schema(vol.In(("quality.history",))))
    finding = schema["properties"]["findings"]["items"]
    assert finding["properties"]["evidence_ids"]["description"].startswith("1-12 exact fact IDs")
    assert finding["properties"]["next_check"]["description"].startswith("30-500 characters")
    assert set(finding["required"]) == set(VALID["findings"][0])
    assert finding["additionalProperties"] is False
    assert finding["properties"]["evidence_ids"]["items"]["enum"] == ["quality.history"]
    for keyword in ("minLength", "maxLength", "maxItems", "uniqueItems"):
        assert keyword not in encode(schema)
    for description in FIELD_DESCRIPTIONS.values():
        assert description in INSTRUCTIONS


@pytest.mark.parametrize("count,accepted", [(0, False), (1, True), (12, True), (13, False)])
def test_reference_count_boundaries(count, accepted):
    refs = [f"fact.{n}" for n in range(count)]
    evidence = {"facts": dict.fromkeys(refs)}
    response = deepcopy(VALID)
    response["findings"][0]["evidence_ids"] = refs
    if accepted:
        assert validate_response(response, evidence) == response
    else:
        with pytest.raises(ReportValidationError, match="invalid_evidence_reference"):
            validate_response(response, evidence)


@pytest.mark.parametrize(
    "field,minimum,maximum,error",
    [
        ("summary", 1, 1000, "invalid_summary"),
        ("title", 1, 160, "invalid_finding"),
        ("detail", 1, 1200, "invalid_finding"),
        ("next_check", 30, 500, "invalid_finding"),
        ("limitation", 1, 500, "invalid_limitation"),
    ],
)
def test_text_boundaries(field, minimum, maximum, error):
    for text, accepted in (
        ("", False),
        (" " * minimum, False),
        ("x" * (minimum - 1), False),
        ("x" * minimum, True),
        ("x" * maximum, True),
        ("x" * (maximum + 1), False),
        (" " + "x" * (minimum - 1) + " ", False),
    ):
        response = deepcopy(VALID)
        if field == "summary":
            response[field] = text
        elif field == "limitation":
            response["limitations"] = [text]
        else:
            response["findings"][0][field] = text
        if accepted:
            assert validate_response(response, {"facts": {"quality.history": {}}}) == response
        else:
            with pytest.raises(ReportValidationError, match=error):
                validate_response(response, {"facts": {"quality.history": {}}})


@pytest.mark.parametrize("field,maximum", [("findings", 6), ("limitations", 8)])
def test_report_list_boundaries(field, maximum):
    for count in (0, maximum, maximum + 1):
        response = deepcopy(VALID)
        response[field] *= count
        if count <= maximum:
            assert validate_response(response, {"facts": {"quality.history": {}}}) == response
        else:
            with pytest.raises(ReportValidationError, match="too_many_findings"):
                validate_response(response, {"facts": {"quality.history": {}}})


async def test_rejection_category_visible_without_report_text(hass, config, sources):
    a = await advisor(hass, config)
    response = deepcopy(VALID)
    response["findings"][0].update(detail="private response text", next_check="")
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=response)) as generate:
        with pytest.raises(HomeAssistantError, match="invalid_finding") as error:
            await a.run("investigation")
        generate.assert_awaited_once()
    assert "private response text" not in str(error.value)
    assert a.quality()["error_type"] == "invalid_finding"
    assert not a.data["reports"]
    assert len(a.data["attempts"]) == 1


async def test_corrupt_report_store_is_preserved_and_blocks_calls(hass, config, sources):
    a = await advisor(hass, config)
    bad = {"schema": 1, "reports": [{"id": "old"}], "attempts": [], "scheduled": {}}
    with patch.object(a.backend, "async_load", return_value=bad):
        await a.initialise()
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(HomeAssistantError, match="storage"):
            await a.run("investigation")
        generate.assert_not_called()
    assert a.status == "storage_read_only"
