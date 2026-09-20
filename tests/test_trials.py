"""Bounded trials: allowlisted tunables only, explicit approval, tested rollback."""

from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.boiler.coordinator import BFCCoordinator
from custom_components.home_heating_optimisation.control.comfort.coordinator import OTCoordinator
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.control.runtime import Controls
from custom_components.home_heating_optimisation.trials.const import ALLOWED_PARAMETERS
from custom_components.home_heating_optimisation.trials.coordinator import ISSUE_ROLLBACK_FAILED
from tests.control.test_runtime import controlled as controlled  # noqa: F401
from tests.control.test_runtime import start

STORE = "home_heating_optimisation.{}.trials"


async def call(hass, service, **data):
    return await hass.services.async_call(
        DOMAIN, service, data, blocking=True, return_response=True
    )


def fake_analytics(deficit=0.2, overshoot=0.1, within_band=90.0, coverage=95.0):
    stats = {
        "study": {
            "deficit_degree_hours": deficit,
            "overshoot_degree_hours": overshoot,
            "within_band": within_band,
            "coverage": coverage,
        }
    }
    return SimpleNamespace(report=lambda: {"analysis": {"zone_stats": stats}}, stop=AsyncMock())


async def live(hass, controlled, scopes=("study",)):
    """Controls handed over and the requested scopes already active/auto."""
    entry, c, calls = await start(hass, controlled)
    entry.runtime_data.analytics = fake_analytics()
    await handover(c)
    for scope in scopes:
        await c.set_mode(scope, "auto" if scope == "boiler" else "active")
    calls.clear()
    return entry, c, calls


async def running(hass, controlled, scope="study", parameter="trust_k", target=0.6, **extra):
    entry, c, calls = await live(hass, controlled, scopes=(scope,))
    proposal = await call(
        hass,
        "propose_trial",
        scope=scope,
        parameter=parameter,
        target_value=target,
        rationale="private reason",
        duration_hours=2,
        **extra,
    )
    trial_id = proposal["id"]
    await call(hass, "approve_trial", trial_id=trial_id, confirm=True)
    await call(hass, "start_trial", trial_id=trial_id, confirm=True)
    return entry, c, calls, trial_id


def coordinator(c, scope):
    return c.boiler if scope == "boiler" else c.rooms[scope]


# Allowlist and bounds ------------------------------------------------------------


@pytest.mark.parametrize(
    "scope,parameter",
    [
        ("boiler", "dhw_delta"),
        ("boiler", "mode_override"),
        ("boiler", "enabled"),
        ("boiler", "dhw_flow_max"),
        ("study", "mode"),
        ("study", "enabled"),
        ("study", "manual_setpoint"),
        ("study", "design_flow"),
        ("boiler", "trust_k"),
    ],
)
async def test_only_allowlisted_tunables_can_be_proposed(
    hass, controlled, sources, scope, parameter
):
    entry, c, calls = await live(hass, controlled)
    with pytest.raises(ServiceValidationError, match="not permitted"):
        await call(
            hass,
            "propose_trial",
            scope=scope,
            parameter=parameter,
            target_value=1,
            rationale="x",
            duration_hours=2,
        )
    assert entry.runtime_data.trials.store.trials == []
    assert "dhw_delta" not in ALLOWED_PARAMETERS["boiler"]


@pytest.mark.parametrize(
    "scope,parameter,target,match",
    [
        ("study", "trust_k", 1.5, "between"),
        ("study", "trust_k", 0.2, "at most"),
        ("study", "trust_k", 0.8, "equals"),
        ("boiler", "design_flow", 80.0, "at most"),
        ("boiler", "design_outdoor", -20.0, "between"),
        ("unknown_room", "trust_k", 0.7, "Scope must be"),
    ],
)
async def test_bounds_max_step_and_scope_are_enforced(
    hass, controlled, sources, scope, parameter, target, match
):
    entry, c, calls = await live(hass, controlled)
    with pytest.raises(ServiceValidationError, match=match):
        await call(
            hass,
            "propose_trial",
            scope=scope,
            parameter=parameter,
            target_value=target,
            rationale="x",
            duration_hours=2,
        )


