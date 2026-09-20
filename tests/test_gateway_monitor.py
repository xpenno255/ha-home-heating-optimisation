"""Gateway monitoring alerts on silence and never touches control."""

from copy import deepcopy
from datetime import timedelta
from unittest.mock import patch

from homeassistant.components import persistent_notification as pn
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import async_fire_time_changed

from custom_components.home_heating_optimisation.const import DOMAIN, effective_config
from custom_components.home_heating_optimisation.control.migration import handover
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start
from tests.test_integration import setup

A = "binary_sensor.gateway_a_online"
B = "binary_sensor.gateway_b_online"
ISSUE_A = "gateway_unresponsive_gateway_a_online"
NOTE_A = f"{DOMAIN}_gateway_gateway_a_online"
SENSOR_A = f"sensor.{DOMAIN}_gateway_gateway_a_online"


def with_gateways(config, minutes=10, **extra):
    config = deepcopy(config)
    config["gateways"] = {
        "gateway_entities": [A, B],
        "gateway_offline_minutes": minutes,
        **extra,
    }
    return config


def gateways(hass, a="on", b="on"):
    hass.states.async_set(A, a)
    hass.states.async_set(B, b)


async def tick(hass, freezer, minutes=0, seconds=0):
    """Advance time, fire due timers and evaluate directly at the exact boundary."""
    freezer.tick(timedelta(minutes=minutes, seconds=seconds))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done()
    for entry in hass.config_entries.async_entries(DOMAIN):
        monitor = getattr(getattr(entry, "runtime_data", None), "gateways", None)
        if monitor is not None and not monitor.stopped:
            monitor.evaluate()
    await hass.async_block_till_done()


def issue(hass, issue_id=ISSUE_A):
    return ir.async_get(hass).async_get_issue(DOMAIN, issue_id)


def notifications(hass):
    return pn._async_get_or_create_notifications(hass)


def listen(hass):
    events = []
    for name in ("gateway_unresponsive", "gateway_recovered"):
        hass.bus.async_listen(f"{DOMAIN}_{name}", lambda e: events.append((e.event_type, e.data)))
    return events


async def test_alert_at_threshold_boundary_names_entity_and_pool(hass, config, sources, freezer):
    gateways(hass)
    entry = await setup(hass, with_gateways(config))
    monitor = entry.runtime_data.gateways
    events = listen(hass)
    await tick(hass, freezer, minutes=15)  # past the startup grace
    assert hass.states.get(SENSOR_A).state == "online"
    hass.states.async_set(A, "off")
    await hass.async_block_till_done()
    await tick(hass, freezer, minutes=9, seconds=59)
    assert issue(hass) is None and events == []
    assert hass.states.get(SENSOR_A).state == "unavailable"
    await tick(hass, freezer, seconds=1)
    created = issue(hass)
    assert created is not None and created.severity == ir.IssueSeverity.WARNING
    assert created.translation_placeholders["entity"] == A
    assert created.translation_placeholders["minutes"] == "10"
    note = notifications(hass)[NOTE_A]
    assert A in note["message"]
    assert "Other configured gateways online: 1 of 1" in note["message"]
    assert "cached copy" in note["message"]
    assert "does not show that any radio command failed" in note["message"]
    assert events == [
        (
            f"{DOMAIN}_gateway_unresponsive",
            {
                "entity_id": A,
                "since": monitor.states[A].offline_since.isoformat(),
                "duration_minutes": 10,
                "flap_count": 0,
            },
        )
    ]
    sensor = hass.states.get(SENSOR_A)
    assert sensor.state == "unresponsive"
    assert sensor.attributes["outage_count"] == 1
    assert sensor.attributes["entity_id"] == A
    assert sensor.attributes["since"] == monitor.states[A].offline_since.isoformat()
    assert sensor.attributes["last_change"] is not None
    assert monitor.report()["status"] == "unresponsive"
    assert monitor.report()["gateways"]["gateway_b_online"]["status"] == "online"


async def test_startup_grace_ignores_transient_offline_and_defers_real_outage(
    hass, config, sources, freezer
):
    gateways(hass, a="off")  # broker publishes a transient LWT while starting
    entry = await setup(hass, with_gateways(config))
    await tick(hass, freezer, seconds=30)
    hass.states.async_set(A, "on")
    await tick(hass, freezer, minutes=30)
    assert issue(hass) is None and NOTE_A not in notifications(hass)
    assert entry.runtime_data.gateways.states[A].outage_count == 0

    # A gateway down from the very start is only reported after the grace period.
    hass.states.async_set(B, "off")
    await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()
    await tick(hass, freezer, minutes=9)
    assert issue(hass, "gateway_unresponsive_gateway_b_online") is None
    await tick(hass, freezer, minutes=1)
    assert issue(hass, "gateway_unresponsive_gateway_b_online") is not None


