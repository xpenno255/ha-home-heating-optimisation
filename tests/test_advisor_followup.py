"""Bounded follow-up conversations grounded in a retained report's saved evidence."""

import json
from copy import deepcopy
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.home_heating_optimisation.advisor.coordinator import Advisor
from custom_components.home_heating_optimisation.advisor.evidence import (
    FOLLOWUP_FIELD_DESCRIPTIONS,
    FOLLOWUP_INSTRUCTIONS,
    MAX_CONVERSATIONS,
    MAX_TURNS_PER_CONVERSATION,
    FollowupValidationError,
    encode,
    validate_followup_response,
)
from custom_components.home_heating_optimisation.const import DOMAIN
from tests.test_advisor import MODULE, VALID, profile
from tests.test_integration import setup

ANSWER = {
    "answer": "The report cites limited source quality; nothing newer is in evidence.",
    "references": ["quality.history"],
    "unsupported_claims": [],
    "missing_data": ["metered energy"],
}


class FakeJournal:
    def __init__(self):
        self.events = []

    def record(self, kind, **kwargs):
        self.events.append({"kind": kind, **kwargs})
        return self.events[-1]


async def advisor(hass, config, followup=True, **options):
    entity = profile(hass)
    settings = {
        "enabled": True,
        "investigation": entity,
        "max_calls_per_day": 12,
        **options,
    }
    if followup:
        settings["followup"] = profile(hass)
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        return_value=([], False, "complete"),
    ):
        entry = await setup(hass, {**config, "analytics_enabled": True, "advisor": settings})
    return entry.runtime_data.advisor


async def report(a, freezer):
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=VALID)):
        created = await a.run("investigation", "What should I check?")
    freezer.tick(timedelta(minutes=2))
    return created


async def test_followup_happy_path_persists_and_lists_ids_only(hass, config, sources, freezer):
    a = await advisor(hass, config)
    a.heating.journal = FakeJournal()
    r = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)) as generate:
        first = await a.followup(r["id"], "  Which room has the weakest evidence?  ")
        instructions = generate.call_args.kwargs["instructions"]
        assert generate.call_args.kwargs["entity_id"] == a.config["followup"]
        assert generate.call_args.kwargs["entity_id"] != a.config["investigation"]
        assert instructions.startswith(FOLLOWUP_INSTRUCTIONS)
        assert "Which room has the weakest evidence?" in instructions
        assert '"original_report"' in instructions and '"newer_observations"' in instructions
        for description in FOLLOWUP_FIELD_DESCRIPTIONS.values():
            assert description in FOLLOWUP_INSTRUCTIONS
        # Provider grammar constrains references to saved fact IDs.
        with pytest.raises(Exception):
            generate.call_args.kwargs["structure"]({**ANSWER, "references": ["invented"]})
    assert first["turn"] == 1 and first["turns_remaining"] == MAX_TURNS_PER_CONVERSATION - 1
    assert first["answer"] == ANSWER["answer"]
    assert first["question"] == "Which room has the weakest evidence?"
    assert a.status == "ready"
    assert len(a.data["attempts"]) == 2
    assert a.heating.journal.events == [
        {
            "kind": "advisor_followup",
            "scope": "system",
            "origin": "service",
            "data": {
                "report_id": r["id"],
                "conversation_id": first["conversation_id"],
                "turn": 1,
                "profile_name": "Heating",
            },
        }
    ]
    freezer.tick(timedelta(minutes=2))
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)) as generate:
        second = await a.followup(r["id"], "And the next check?", first["conversation_id"])
        instructions = generate.call_args.kwargs["instructions"]
        assert "Which room has the weakest evidence?" in instructions
        assert ANSWER["answer"] in instructions
    assert second["conversation_id"] == first["conversation_id"] and second["turn"] == 2

    listing = a.report_list()
    assert listing["reports"][0]["conversations"] == [
        {
            "id": first["conversation_id"],
            "turns": 2,
            "updated_at": a.data["conversations"][0]["updated_at"],
        }
    ]
    assert "Which room" not in encode(listing) and ANSWER["answer"] not in encode(listing)
    assert "Which room" not in encode(a.report_list(r["id"]))
    assert "answer" not in encode(a.quality())
    conversation = a.followup_conversation(first["conversation_id"])
    assert [t["question"] for t in conversation["turns"]] == [
        "Which room has the weakest evidence?",
        "And the next check?",
    ]
    assert conversation["turns"][1]["answer"] == ANSWER["answer"]