async def test_duration_comfort_floor_and_recommendation_link_are_validated(
    hass, controlled, sources
):
    entry, c, calls = await live(hass, controlled)
    base = dict(scope="study", parameter="trust_k", target_value=0.6, rationale="x")
    with pytest.raises(Exception):
        await call(hass, "propose_trial", duration_hours=0, **base)
    with pytest.raises(ServiceValidationError, match="Comfort floor"):
        await call(hass, "propose_trial", duration_hours=2, comfort_floor_c=40, **base)
    with pytest.raises(ServiceValidationError, match="Unknown recommendation"):
        await call(hass, "propose_trial", duration_hours=2, recommendation_id="nope", **base)
    assert entry.runtime_data.trials.store.trials == []


# Happy path ----------------------------------------------------------------------


async def test_propose_approve_start_applies_only_through_set_tunable(hass, controlled, sources):
    entry, c, calls = await live(hass, controlled)
    room = c.rooms["study"]
    baseline = room.get_tunable("trust_k")
    with (
        patch.object(OTCoordinator, "_perform", new=AsyncMock()) as room_perform,
        patch.object(BFCCoordinator, "_perform", new=AsyncMock()) as boiler_perform,
        patch.object(Controls, "set_mode", new=AsyncMock()) as set_mode,
    ):
        proposal = await call(
            hass,
            "propose_trial",
            scope="study",
            parameter="trust_k",
            target_value=0.6,
            rationale="private reason",
            duration_hours=24,
            comfort_floor_c=16,
        )
        assert proposal["state"] == "proposed"
        assert proposal["baseline_value"] == baseline == proposal["rollback_value"]
        assert proposal["bounds"] == {"min": 0.0, "max": 1.0, "max_step": 0.2, "unit": None}
        assert proposal["stop_criteria"]["max_deficit_degree_hours"] == {"study": 1.2}
        assert proposal["success_criteria"]["deficit_degree_hours_not_above"] == {"study": 0.2}
        assert proposal["stop_criteria"]["dhw_active"] == "note_only"
        assert "private_rationale" not in proposal
        assert room.get_tunable("trust_k") == baseline
        with pytest.raises(ServiceValidationError, match="confirm"):
            await call(hass, "approve_trial", trial_id=proposal["id"], confirm=False)
        approved = await call(hass, "approve_trial", trial_id=proposal["id"], confirm=True)
        assert approved["state"] == "approved" and room.get_tunable("trust_k") == baseline
        with pytest.raises(ServiceValidationError, match="confirm"):
            await call(hass, "start_trial", trial_id=proposal["id"], confirm=False)
        started = await call(hass, "start_trial", trial_id=proposal["id"], confirm=True)
    assert started["state"] == "running"
    assert started["applied"]["before"] == baseline and started["applied"]["after"] == 0.6
    assert room.get_tunable("trust_k") == 0.6
    assert dt_util.parse_datetime(started["expires_at"]) - dt_util.parse_datetime(
        started["started_at"]
    ) == timedelta(hours=24)
    assert calls == [] and not room_perform.called and not boiler_perform.called
    assert not set_mode.called
    assert room.mode == "active"  # the trial never touched the mode
    assert [h["state"] for h in started["history"]] == ["proposed", "approved", "running"]
    sensor = hass.states.get("sensor.home_heating_optimisation_trials")
    assert sensor.state == "1"
    assert sensor.attributes["running_scope"] == "study"
    assert sensor.attributes["running_parameter"] == "trust_k"
    assert "private reason" not in str(sensor.attributes)
    listing = await call(hass, "get_trials", state="running")
    assert [t["id"] for t in listing["trials"]] == [proposal["id"]]
    assert "private reason" not in str(listing)
    private = await call(hass, "get_trials", include_private=True)
    assert private["trials"][0]["private_rationale"] == "private reason"
    # Persisted through the control store so the applied value survives with the room.
    assert room._store.get("trust_k") == 0.6