async def test_short_threshold_still_has_two_minute_startup_grace(hass, config, sources, freezer):
    gateways(hass, a="off")
    await setup(hass, with_gateways(config, minutes=1))
    await tick(hass, freezer, minutes=1, seconds=30)
    assert issue(hass) is None
    await tick(hass, freezer, seconds=30)
    assert issue(hass) is not None


async def test_flapping_gateway_gets_one_alert_and_one_recovery_with_flap_count(
    hass, config, sources, freezer
):
    gateways(hass)
    entry = await setup(hass, with_gateways(config))
    events = listen(hass)
    await tick(hass, freezer, minutes=15)
    hass.states.async_set(A, "off")
    await tick(hass, freezer, minutes=10)
    assert len(events) == 1
    for _ in range(3):  # brief returns inside the settle window do not end the outage
        hass.states.async_set(A, "on")
        await tick(hass, freezer, seconds=30)
        hass.states.async_set(A, "off")
        await tick(hass, freezer, seconds=30)
    assert [e for e, _ in events] == [f"{DOMAIN}_gateway_unresponsive"]
    assert issue(hass) is not None
    assert hass.states.get(SENSOR_A).state == "unresponsive"
    hass.states.async_set(A, "on")
    await tick(hass, freezer, minutes=1)
    assert issue(hass) is not None  # not yet settled
    await tick(hass, freezer, minutes=1)
    assert issue(hass) is None
    assert NOTE_A not in notifications(hass)
    recovered = notifications(hass)[f"{NOTE_A}_recovered"]
    assert "flapped 4 times in the last hour" in recovered["message"]
    assert "not radio delivery" in recovered["message"]
    assert events[-1][0] == f"{DOMAIN}_gateway_recovered"
    assert events[-1][1]["flap_count"] == 4
    assert events[-1][1]["entity_id"] == A
    assert events[-1][1]["duration_minutes"] == 15
    assert len(events) == 2
    sensor = hass.states.get(SENSOR_A)
    assert sensor.state == "online"
    assert sensor.attributes["outage_count"] == 1
    assert sensor.attributes["flap_count"] == 4
    assert sensor.attributes["since"] is None
    assert entry.runtime_data.gateways.report()["status"] == "online"


async def test_unavailable_and_missing_entities_alert_like_off(hass, config, sources, freezer):
    gateways(hass, a="unavailable")
    hass.states.async_remove(B)
    await setup(hass, with_gateways(config))
    await tick(hass, freezer, minutes=10)
    assert "is unavailable in Home Assistant" in notifications(hass)[NOTE_A]["message"]
    note_b = notifications(hass)[f"{DOMAIN}_gateway_gateway_b_online"]
    assert "is missing in Home Assistant" in note_b["message"]
    assert "Other configured gateways online: 0 of 1" in note_b["message"]
    assert issue(hass) is not None
    assert issue(hass, "gateway_unresponsive_gateway_b_online") is not None
    hass.states.async_set(A, "off")
    await tick(hass, freezer, minutes=1)
    assert len([i for i in ir.async_get(hass).issues if i[0] == DOMAIN]) == 2


async def test_notifications_and_events_can_be_disabled(hass, config, sources, freezer):
    gateways(hass, a="off")
    await setup(hass, with_gateways(config, gateway_notify=False, gateway_events=False))
    events = listen(hass)
    await tick(hass, freezer, minutes=10)
    assert issue(hass) is not None
    assert NOTE_A not in notifications(hass) and events == []


async def test_unload_cancels_timers_and_reload_clears_state(hass, config, sources, freezer):
    gateways(hass)
    entry = await setup(hass, with_gateways(config))
    monitor = entry.runtime_data.gateways
    assert len(monitor._unsub) == 2
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert monitor.stopped and monitor._unsub == []
    hass.states.async_set(A, "off")
    await tick(hass, freezer, minutes=30)
    monitor.evaluate()
    assert issue(hass) is None and NOTE_A not in notifications(hass)
    assert hass.states.get(SENSOR_A).state == "unavailable"


