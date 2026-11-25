"""The rooms overview sensor behind the HHO rooms card."""

from types import SimpleNamespace

from homeassistant.core import State

from custom_components.home_heating_optimisation.card import URL, async_register_card
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.control.overview import (
    ENTITY_ID,
    RoomsOverviewSensor,
    cooling_step,
    spread_lift,
)
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start


async def test_overview_carries_each_room_and_the_boiler(hass, controlled, sources):  # noqa: F811
    hass.states.async_set(
        "climate.study",
        "auto",
        {"current_temperature": 18.0, "temperature": 20, "heat_demand": 0.35},
    )
    entry, c, calls = await start(hass, controlled)
    await handover(c)
    await c.set_mode("study", "active")
    settings = {"catchup_overdrive_k": 1.0, "overdrive_strength": 1.0, "overdrive_period_min": 10}
    c.rooms["study"]._overdrive_settings = lambda: settings
    await c.refresh()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state is not None and state.state == "1"
    [room] = state.attributes["rooms"]
    data = c.rooms["study"].data
    assert room["id"] == "study" and room["name"] == c.rooms["study"].room_name
    assert room["air"] == round(data.air_temp, 2) and room["target"] == round(data.air_setpoint, 2)
    assert room["overdrive"] == 1.0 and room["demand"] == 35 and room["calling"] is True
    assert room["preheat"] is None
    assert room["climate"] == c.rooms["study"]._config.get("primary_climate")
    assert set(state.attributes["house"]) >= {"outdoor", "flow", "flow_target", "return", "burner"}
    assert "hot_water" in state.attributes
    # Overdrive, not a spreading lift; nothing lowered the target; nobody is away.
    assert room["spread"] is None and room["cooling"] is None
    assert room["comfort"] == round(data.target_ot, 2) and room["away"] == 0.0
    assert state.attributes["house"]["away_status"] == "off"
    assert state.attributes["house"]["away_setback"] == 0.0


def reading(comfort, air, target, away=0.0, preheating=False, state="active"):
    return SimpleNamespace(
        state=state,
        target_ot=comfort,
        air_temp=air,
        air_setpoint=target,
        away_setback_k=away,
        preheating=preheating,
    )


def cool(readings, mark=None, last=None):
    for r in readings:
        mark, last = cooling_step(mark, last, r)
    return mark, last


def test_a_room_over_a_lowered_target_is_cooling_not_overshooting():
    evening = reading(20.0, 20.7, 20.6)
    # The schedule drops 2 K with the room still warm: cooling until it is within 0.15.
    assert cool([evening, reading(18.0, 20.7, 18.5)])[0] == "schedule"
    assert cool([evening, reading(18.0, 20.7, 18.5), reading(18.0, 18.7, 18.5)])[0] == "schedule"
    assert cool([evening, reading(18.0, 20.7, 18.5), reading(18.0, 18.6, 18.5)])[0] is None
    # The away setback is named as the reason; the deeper stage keeps it.
    out = [reading(19.2, 20.4, 19.8), reading(18.7, 20.4, 19.3, away=0.5)]
    assert cool(out)[0] == "away"
    assert cool([*out, reading(18.2, 19.6, 18.8, away=1.0)])[0] == "away"
    # The target back up (someone home, the next switchpoint) ends it, as does a pre-heat.
    assert cool([*out, reading(19.2, 20.4, 19.8)])[0] is None
    assert cool([*out, reading(18.7, 20.4, 19.3, away=0.5, preheating=True)])[0] is None


def test_an_overshoot_stays_an_overshoot():
    # Over the target with no drop, a room already down at the drop, and the small moves
    # of the weather correction (which leave the comfort target alone): none is cooling.
    assert cool([reading(19.2, 20.4, 19.8), reading(19.2, 20.5, 19.8)])[0] is None
    assert cool([reading(20.0, 18.4, 20.6), reading(18.0, 18.4, 18.5)])[0] is None
    assert cool([reading(19.2, 20.4, 19.8), reading(19.2, 20.4, 19.3)])[0] is None
    assert cool([reading(19.2, 20.4, 19.8), reading(19.0, 20.4, 19.6)])[0] is None
    # First sight of a room, nothing remembered: only an away setback already on explains it.
    assert cool([reading(19.2, 20.4, 19.8)])[0] is None
    assert cool([reading(18.2, 19.4, 18.6, away=1.0)])[0] == "away"
    assert cool([reading(18.2, 18.7, 18.6, away=1.0)])[0] is None
    # A cycle without a room reading (a sensor not yet heard after a restart) keeps the mark.
    silent = [reading(18.2, None, 18.6, away=1.0), reading(18.2, 19.4, 18.6, away=1.0)]
    assert cool(silent[:1])[0] == "away" and cool(silent)[0] == "away"
    late = [reading(20.0, 20.7, 20.6), reading(18.0, 20.7, 18.5), reading(18.0, None, 18.5)]
    assert cool(late)[0] == "schedule"
    assert cool([*late, reading(18.0, 18.6, 18.5)])[0] is None
    # No comfort target this cycle: nothing is learnt and nothing forgotten.
    assert cool([reading(None, 20.4, None)], "schedule", (20.0, 0.0)) == ("schedule", (20.0, 0.0))


