"""Controller producers record decisions, commands, readbacks, holds and modes."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch

from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.const import DOMAIN
from custom_components.home_heating_optimisation.control.comfort.core.policy import (
    Action,
    Decision,
    State,
)
from custom_components.home_heating_optimisation.control.migration import handover, rollback
from tests.control.test_runtime import controlled as controlled  # noqa: F401
from tests.control.test_runtime import start

DECIDE = "custom_components.home_heating_optimisation.control.comfort.coordinator.decide"


def kinds(journal, **filters):
    return [e["kind"] for e in journal.events(**filters)]


async def test_shadow_cycle_records_decisions_without_commands(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await c.refresh()
    room = journal.events(room_id="study")
    assert "decision" in [e["kind"] for e in room]
    assert "schedule_change" in [e["kind"] for e in room]
    decision = next(e for e in room if e["kind"] == "decision")
    assert decision["scope"] == "study" and decision["origin"] == "controller"
    assert decision["data"]["outcome"] in ("shadow", "no_action")
    assert decision["provenance"]["model_version"] == "steady_state_ot_v2"
    assert decision["provenance"]["control_schema"] == 1
    assert "schedule_source" in decision["provenance"]
    boiler = [e for e in journal.events() if e["scope"] == "boiler"]
    assert any(e["kind"] == "decision" and e["data"]["outcome"] == "shadow" for e in boiler)
    assert not any(e["kind"].startswith("command") for e in journal.events())
    assert calls == []
    # Repeated unchanged cycles do not add decision events.
    count = len(journal.events(kinds=["decision"]))
    await c.refresh()
    await c.refresh()
    assert len(journal.events(kinds=["decision"])) == count


async def test_handover_mode_change_and_commands_are_journalled(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await handover(c)
    handovers = journal.events(kinds=["handover"])
    assert handovers[-1]["origin"] == "service"
    assert handovers[-1]["data"]["outcome"] == "handover_complete_shadow"
    await c.set_mode("boiler", "auto")
    await c.set_mode("study", "active")
    modes = journal.events(kinds=["mode_change"])
    assert [(e["scope"], e["data"]["from"], e["data"]["to"]) for e in modes] == [
        ("boiler", "shadow", "auto"),
        ("study", "shadow", "active"),
    ]
    assert modes[1]["room_id"] == "study" and modes[0]["room_id"] is None
    for scope in ("boiler", "study"):
        events = [e for e in journal.events() if e["scope"] == scope]
        sequence = [e["kind"] for e in events]
        requested = sequence.index("command_requested")
        assert sequence[requested + 1 : requested + 3] == ["command_sent", "command_result"]
        sent = events[requested + 1]
        assert sent["data"]["outcome"] == "service_succeeded"
        assert sent["data"]["setpoint"] is not None
        assert "reason" in sent["data"]
    room_sent = journal.events(kinds=["command_sent"], room_id="study")[-1]
    assert room_sent["data"]["service"] == "ramses_cc.set_zone_mode"
    assert room_sent["data"]["action"] == "write"
    readbacks = journal.events(kinds=["readback"], room_id="study")
    assert readbacks[-1]["origin"] == "source"
    assert readbacks[-1]["data"]["status"] == "readback_no_echo"
    assert {kind for kind, _ in calls} == {"number", "ramses"}
    await rollback(c)
    assert journal.events(kinds=["rollback"])[-1]["data"]["outcome"] == (
        "legacy_restored_consolidated_shadow"
    )


async def test_failed_and_blocked_commands_record_explicit_outcomes(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal

    async def fail(call):
        raise RuntimeError("bus down")

    hass.services.async_register("number", "set_value", fail)
    await handover(c)
    await c.set_mode("boiler", "auto")
    results = [e for e in journal.events(kinds=["command_result"]) if e["scope"] == "boiler"]
    assert results[-1]["data"]["outcome"] == "service_failed"
    assert results[-1]["data"]["error"] == "RuntimeError"
    assert "command_sent" not in [e["kind"] for e in journal.events() if e["scope"] == "boiler"]
    # A write refused by the ownership guard is journalled as blocked, not lost.
    c.ready = False
    await c.boiler.async_refresh()
    results = [e for e in journal.events(kinds=["command_result"]) if e["scope"] == "boiler"]
    assert results[-1]["data"]["outcome"] == "blocked"


async def test_readback_transitions_include_timeout_and_confirmation(
    hass, controlled, sources, freezer
):
    entry, c, _ = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    target = room.data.sent_target
    freezer.tick(timedelta(minutes=3))
    await room.async_refresh()
    timed_out = journal.events(kinds=["readback"], room_id="study")[-1]
    assert timed_out["data"]["timed_out"] is True
    assert timed_out["data"]["status"] == "readback_no_echo"
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set(
        "climate.study", "auto", {"current_temperature": 18, "temperature": target}
    )
    await room.async_refresh()
    confirmed = journal.events(kinds=["readback"], room_id="study")[-1]
    assert confirmed["data"]["status"] == "confirmed"
    assert confirmed["data"]["confirmed_target"] == target
    assert confirmed["data"]["outcome"] == "confirmed"


async def test_short_lived_command_and_reversion_between_samples_are_recorded(
    hass, controlled, sources, freezer
):
    """A command sent and reverted within 60 s cannot appear in 5-minute samples."""
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    old_target = room.data.pending_target
    old_until = dt_util.utcnow() + timedelta(hours=2)
    freezer.tick(timedelta(seconds=1))
    ramses = {"mode": "temporary_override", "setpoint": old_target, "until": old_until.isoformat()}
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 18, "temperature": old_target, "mode": ramses},
    )
    await room.async_refresh()
    assert room.data.confirmed_target == old_target
    new_target = old_target + 0.1
    new_until = dt_util.utcnow() + timedelta(hours=2, minutes=15)

    async def optimistic(call):
        calls.append(("ramses", dict(call.data)))
        hass.states.async_set(
            "climate.study",
            "auto",
            {
                "current_temperature": 18,
                "temperature": call.data["setpoint"],
                "mode": {
                    "mode": "temporary_override",
                    "setpoint": call.data["setpoint"],
                    "until": new_until.isoformat(),
                },
            },
            context=call.context,
        )

    hass.services.async_register("ramses_cc", "set_zone_mode", optimistic)
    memory = replace(
        room._memory(), last_written_setpoint=new_target, last_written_at=dt_util.utcnow()
    )
    forced = Decision(State.ACTIVE, Action.WRITE, new_target, "forced change", memory)
    start_count = len(journal.events())
    sent_at = dt_util.utcnow().timestamp()
    with patch(DECIDE, return_value=forced):
        await room.async_refresh()
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set("climate.study", "auto", hass.states.get("climate.study").attributes)
    room._refresh_readback(dt_util.utcnow())
    assert room._confirmed_target == new_target
    freezer.tick(timedelta(seconds=10))
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 18, "temperature": old_target, "mode": ramses},
    )
    room._refresh_readback(dt_util.utcnow())
    assert room._readback_status == "readback_reverted"
    assert len(journal.events()) > start_count
    sequence = [e["kind"] for e in journal.events(room_id="study", since=sent_at)]
    assert "command_sent" in sequence
    statuses = [
        e["data"]["status"]
        for e in journal.events(kinds=["readback"], room_id="study", since=sent_at)
    ]
    assert "confirmed" in statuses and statuses[-1] == "readback_reverted"
    assert dt_util.utcnow().timestamp() - sent_at < 60


async def test_manual_override_set_and_clear_are_journalled(hass, controlled, sources, freezer):
    entry, c, _ = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    freezer.tick(timedelta(seconds=1))
    # A dial change away from both schedule and our write is a manual override.
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18, "temperature": 25})
    await room.async_refresh()
    assert room.data.state == "manual"
    manual = journal.events(kinds=["manual_override"], room_id="study")
    assert manual[-1]["data"]["status"] == "set"
    assert manual[-1]["origin"] == "user"
    assert manual[-1]["data"]["held_setpoint"] == 25
    assert manual[-1]["data"]["outcome"] == "holding"
    count = len(manual)
    await room.async_refresh()
    assert len(journal.events(kinds=["manual_override"], room_id="study")) == count
    # Returning to the schedule clears the hold and is journalled as a release.
    freezer.tick(timedelta(seconds=1))
    hass.states.async_set("climate.study", "auto", {"current_temperature": 18, "temperature": 20})
    await room.async_refresh()
    cleared = journal.events(kinds=["manual_override"], room_id="study")[-1]
    assert cleared["data"]["status"] == "cleared" and cleared["origin"] == "controller"


async def test_boiler_manual_hold_and_dhw_transitions_are_journalled(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal
    await handover(c)
    await c.set_mode("boiler", "auto")
    hass.states.async_set("sensor.hw", 100)
    await c.boiler.async_refresh()
    assert c.boiler.data.dhw_active
    dhw = [e for e in journal.events(kinds=["decision"]) if e["data"].get("subject") == "dhw"]
    assert dhw[-1]["data"]["dhw_active"] is True and dhw[-1]["origin"] == "source"
    # A live setpoint that differs from our write and the dial is a manual hold.
    written = c.boiler._hub.last_written_setpoint
    state = hass.states.get("number.flow_setpoint")
    hass.states.async_set("number.flow_setpoint", written - 7, state.attributes)
    await c.boiler.async_refresh()
    assert c.boiler.data.manual_hold_active
    hold = [e for e in journal.events(kinds=["manual_override"]) if e["scope"] == "boiler"]
    assert hold[-1]["data"]["status"] == "set" and hold[-1]["origin"] == "user"
    assert hold[-1]["data"]["live_setpoint"] == written - 7


async def test_adjustment_note_is_mirrored_with_private_text(hass, controlled, sources):
    controlled["analytics_enabled"] = True
    with patch(
        "custom_components.home_heating_optimisation.analytics.coordinator.async_backfill",
        return_value=([], False, "complete"),
    ):
        entry, c, _ = await start(hass, controlled)
    await hass.services.async_call(
        DOMAIN,
        "record_adjustment",
        {"note": "Bled the radiator", "kind": "lockshield", "room_id": "study"},
        blocking=True,
    )
    journal = entry.runtime_data.journal
    note = journal.events(kinds=["adjustment_note"])[-1]
    assert note["origin"] == "user" and note["room_id"] == "study"
    assert note["data"]["private_note"] == "Bled the radiator"
    assert note["data"]["adjustment_kind"] == "lockshield"
    assert "Bled" not in str(journal.export())
    assert "Bled" not in str(hass.states.async_all())


async def test_journal_failure_does_not_stop_control(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    journal = entry.runtime_data.journal
    with patch.object(journal.store, "append", side_effect=RuntimeError("journal broken")):
        await handover(c)
        await c.set_mode("boiler", "auto")
        await c.set_mode("study", "active")
    assert {kind for kind, _ in calls} == {"number", "ramses"}
    assert c.settings.get("ownership") == "ready"
    # A hook raising inside the producer is swallowed too.
    with patch.object(journal, "record", side_effect=RuntimeError("record broken")):
        calls.clear()
        await c.refresh()
    assert any(kind == "number" for kind, _ in calls)
    with patch.object(journal.store.backend, "async_save", side_effect=OSError("disk full")):
        await journal.flush()
    assert journal.status == "save_failed"
    calls.clear()
    await c.refresh()
    assert any(kind == "number" for kind, _ in calls)


async def test_controllers_without_journal_still_run(hass, controlled, sources):
    entry, c, calls = await start(hass, controlled)
    c.boiler.journal = None
    c.rooms["study"].journal = None
    entry.runtime_data.journal = None
    await handover(c)
    await c.set_mode("boiler", "auto")
    await c.set_mode("study", "active")
    assert {kind for kind, _ in calls} == {"number", "ramses"}