async def test_unconfigured_monitor_is_inert(hass, config, sources, freezer):
    gateways(hass, a="off")
    entry = await setup(hass, config)
    monitor = entry.runtime_data.gateways
    assert not monitor.enabled and monitor._unsub == []
    await tick(hass, freezer, minutes=30)
    assert issue(hass) is None
    assert monitor.report()["status"] == "unconfigured"
    assert hass.states.get(SENSOR_A) is None
    assert entry.runtime_data.controls.report()["gateways"]["status"] == "unconfigured"
    assert entry.runtime_data.controls.report()["gateways"]["gateways"] == {}


async def test_outage_does_not_change_control_decisions_or_writes(
    hass, controlled, sources, freezer
):
    gateways(hass)
    entry, controls, calls = await start(hass, with_gateways(controlled))
    await handover(controls)
    await controls.set_mode("boiler", "auto")
    await controls.set_mode("study", "active")
    boiler = deepcopy(controls.boiler.data)
    room = deepcopy(controls.rooms["study"].data)
    await tick(hass, freezer, minutes=15)
    hass.states.async_set(A, "off")
    await tick(hass, freezer, minutes=10)
    assert issue(hass) is not None
    # Only the two controllers write, at unchanged values; the monitor issues no service calls.
    assert {kind for kind, _ in calls} == {"number", "ramses"}
    assert {data["value"] for kind, data in calls if kind == "number"} == {calls[0][1]["value"]}
    assert {data["entity_id"] for kind, data in calls if kind == "ramses"} == {"climate.study"}
    assert controls.boiler.data.flow_setpoint == boiler.flow_setpoint
    assert controls.rooms["study"].data.sent_target == room.sent_target
    report = controls.report()
    assert report["gateways"]["status"] == "unresponsive"
    assert report["gateways"]["gateways"]["gateway_a_online"]["outage_count"] == 1
    assert report["status"] == "active"
    journal_calls = []
    entry.runtime_data.journal = type(
        "Journal", (), {"record": lambda self, *a, **k: journal_calls.append((a, k))}
    )()
    hass.states.async_set(A, "on")
    await tick(hass, freezer, minutes=2)
    assert journal_calls[0][0] == ("gateway",)
    assert journal_calls[0][1]["origin"] == "source"
    assert journal_calls[0][1]["data"] == {
        "entity_id": A,
        "state": "recovered",
        "duration_minutes": 12,
    }


async def test_alert_failure_is_isolated_from_control(hass, controlled, sources, freezer):
    gateways(hass, a="off")
    _, controls, calls = await start(hass, with_gateways(controlled))
    with patch(
        "custom_components.home_heating_optimisation.gateway.monitor.ir.async_create_issue",
        side_effect=RuntimeError("registry unavailable"),
    ):
        await tick(hass, freezer, minutes=10)
    monitor = controls.heating.gateways
    assert monitor.last_error == "RuntimeError: registry unavailable"
    assert controls.report()["gateways"]["last_error"] == monitor.last_error
    await handover(controls)
    await controls.set_mode("boiler", "auto")
    await controls.set_mode("study", "active")
    assert {kind for kind, _ in calls} == {"number", "ramses"}


async def test_options_flow_stores_normalised_gateway_config(hass, config, sources):
    gateways(hass)
    entry = await setup(hass, config)
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    assert "gateways" in flow["menu_options"]
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "gateways"}
    )
    assert flow["step_id"] == "gateways"
    result = await hass.config_entries.options.async_configure(
        flow["flow_id"],
        {"gateway_entities": [A, B], "gateway_offline_minutes": 5, "gateway_notify": False},
    )
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    stored = effective_config(entry)
    assert stored["gateways"] == {
        "gateway_entities": [A, B],
        "gateway_offline_minutes": 5,
        "gateway_notify": False,
        "gateway_events": True,
    }
    assert stored["rooms"] == config["rooms"]  # other mappings are preserved
    monitor = entry.runtime_data.gateways
    assert monitor.enabled and monitor.threshold == timedelta(minutes=5)
    assert not monitor.notify
    assert hass.states.get(SENSOR_A).state == "online"

    # Clearing the list disables monitoring and removes the sensors.
    flow = await hass.config_entries.options.async_init(entry.entry_id)
    flow = await hass.config_entries.options.async_configure(
        flow["flow_id"], {"next_step_id": "gateways"}
    )
    result = await hass.config_entries.options.async_configure(flow["flow_id"], {})
    assert result["type"] == "create_entry"
    await hass.async_block_till_done()
    assert effective_config(entry)["gateways"]["gateway_entities"] == []
    assert not entry.runtime_data.gateways.enabled