def test_a_spreading_lift_stands_in_for_the_overdrive():
    def lift(spread, overdrive, mode="active", preheating=False):
        return spread_lift(
            SimpleNamespace(
                spread_k=spread, overdrive_k=overdrive, spread_mode=mode, preheating=preheating
            )
        )

    assert lift(0.6, 0.2) == 0.6 and lift(0.0, -0.5) == 0.0
    assert lift(0.3, 0.5) is None and lift(None, -0.5) is None
    assert lift(0.6, 0.2, mode="shadow") is None and lift(0.6, 0.2, preheating=True) is None


def test_cooling_marks_survive_a_restart():
    sensor = RoomsOverviewSensor(SimpleNamespace(entry=SimpleNamespace(entry_id="e")))
    rooms = [
        {"id": "living", "comfort": 18.0, "away": 0.0, "cooling": "schedule"},
        {"id": "hall", "comfort": 18.0, "away": 1.0, "cooling": None},
        {"id": "study", "state": "no_data"},
        "nonsense",
    ]
    sensor._restore(State(ENTITY_ID, "0", {"rooms": rooms}))
    assert sensor._cooling == {"living": "schedule"}
    assert sensor._comfort == {"living": (18.0, 0.0), "hall": (18.0, 1.0)}
    # Readings taken while the room is still starting up end nothing: on 25 Nov the Study's
    # first cycle after a restart read 17.7 against 17.6 before its real 18.3 against 17.7.
    starting = SimpleNamespace(data=reading(18.0, 18.6, 18.5, state="no_data"))
    assert sensor._cooling_now("living", starting) == "schedule"
    assert sensor._comfort["living"] == (18.0, 0.0)
    # Still warm after the restart: still cooling. A room without data keeps its mark.
    living = SimpleNamespace(data=reading(18.0, 19.4, 18.5))
    assert sensor._cooling_now("living", living) == "schedule"
    assert sensor._cooling_now("living", SimpleNamespace(data=None)) == "schedule"
    assert sensor._cooling_now("living", SimpleNamespace(data=reading(18.0, 18.6, 18.5))) is None
    # The setback ended while Home Assistant was down: the target is back up, nothing to mark.
    assert sensor._cooling_now("hall", SimpleNamespace(data=reading(19.0, 19.9, 19.1))) is None
    sensor._restore(None)
    sensor._restore(State(ENTITY_ID, "0", {"rooms": "nonsense"}))


async def test_card_registration_skips_without_a_frontend(hass):
    await async_register_card(hass)
    assert URL.endswith("hho-rooms-card.js")
    assert not hass.data.get("home_heating_optimisation_card_registered")


async def test_hot_water_view_reads_the_cylinder_and_cloud_schedule(hass, controlled, sources):  # noqa: F811
    from custom_components.home_heating_optimisation.control.overview import hot_water_view

    entry, c, calls = await start(hass, controlled)
    hass.states.async_set("sensor.cyl", "47.24")
    hass.states.async_set(
        "water_heater.hw", "auto", {"temperature": 50, "mode": {"mode": "follow_schedule"}}
    )
    hass.states.async_set(
        "water_heater.cloud",
        "auto",
        {
            "status": {
                "setpoints": {
                    "this_sp_state": "Off",
                    "next_sp_from": "2026-11-03T04:30:00+00:00",
                    "next_sp_state": "On",
                }
            }
        },
    )
    c.heating.dhw_cutoff.cfg = {
        "water_heater_entity": "water_heater.hw",
        "cylinder_temp_entity": "sensor.cyl",
        "cloud_entity": "water_heater.cloud",
    }
    view = hot_water_view(c)
    assert view["temp"] == 47.2 and view["target"] == 50.0 and view["mode"] == "follow_schedule"
    assert view["scheduled_on"] is False and view["next_state"] == "On"
    assert view["entity"] == "water_heater.hw"