async def test_newer_observations_only_contain_allowlisted_quality(hass, config, sources, freezer):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)) as generate:
        await a.followup(r["id"], "Anything changed?")
    payload = generate.call_args.kwargs["instructions"].split("Follow-up JSON:\n", 1)[1]
    newer = json.loads(payload)["newer_observations"]
    assert set(newer) == {"availability_percent", "room_quality"}
    assert set(newer["room_quality"]) == {"study"}
    assert set(newer["room_quality"]["study"]) <= {"air", "target", "demand"}
    # Quality flags only, never current values or notes.
    assert all(isinstance(v, str) for v in newer["room_quality"]["study"].values())
    assert "18" not in encode(newer["room_quality"])


async def test_missing_report_and_evidence_mismatch_are_user_errors(hass, config, sources, freezer):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(ServiceValidationError, match="Unknown advisor report"):
            await a.followup("missing", "Why?")
        a.data["reports"][0]["evidence"]["facts"]["quality.history"] = {"tampered": True}
        with pytest.raises(ServiceValidationError, match="does not match"):
            await a.followup(r["id"], "Why?")
        generate.assert_not_called()
    assert not a.data["conversations"]


async def test_conversation_bound_to_its_report_and_snapshot(hass, config, sources, freezer):
    a = await advisor(hass, config)
    first = await report(a, freezer)
    second = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)):
        answer = await a.followup(first["id"], "Why?")
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(ServiceValidationError, match="different report"):
            await a.followup(second["id"], "Why?", answer["conversation_id"])
        with pytest.raises(ServiceValidationError, match="Unknown advisor conversation"):
            await a.followup(first["id"], "Why?", "missing")
        a.data["conversations"][0]["evidence_hash"] = "stale"
        with pytest.raises(ServiceValidationError, match="snapshot no longer matches"):
            await a.followup(first["id"], "Why?", answer["conversation_id"])
        generate.assert_not_called()


@pytest.mark.parametrize(
    "mutation,code",
    [
        (lambda v: v.update(references=["invented"]), "invalid_followup_reference"),
        (lambda v: v.update(answer="x" * 2001), "invalid_followup_answer"),
        (lambda v: v.update(answer="   "), "invalid_followup_answer"),
        (lambda v: v.update(service="climate.set_temperature"), "invalid_followup_structure"),
        (lambda v: v.update(missing_data=["m"] * 9), "too_many_followup_items"),
        (lambda v: v.update(unsupported_claims=[""]), "invalid_followup_unsupported_claim"),
    ],
)
async def test_malformed_answer_rejected_without_persisting_turn(
    hass, config, sources, freezer, mutation, code
):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    bad = {**deepcopy(ANSWER), "answer": "private answer text"}
    mutation(bad)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=bad)) as generate:
        with pytest.raises(HomeAssistantError, match=code) as error:
            await a.followup(r["id"], "Why?")
        generate.assert_awaited_once()
    assert "private answer text" not in str(error.value)
    assert a.status == "invalid_response" and a.quality()["error_type"] == code
    assert not a.data["conversations"]
    assert len(a.data["attempts"]) == 2
    restored = Advisor(hass, a.entry, a.heating)
    await restored.initialise()
    assert not restored.data["conversations"]


def test_validate_followup_response_rejects_oversize_and_unknown_reference():
    evidence = {"facts": {"quality.history": {}}}
    assert validate_followup_response(ANSWER, evidence) == ANSWER
    with pytest.raises(FollowupValidationError, match="too_large"):
        validate_followup_response({**ANSWER, "answer": "y" * 9000}, evidence)
    with pytest.raises(FollowupValidationError, match="invalid_followup_reference"):
        validate_followup_response({**ANSWER, "references": ["other"]}, evidence)
    assert isinstance(FollowupValidationError("x"), ValueError)


@pytest.mark.parametrize(
    "failure,status",
    [(TimeoutError(), "timeout"), (RuntimeError("provider secret"), "provider_failed")],
)
async def test_provider_failure_sets_status_without_retry_or_control_effect(
    hass, config, sources, freezer, failure, status
):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate", side_effect=failure) as generate:
        with pytest.raises(HomeAssistantError) as error:
            await a.followup(r["id"], "Why?")
        generate.assert_awaited_once()
    assert "provider secret" not in str(error.value)
    assert a.status == status
    assert len(a.data["attempts"]) == 2
    assert not a.data["conversations"]
    assert a.heating.snapshot().rooms[0].air.value == 18