async def test_start_refuses_when_baseline_moved_after_proposal(hass, controlled, sources):
    entry, c, calls = await live(hass, controlled)
    proposal = await call(
        hass,
        "propose_trial",
        scope="study",
        parameter="trust_k",
        target_value=0.6,
        rationale="x",
        duration_hours=2,
    )
    await call(hass, "approve_trial", trial_id=proposal["id"], confirm=True)
    c.rooms["study"].set_tunable("trust_k", 0.5)
    with pytest.raises(ServiceValidationError, match="differs from the proposal baseline"):
        await call(hass, "start_trial", trial_id=proposal["id"], confirm=True)
    assert c.rooms["study"].get_tunable("trust_k") == 0.5


# Approval refusals ---------------------------------------------------------------


async def propose(hass, scope="study", parameter="trust_k", target=0.6):
    return (
        await call(
            hass,
            "propose_trial",
            scope=scope,
            parameter=parameter,
            target_value=target,
            rationale="x",
            duration_hours=2,
        )
    )["id"]


async def test_approve_refused_while_scope_is_shadow(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    trial_id = await propose(hass)
    with pytest.raises(ServiceValidationError, match="already be active"):
        await call(hass, "approve_trial", trial_id=trial_id, confirm=True)
    boiler_id = await propose(hass, "boiler", "design_flow", 60)
    with pytest.raises(ServiceValidationError, match="already be in auto"):
        await call(hass, "approve_trial", trial_id=boiler_id, confirm=True)
    assert c.rooms["study"].mode == "shadow" and c.boiler.override == "shadow"


async def test_approve_refused_before_handover(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    trial_id = await propose(hass)
    with pytest.raises(ServiceValidationError, match="handover"):
        await call(hass, "approve_trial", trial_id=trial_id, confirm=True)


async def test_approve_refused_with_guard_reason(hass, controlled, sources):
    entry, c, calls = await live(hass, controlled)
    trial_id = await propose(hass)
    with patch.object(Controls, "guard_reason", return_value="thermostat missing or stale"):
        with pytest.raises(ServiceValidationError, match="thermostat missing"):
            await call(hass, "approve_trial", trial_id=trial_id, confirm=True)


async def test_approve_refused_while_another_trial_runs(hass, controlled, sources):
    entry, c, calls, first = await running(hass, controlled)
    second = await propose(hass, "study", "cap_up", 1.0)
    with pytest.raises(ServiceValidationError, match="one change at a time"):
        await call(hass, "approve_trial", trial_id=second, confirm=True)
    await call(hass, "reject_trial", trial_id=second, note="later")
    assert entry.runtime_data.trials.find(second)["state"] == "rejected"


async def test_start_refused_when_conditions_changed_after_approval(hass, controlled, sources):
    entry, c, calls = await live(hass, controlled)
    trial_id = await propose(hass)
    await call(hass, "approve_trial", trial_id=trial_id, confirm=True)
    await c.set_mode("study", "shadow")
    with pytest.raises(ServiceValidationError, match="already be active"):
        await call(hass, "start_trial", trial_id=trial_id, confirm=True)
    assert c.rooms["study"].get_tunable("trust_k") == 0.8


# Automatic rollback ---------------------------------------------------------------


async def test_expiry_rolls_back_and_marks_expired(hass, controlled, sources, freezer):
    entry, c, calls, trial_id = await running(hass, controlled)
    trials = entry.runtime_data.trials
    room = c.rooms["study"]
    await trials._tick()
    assert trials.find(trial_id)["state"] == "running" and room.get_tunable("trust_k") == 0.6
    freezer.tick(timedelta(hours=2, seconds=1))
    await trials._tick()
    trial = trials.find(trial_id)
    assert trial["state"] == "expired" and trial["stop_reason"] == "duration_elapsed"
    assert trial["rollback_verified"] is True
    assert room.get_tunable("trust_k") == 0.8 and room._store.get("trust_k") == 0.8
    assert calls == []
    assert hass.states.get("sensor.home_heating_optimisation_trials").state == "0"


@pytest.mark.parametrize(
    "arrange,reason",
    [
        (lambda entry, c: setattr(c.rooms["study"], "mode", "shadow"), "mode_changed"),
        (lambda entry, c: c.rooms["study"]._store.set("manual_setpoint", 23.0), "manual_override"),
        (
            lambda entry, c: setattr(entry.runtime_data, "analytics", fake_analytics(deficit=5.0)),
            "deficit_degree_hours_exceeded",
        ),
        (
            lambda entry, c: setattr(
                entry.runtime_data, "analytics", fake_analytics(overshoot=3.0)
            ),
            "overshoot_degree_hours_exceeded",
        ),
    ],
)
async def test_each_stop_criterion_rolls_back(hass, controlled, sources, arrange, reason):
    entry, c, calls, trial_id = await running(hass, controlled)
    trials = entry.runtime_data.trials
    arrange(entry, c)
    await trials._tick()
    trial = trials.find(trial_id)
    assert trial["state"] == "stopped" and trial["stop_reason"] == reason
    assert c.rooms["study"].get_tunable("trust_k") == 0.8


async def test_guard_reason_while_running_rolls_back(hass, controlled, sources):
    entry, c, calls, trial_id = await running(hass, controlled)
    with patch.object(Controls, "guard_reason", return_value="room temperature stale"):
        await entry.runtime_data.trials._tick()
    trial = entry.runtime_data.trials.find(trial_id)
    assert trial["state"] == "stopped" and trial["stop_reason"] == "guard: room temperature stale"
    assert c.rooms["study"].get_tunable("trust_k") == 0.8


async def test_comfort_floor_uses_measured_air_temperature(hass, controlled, sources):
    entry, c, calls, trial_id = await running(hass, controlled, comfort_floor_c=17.5)
    trials = entry.runtime_data.trials
    await trials._tick()
    assert trials.find(trial_id)["state"] == "running"
    hass.states.async_set("sensor.air", 17, {"unit_of_measurement": "°C"})
    await c.rooms["study"].async_refresh()
    await trials._tick()
    trial = trials.find(trial_id)
    assert trial["state"] == "stopped" and trial["stop_reason"] == "comfort_floor"
    assert trial["stop_detail"] == {"room": "study", "air_temp": 17.0}
    assert c.rooms["study"].get_tunable("trust_k") == 0.8


async def test_boiler_trial_notes_dhw_activity_without_stopping_or_touching_dhw(
    hass, controlled, sources
):
    entry, c, calls, trial_id = await running(hass, controlled, "boiler", "design_flow", 60)
    dhw_delta = c.boiler.get_tunable("dhw_delta")
    hass.states.async_set("sensor.hw", 100)
    await c.boiler.async_refresh()
    assert c.boiler.data.dhw_active
    calls.clear()
    await entry.runtime_data.trials._tick()
    trial = entry.runtime_data.trials.find(trial_id)
    assert trial["state"] == "running" and trial["dhw_active_observed"] is True
    assert c.boiler.get_tunable("design_flow") == 60
    assert c.boiler.get_tunable("dhw_delta") == dhw_delta
    assert calls == []


async def test_stop_trial_service_rolls_back_on_demand(hass, controlled, sources):
    entry, c, calls, trial_id = await running(hass, controlled)
    result = await call(hass, "stop_trial", trial_id=trial_id, complete=True, note="fine")
    assert result["state"] == "completed" and result["stop_reason"] == "owner"
    assert "private_note" not in result
    assert c.rooms["study"].get_tunable("trust_k") == 0.8
    with pytest.raises(ServiceValidationError, match="nothing to stop"):
        await call(hass, "stop_trial", trial_id=trial_id)


# Restart, unload, rollback verification ------------------------------------------


async def test_restart_with_persisted_running_trial_rolls_back(
    hass, controlled, sources, hass_storage
):
    entry, c, calls, trial_id = await running(hass, controlled)
    stored = hass_storage[STORE.format(entry.entry_id)]["data"]["trials"][0]
    assert stored["state"] == "running"
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    # Simulate an abrupt stop: the store still says running and the value is still applied.
    hass_storage[STORE.format(entry.entry_id)]["data"]["trials"][0]["state"] = "running"
    hass_storage[f"home_heating_optimisation.control.{entry.entry_id}.room.study"]["data"][
        "trust_k"
    ] = 0.6
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    trials = entry.runtime_data.trials
    trial = trials.find(trial_id)
    assert trial["state"] == "stopped" and trial["stop_reason"] == "restart"
    room = entry.runtime_data.controls.rooms["study"]
    assert room.get_tunable("trust_k") == 0.8 and room.mode == "active"
    assert hass_storage[STORE.format(entry.entry_id)]["data"]["trials"][0]["state"] == "stopped"


async def test_unload_rolls_back_running_trial(hass, controlled, sources, hass_storage):
    entry, c, calls, trial_id = await running(hass, controlled)
    room = c.rooms["study"]
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert room.get_tunable("trust_k") == 0.8
    stored = hass_storage[STORE.format(entry.entry_id)]["data"]["trials"][0]
    assert stored["state"] == "stopped" and stored["stop_reason"] == "unload"
    assert (
        hass_storage[f"home_heating_optimisation.control.{entry.entry_id}.room.study"]["data"][
            "trust_k"
        ]
        == 0.8
    )


async def test_rollback_verification_failure_raises_repairs_issue(hass, controlled, sources):
    entry, c, calls, trial_id = await running(hass, controlled)
    room = c.rooms["study"]
    with patch.object(room, "set_tunable", lambda key, value: None):
        result = await call(hass, "stop_trial", trial_id=trial_id)
    assert result["state"] == "rollback_failed" and result["rollback_verified"] is False
    issue = ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_ROLLBACK_FAILED}_{trial_id}")
    assert issue is not None and issue.translation_key == ISSUE_ROLLBACK_FAILED
    assert issue.translation_placeholders["parameter"] == "trust_k"
    assert (
        hass.states.get("sensor.home_heating_optimisation_trials").attributes[
            "rollback_failed_count"
        ]
        == 1
    )
    # A later stop that does verify clears the issue and closes the trial.
    result = await call(hass, "stop_trial", trial_id=trial_id)
    assert result["state"] == "rolled_back" and room.get_tunable("trust_k") == 0.8
    assert ir.async_get(hass).async_get_issue(DOMAIN, f"{ISSUE_ROLLBACK_FAILED}_{trial_id}") is None


# Evaluation ----------------------------------------------------------------------


async def test_evaluation_records_criteria_and_separates_air_from_operative(
    hass, controlled, sources
):
    entry, c, calls, trial_id = await running(hass, controlled)
    with pytest.raises(ServiceValidationError, match="evaluate only ended"):
        await call(hass, "evaluate_trial", trial_id=trial_id, outcome="improved")
    await call(hass, "stop_trial", trial_id=trial_id)
    entry.runtime_data.analytics = fake_analytics(deficit=0.3, within_band=88.0)
    result = await call(
        hass, "evaluate_trial", trial_id=trial_id, outcome="inconclusive", note="too short"
    )
    evaluation = result["evaluation"]
    assert evaluation["outcome"] == "inconclusive"
    assert evaluation["evidence_type"] == "association"
    assert evaluation["predeclared"]["success_criteria"]["deficit_degree_hours_not_above"] == {
        "study": 0.2
    }
    assert evaluation["predeclared"]["stop_criteria"]["max_deficit_degree_hours"] == {"study": 1.2}
    assert evaluation["measured_metrics"]["study"]["deficit_degree_hours"] == 0.3
    assert evaluation["baseline_metrics"]["study"]["deficit_degree_hours"] == 0.2
    room = c.rooms["study"].data
    assert evaluation["comfort"]["air_temp"]["values"] == {"study": room.air_temp}
    assert evaluation["comfort"]["operative_temp"]["values"] == {"study": room.operative_temp}
    assert "measured" in evaluation["comfort"]["air_temp"]["meaning"]
    assert "estimated" in evaluation["comfort"]["operative_temp"]["meaning"]
    assert "private_note" not in result
    assert result["state"] == "stopped"  # inconclusive results are retained, never discarded
    listing = await call(hass, "get_trials", scope="study")
    assert listing["trials"][0]["evaluation"]["outcome"] == "inconclusive"
    assert "too short" not in str(listing)


# Failure isolation ---------------------------------------------------------------


async def test_corrupt_store_is_read_only_and_heating_unaffected(
    hass, controlled, sources, hass_storage
):
    entry = MockConfigEntry(domain=DOMAIN, title="Heating", unique_id=DOMAIN, data=controlled)
    entry.add_to_hass(hass)
    hass_storage[STORE.format(entry.entry_id)] = {
        "version": 1,
        "minor_version": 1,
        "key": STORE.format(entry.entry_id),
        "data": {"schema": 1, "trials": [{"id": "bad"}]},
    }
    hass.states.async_set("sensor.air", 18, {"unit_of_measurement": "°C"})
    hass.states.async_set("climate.cloud", "auto", {"status": {"setpoints": {"this_sp_temp": 20}}})
    hass.states.async_set(
        "number.flow_setpoint", 50, {"unit_of_measurement": "°C", "min": 0, "max": 90, "step": 1}
    )
    hass.states.async_set("number.limit", 70, {"unit_of_measurement": "°C"})
    hass.states.async_set("sensor.hw", 0)
    hass.states.async_set("sensor.cylinder", 50, {"unit_of_measurement": "°C"})
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    trials = entry.runtime_data.trials
    assert trials.store.status == "storage_read_only" and trials.store.trials == []
    assert hass_storage[STORE.format(entry.entry_id)]["data"]["trials"] == [{"id": "bad"}]
    with pytest.raises(HomeAssistantError, match="read-only"):
        await call(
            hass,
            "propose_trial",
            scope="study",
            parameter="trust_k",
            target_value=0.6,
            rationale="x",
            duration_hours=2,
        )
    c = entry.runtime_data.controls
    await handover(c)
    await c.set_mode("study", "active")
    assert c.rooms["study"].mode == "active"
    assert hass.states.get("sensor.home_heating_optimisation_trials").attributes["status"] == (
        "storage_read_only"
    )


async def test_journal_is_used_defensively(hass, controlled, sources):
    entry, c, calls = await live(hass, controlled)
    events = []

    class Journal:
        def record(self, kind, **data):
            events.append((kind, data))
            raise RuntimeError("journal broken")

    entry.runtime_data.journal = Journal()
    trial_id = await propose(hass)
    await call(hass, "approve_trial", trial_id=trial_id, confirm=True)
    await call(hass, "start_trial", trial_id=trial_id, confirm=True)
    await call(hass, "stop_trial", trial_id=trial_id)
    assert entry.runtime_data.trials.find(trial_id)["state"] == "stopped"
    assert [e[1]["data"]["to"] for e in events] == ["proposed", "approved", "running", "stopped"]
    assert all(kind == "trial" and data["scope"] == "study" for kind, data in events)
    assert all(data["room_id"] == "study" for _, data in events)
    assert events[-1][1]["origin"] == "user" and events[-1][1]["data"]["reason"] == "owner"
    assert "private" not in str(events)


async def test_save_failure_keeps_trial_in_memory_and_control_working(hass, controlled, sources):
    entry, c, calls, trial_id = await running(hass, controlled)
    trials = entry.runtime_data.trials
    with patch.object(trials.store.backend, "async_save", side_effect=OSError("disk full")):
        with pytest.raises(HomeAssistantError, match="could not be saved"):
            await call(hass, "stop_trial", trial_id=trial_id)
    assert trials.find(trial_id)["state"] == "stopped"
    assert c.rooms["study"].get_tunable("trust_k") == 0.8
    assert trials.store.status == "save_failed"
    await c.set_mode("study", "shadow")
    assert c.rooms["study"].mode == "shadow"
