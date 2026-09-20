"""Recommendation decisions and outcomes: durable, private, never actuator commands."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

import pytest
import voluptuous as vol
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError

from custom_components.home_heating_optimisation.advisor.coordinator import Advisor
from custom_components.home_heating_optimisation.advisor.recommendations import (
    MAX_RECOMMENDATIONS,
    PRIVATE_FIELDS,
    Recommendations,
    eligibility,
    extract,
    recommendation_id,
)
from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.boiler.coordinator import BFCCoordinator
from custom_components.home_heating_optimisation.control.comfort.coordinator import OTCoordinator
from custom_components.home_heating_optimisation.control.runtime import Controls
from tests.control.test_runtime import controlled as controlled  # noqa: F401
from tests.control.test_runtime import start
from tests.test_advisor import MODULE, VALID, advisor, profile

NOW = datetime(2026, 9, 20, 12, tzinfo=timezone.utc)
RESPONSE = deepcopy(VALID)
RESPONSE["findings"].append(
    {
        "title": "Study recovery slow",
        "detail": "Observed warm-up is slower than other rooms.",
        "kind": "hypothesis",
        "evidence_ids": ["room.1.within_band", "room.1.identity"],
        "next_check": "Observe one more complete heating cycle before comparing recovery.",
    }
)


async def stored(hass, config):
    a = await advisor(hass, config)
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=RESPONSE)):
        report = await a.run("investigation", "What should I check?")
    return a, report


async def call(hass, service, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


def applied(rec_id="abc", days_ago=8, era="era-1"):
    at = (NOW - timedelta(days=days_ago)).isoformat()
    return {"id": rec_id, "state": "applied", "applied_at": at, "applied_era": era}


async def test_stored_report_yields_stable_scoped_proposals(hass, config, sources):
    a, report = await stored(hass, config)
    recs = a.recommendations.data["recommendations"]
    assert [r["id"] for r in recs] == [recommendation_id(report["id"], i) for i in range(2)]
    assert recs[0]["scope"] == {"room_ids": [], "system": True}
    assert recs[1]["scope"] == {"room_ids": ["study"], "system": False}
    assert recs[1]["action_text"] == RESPONSE["findings"][1]["next_check"]
    assert recs[1]["profile"] == {"name": "Heating", "model": "test-model"}
    assert recs[1]["evidence_hash"] == report["evidence_hash"]
    assert recs[1]["prompt_version"] == report["prompt_version"]
    assert recs[1]["history"] == [
        {"state": "proposed", "at": recs[1]["created_at"], "by": "advisor"}
    ]
    # Re-adding the same report is idempotent and the pure extraction is deterministic.
    assert await a.recommendations.add_report(report) == []
    assert [r["id"] for r in extract(report, NOW)] == [r["id"] for r in recs]
    assert recommendation_id("r", 0) == recommendation_id("r", 0) != recommendation_id("r", 1)
    assert a.quality()["recommendations"]["counts"]["proposed"] == 2
    listing = a.recommendations.report_list(room_id="study")
    assert [r["id"] for r in listing["recommendations"]] == [recs[1]["id"]]
    assert listing["evidence_type"] == "association"


@pytest.mark.parametrize(
    "path,illegal",
    [
        (["accepted", "applied", "evaluated"], {"proposed": ["applied", "evaluated"]}),
        (["deferred", "accepted"], {"deferred": ["deferred", "applied", "evaluated"]}),
        (["deferred", "rejected"], {"rejected": ["accepted", "applied"]}),
        (["rejected"], {"rejected": ["deferred", "evaluated"]}),
    ],
)
async def test_legal_and_illegal_transitions(hass, config, sources, path, illegal):
    a, report = await stored(hass, config)
    r = a.recommendations
    rec_id = r.data["recommendations"][0]["id"]

    async def move(to):
        if to in ("accepted", "rejected", "deferred"):
            return await r.decide(rec_id, to, defer_until=NOW if to == "deferred" else None)
        if to == "applied":
            return await r.mark_applied(rec_id)
        return await r.evaluate(rec_id, "inconclusive", NOW, NOW + timedelta(days=7))

    for state, targets in illegal.items():
        while r.find(rec_id)["state"] != state:
            await move(path[len(r.find(rec_id)["history"]) - 1])
        for to in targets:
            with pytest.raises(ServiceValidationError, match="cannot move"):
                await move(to)
    while len(r.find(rec_id)["history"]) - 1 < len(path):
        await move(path[len(r.find(rec_id)["history"]) - 1])
    rec = r.find(rec_id)
    assert [h["state"] for h in rec["history"]] == ["proposed", *path]
    assert all(h["by"] == "owner" for h in rec["history"][1:])
    if path[-1] == "evaluated":
        assert rec["outcome"] == "inconclusive"
        assert rec["evidence_type"] == "association"
        assert rec["evaluation_window"]["end"] > rec["evaluation_window"]["start"]
    with pytest.raises(ServiceValidationError, match="Unknown recommendation"):
        r.find("missing")


async def test_services_store_private_notes_and_strip_them_by_default(hass, config, sources):
    a, report = await stored(hass, config)
    rec_id = a.recommendations.data["recommendations"][0]["id"]
    response = await call(
        hass, "decide_recommendation", recommendation_id=rec_id, decision="accepted", note="why"
    )
    assert response["state"] == "accepted" and "private_note" not in response
    await call(
        hass,
        "mark_recommendation_applied",
        recommendation_id=rec_id,
        intervention_note="turned lockshield",
        journal_event_id="evt-1",
    )
    public = await call(hass, "get_recommendations", state="applied")
    private = await call(hass, "get_recommendations", include_private=True)
    assert [r["id"] for r in public["recommendations"]] == [rec_id]
    text = str(public)
    assert "why" not in text and "turned lockshield" not in text
    assert public["recommendations"][0]["journal_event_id"] == "evt-1"
    assert public["recommendations"][0]["eligibility"]["evidence_type"] == "association"
    found = next(r for r in private["recommendations"] if r["id"] == rec_id)
    assert found["private_note"] == "why" and found["intervention_note"] == "turned lockshield"
    with pytest.raises(vol.Invalid):
        await call(
            hass,
            "decide_recommendation",
            recommendation_id=rec_id,
            decision="accepted",
            note="x" * 501,
        )
    with pytest.raises(ServiceValidationError, match="end after"):
        await call(
            hass,
            "evaluate_recommendation",
            recommendation_id=rec_id,
            outcome="failed",
            window_start=NOW,
            window_end=NOW,
        )
    response = await call(
        hass,
        "evaluate_recommendation",
        recommendation_id=rec_id,
        outcome="no_change",
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
    )
    assert response["outcome"] == "no_change"
    state = hass.states.get("sensor.home_heating_optimisation_recommendations")
    assert state.state == "1"
    assert state.attributes["evaluated_count"] == 1
    assert state.attributes["latest_id"] == a.recommendations.data["recommendations"][-1]["id"]
    assert "eligible_for_evaluation" in state.attributes
    assert "Limited evidence" not in str(state.attributes)


async def test_accepting_and_applying_touch_no_controller(hass, controlled, sources):  # noqa: F811
    controlled["analytics_enabled"] = True
    controlled["advisor"] = {"enabled": True, "investigation": profile(hass)}
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        return_value=([], False, "complete"),
    ):
        entry, controls, calls = await start(hass, controlled)
    a = entry.runtime_data.advisor
    with patch(MODULE + ".generate", return_value=SimpleNamespace(data=RESPONSE)):
        await a.run("investigation")
    rec_id = a.recommendations.data["recommendations"][1]["id"]
    with (
        patch.object(Controls, "set_mode") as set_mode,
        patch.object(BFCCoordinator, "_perform") as boiler,
        patch.object(OTCoordinator, "_perform") as room,
    ):
        await call(hass, "decide_recommendation", recommendation_id=rec_id, decision="accepted")
        await call(hass, "mark_recommendation_applied", recommendation_id=rec_id)
        await call(
            hass,
            "evaluate_recommendation",
            recommendation_id=rec_id,
            outcome="improved",
            window_start=NOW,
            window_end=NOW + timedelta(days=7),
        )
        await hass.async_block_till_done()
    set_mode.assert_not_called()
    boiler.assert_not_called()
    room.assert_not_called()
    assert calls == []
    assert controls.settings.get("modes", {}) == {}
    rec = a.recommendations.find(rec_id)
    assert isinstance(rec["applied_era"], str) and len(rec["applied_era"]) == 16
    assert "era_changed" not in a.recommendations.assess(rec)["reasons"]


async def test_persistence_bounds_and_retention(hass, config, sources):
    a, report = await stored(hass, config)
    r = a.recommendations
    rec_id = r.data["recommendations"][0]["id"]
    await r.decide(rec_id, "deferred", note="later", defer_until=NOW + timedelta(days=3))
    restored = Advisor(hass, a.entry, a.heating)
    await restored.initialise()
    assert restored.recommendations.data == r.data
    assert (
        restored.recommendations.find(rec_id)["defer_until"]
        == (NOW + timedelta(days=3)).isoformat()
    )
    old = deepcopy(r.data["recommendations"][0])
    old.update(id="old", updated_at=(NOW - timedelta(days=366)).isoformat())
    filler = [
        {**deepcopy(r.data["recommendations"][0]), "id": f"f{n}", "updated_at": NOW.isoformat()}
        for n in range(MAX_RECOMMENDATIONS + 5)
    ]
    r.data["recommendations"] = [old, *filler]
    r.bound(NOW)
    ids = [x["id"] for x in r.data["recommendations"]]
    assert len(ids) == MAX_RECOMMENDATIONS and "old" not in ids and ids[-1] == "f204"


@pytest.mark.parametrize(
    "rec,kwargs,reasons,unknown",
    [
        (applied(), {"coverage": 95, "era": "era-1", "energy": {}}, [], ["energy"]),
        (
            applied(),
            {
                "coverage": 95,
                "era": "era-1",
                "energy": {"conclusion": "comparable", "hard_limits": [], "limits": []},
            },
            [],
            [],
        ),
        (
            applied(),
            {
                "coverage": 95,
                "era": "era-1",
                "energy": {
                    "conclusion": "comparable",
                    "hard_limits": [],
                    "limits": ["period_a_contains_interventions"],
                },
            },
            [],
            [],
        ),
        (
            applied(),
            {
                "coverage": 95,
                "era": "era-1",
                "energy": {
                    "conclusion": "insufficient",
                    "hard_limits": ["period_b_no_data"],
                    "limits": ["period_b_no_data"],
                },
            },
            ["energy_not_comparable"],
            [],
        ),
        (
            applied(),
            {"coverage": 95, "era": "era-1", "energy": {"limits": ["dhw_share_unknown"]}},
            ["energy_not_comparable"],
            [],
        ),
        (
            applied(days_ago=3),
            {"coverage": 95, "era": "era-1", "energy": None},
            ["too_early"],
            ["energy"],
        ),
        (applied(), {"coverage": 95, "era": "era-2", "energy": None}, ["era_changed"], ["energy"]),
        (applied(), {"coverage": 50, "era": "era-1", "energy": None}, ["low_coverage"], ["energy"]),
        (
            applied(),
            {"coverage": None, "era": None, "energy": None},
            ["coverage_unknown", "era_unknown"],
            ["energy"],
        ),
        (
            {"id": "x", "state": "accepted"},
            {"coverage": 95, "era": "e"},
            ["not_applied", "era_unknown"],
            ["energy"],
        ),
    ],
)
def test_eligibility_reasons_are_explicit_and_association_only(rec, kwargs, reasons, unknown):
    result = eligibility(rec, NOW, **kwargs)
    assert result["reasons"] == reasons
    assert result["unknown"] == unknown
    assert result["eligible"] is (not reasons)
    assert result["evidence_type"] == "association"
    assert "causal" not in str(result)


async def test_energy_context_uses_the_real_comparability_contract(hass, config, sources):
    """An explicitly insufficient energy comparison must block eligibility, not pass it."""
    a, report = await stored(hass, config)
    rec_id = a.recommendations.data["recommendations"][1]["id"]
    await a.recommendations.decide(rec_id, "accepted")
    await a.recommendations.mark_applied(rec_id)
    rec = a.recommendations.find(rec_id)
    rec["applied_at"] = (NOW - timedelta(days=10)).isoformat()
    calls = []

    class Energy:
        def __init__(self, result):
            self.result = result

        def comparability(self, since):
            calls.append(since)
            return self.result

    a.heating.energy = Energy(
        {"conclusion": "insufficient", "hard_limits": ["period_b_no_data"], "limits": ["x"]}
    )
    assessment = a.recommendations.assess(rec)
    assert calls and isinstance(calls[0], datetime) and calls[0].tzinfo is not None
    assert "energy_not_comparable" in assessment["reasons"] and assessment["eligible"] is False
    assert "energy" not in assessment["unknown"]
    a.heating.energy = Energy({"conclusion": "comparable", "hard_limits": [], "limits": []})
    assessment = a.recommendations.assess(rec)
    assert "energy_not_comparable" not in assessment["reasons"]
    assert assessment["unknown"] == []
    a.heating.energy = None
    assert a.recommendations.assess(rec)["unknown"] == ["energy"]


async def test_evaluation_note_is_private_by_default(hass, config, sources):
    a, report = await stored(hass, config)
    rec_id = a.recommendations.data["recommendations"][0]["id"]
    await call(hass, "decide_recommendation", recommendation_id=rec_id, decision="accepted")
    await call(hass, "mark_recommendation_applied", recommendation_id=rec_id)
    response = await call(
        hass,
        "evaluate_recommendation",
        recommendation_id=rec_id,
        outcome="improved",
        window_start=NOW,
        window_end=NOW + timedelta(days=7),
        note="felt warmer in the evenings",
    )
    assert "evaluation_note" not in response
    public = await call(hass, "get_recommendations")
    private = await call(hass, "get_recommendations", include_private=True)
    assert "felt warmer" not in str(public)
    assert all("evaluation_note" not in r for r in public["recommendations"])
    found = next(r for r in private["recommendations"] if r["id"] == rec_id)
    assert found["evaluation_note"] == "felt warmer in the evenings"
    assert "evaluation_note" in PRIVATE_FIELDS


async def test_linked_interventions_are_counted_from_the_real_journal(
    hass, config, sources, freezer
):
    """applied_at is a datetime; the journal stores epochs. The count must still work."""
    freezer.move_to("2026-09-20T12:00:00+00:00")
    a, report = await stored(hass, config)
    journal = a.heating.journal
    assert journal is not None and journal.status == "ready"
    rec_id = a.recommendations.data["recommendations"][1]["id"]
    journal.record("command_sent", room_id="study", scope="study")  # before applying
    await a.recommendations.decide(rec_id, "accepted")
    freezer.tick(timedelta(minutes=1))
    await a.recommendations.mark_applied(rec_id)
    freezer.tick(timedelta(minutes=1))
    journal.record("command_sent", room_id="study", scope="study")
    journal.record("manual_override", room_id="lounge")  # other room, not counted
    item = a.recommendations.public(a.recommendations.find(rec_id))
    assert item["linked_intervention_count"] == 1


async def test_journal_contract_is_used_defensively(hass, config, sources):
    a, report = await stored(hass, config)
    events = []

    class Journal:
        def record(self, kind, **kwargs):
            events.append((kind, kwargs))
            return {"id": len(events)}

        def events(self, kinds=None, room_id=None, since=None, until=None, limit=None):
            return [{"kind": "manual_override"}, {"kind": "command_sent"}]

    a.heating.journal = Journal()
    rec_id = a.recommendations.data["recommendations"][1]["id"]
    await a.recommendations.decide(rec_id, "accepted", note="secret reason")
    await a.recommendations.mark_applied(rec_id)
    assert [(k, e["data"]["from"], e["data"]["to"]) for k, e in events] == [
        ("recommendation", "proposed", "accepted"),
        ("recommendation", "accepted", "applied"),
    ]
    assert all(e["origin"] == "user" and e["room_id"] == "study" for _, e in events)
    assert "secret reason" not in str(events)
    assert (
        a.recommendations.public(a.recommendations.find(rec_id))["linked_intervention_count"] == 2
    )

    class Broken:
        def record(self, *a, **k):
            raise RuntimeError("journal down")

        events = record

    a.heating.journal = Broken()
    item = await a.recommendations.evaluate(rec_id, "worse", NOW, NOW + timedelta(days=8))
    assert item["state"] == "evaluated" and "linked_intervention_count" not in item


async def test_corrupt_store_is_read_only_and_reports_still_run(hass, config, sources):
    a = await advisor(hass, config)
    bad = {"schema": 1, "recommendations": [{"id": "x", "state": "unknown"}]}
    with patch.object(a.recommendations.backend, "async_load", return_value=bad):
        await a.initialise()
    assert a.status == "ready"
    assert a.recommendations.status == "storage_read_only"
    with patch.object(a.recommendations.backend, "async_save") as save:
        with patch(MODULE + ".generate", return_value=SimpleNamespace(data=RESPONSE)):
            report = await a.run("investigation")
        save.assert_not_called()
    assert a.report_list(report["id"]) == report
    assert a.recommendations.data["recommendations"] == []
    with pytest.raises(HomeAssistantError, match="read-only"):
        await a.recommendations.decide("x", "accepted")
    fresh = Recommendations(hass, a.entry, a.heating)
    with patch.object(fresh.backend, "async_load", return_value={"schema": 2}):
        await fresh.initialise()
    assert fresh.status == "storage_read_only"


async def test_save_failure_holds_decision_in_memory_without_stopping_heating(
    hass, config, sources
):
    a, report = await stored(hass, config)
    rec_id = a.recommendations.data["recommendations"][0]["id"]
    with patch.object(a.recommendations.backend, "async_save", side_effect=OSError):
        with pytest.raises(HomeAssistantError, match="could not be saved"):
            await a.recommendations.decide(rec_id, "rejected")
    assert a.recommendations.find(rec_id)["state"] == "rejected"
    assert a.recommendations.status == "save_failed"
    assert a.heating.snapshot().rooms[0].air.value == 18