async def test_restart_restores_conversations_and_accepts_old_store(hass, config, sources, freezer):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)):
        answer = await a.followup(r["id"], "Why?")
    restored = Advisor(hass, a.entry, a.heating)
    await restored.initialise()
    assert restored.followup_conversation(answer["conversation_id"]) == a.followup_conversation(
        answer["conversation_id"]
    )
    assert restored.report_list()["reports"][0]["conversations"][0]["turns"] == 1
    # A schema 1 store written before follow-ups existed loads read/write.
    old = {k: v for k, v in deepcopy(a.data).items() if k != "conversations"}
    legacy = Advisor(hass, a.entry, a.heating)
    with patch.object(legacy.backend, "async_load", return_value=old):
        await legacy.initialise()
    assert legacy.storage_ready and legacy.data["conversations"] == []
    # Corrupt conversation entries are treated like any other corrupt store.
    corrupt = Advisor(hass, a.entry, a.heating)
    with patch.object(
        corrupt.backend, "async_load", return_value={**deepcopy(a.data), "conversations": [{}]}
    ):
        await corrupt.initialise()
    assert corrupt.status == "storage_read_only"


async def test_turn_and_conversation_bounds(hass, config, sources, freezer):
    a = await advisor(hass, config)
    a.config["max_calls_per_day"] = 12
    r = await report(a, freezer)
    conversation = None
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)):
        for turn in range(1, MAX_TURNS_PER_CONVERSATION + 1):
            a.data["attempts"] = []
            answer = await a.followup(r["id"], f"Question {turn}", conversation)
            conversation = answer["conversation_id"]
            assert answer["turn"] == turn
        a.data["attempts"] = []
        with pytest.raises(ServiceValidationError, match="8 turns"):
            await a.followup(r["id"], "One more", conversation)
        with pytest.raises(ServiceValidationError, match="1-600"):
            await a.followup(r["id"], "x" * 601)
        with pytest.raises(ServiceValidationError, match="1-600"):
            await a.followup(r["id"], "   ")
        for n in range(MAX_CONVERSATIONS + 2):
            a.data["attempts"] = []
            await a.followup(r["id"], f"New conversation {n}")
    assert len(a.data["conversations"]) == MAX_CONVERSATIONS
    assert conversation not in {c["id"] for c in a.data["conversations"]}
    assert len(a.data["conversations"][-1]["turns"]) == 1
    assert len(a.followup_conversation(a.data["conversations"][0]["id"])["turns"]) == 1


async def test_conversations_are_isolated(hass, config, sources, freezer):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)) as generate:
        one = await a.followup(r["id"], "Alpha question")
        a.data["attempts"] = []
        two = await a.followup(r["id"], "Beta question")
        a.data["attempts"] = []
        await a.followup(r["id"], "Alpha again", one["conversation_id"])
        instructions = generate.call_args.kwargs["instructions"]
    assert one["conversation_id"] != two["conversation_id"]
    assert "Alpha question" in instructions and "Beta question" not in instructions
    assert [t["question"] for t in a.followup_conversation(two["conversation_id"])["turns"]] == [
        "Beta question"
    ]


async def test_followup_shares_daily_limit_and_spacing(hass, config, sources, freezer):
    a = await advisor(hass, config, max_calls_per_day=2)
    r = await report(a, freezer)
    freezer.tick(timedelta(minutes=-2))
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(ServiceValidationError, match="one minute"):
            await a.followup(r["id"], "Too soon")
        generate.assert_not_called()
    freezer.tick(timedelta(minutes=2))
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)):
        await a.followup(r["id"], "Second call")
    freezer.tick(timedelta(minutes=2))
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(ServiceValidationError, match="24-hour"):
            await a.followup(r["id"], "Third call")
        with pytest.raises(ServiceValidationError, match="24-hour"):
            await a.run("investigation")
        generate.assert_not_called()


async def test_no_followup_profile_means_no_call_and_no_fallback(hass, config, sources, freezer):
    a = await advisor(hass, config, followup=False)
    r = await report(a, freezer)
    with patch(MODULE + ".generate") as generate:
        with pytest.raises(ServiceValidationError, match="Select an AI Task profile"):
            await a.followup(r["id"], "Why?")
        generate.assert_not_called()
    assert not a.data["conversations"]
    assert len(a.data["attempts"]) == 1


async def test_services_return_answer_and_conversation_text(hass, config, sources, freezer):
    a = await advisor(hass, config)
    r = await report(a, freezer)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=ANSWER)):
        answer = await hass.services.async_call(
            DOMAIN,
            "ask_advisor_followup",
            {"report_id": r["id"], "question": "Why?"},
            blocking=True,
            return_response=True,
        )
    assert answer["answer"] == ANSWER["answer"]
    conversation = await hass.services.async_call(
        DOMAIN,
        "get_advisor_followup",
        {"conversation_id": answer["conversation_id"]},
        blocking=True,
        return_response=True,
    )
    assert conversation["turns"][0]["question"] == "Why?"
    with pytest.raises(Exception):
        await hass.services.async_call(
            DOMAIN,
            "ask_advisor_followup",
            {"report_id": r["id"], "question": "x" * 601},
            blocking=True,
            return_response=True,
        )
