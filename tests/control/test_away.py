"""Away setback: room targets come down a little while everyone is out."""

from copy import deepcopy
from datetime import datetime, timedelta, timezone

from homeassistant.util import dt as dt_util

from custom_components.home_heating_optimisation.control.away import (
    HOME,
    OUT,
    WORK,
    AwayParams,
    AwayState,
    away_config,
    away_step,
    dump_state,
    load_state,
    validation_error,
)
from custom_components.home_heating_optimisation.control.migration import handover
from custom_components.home_heating_optimisation.scorecard.core import (
    add_away,
    empty_day,
    finalise,
)
from tests.control.test_runtime import controlled as controlled  # noqa: F401 - fixture
from tests.control.test_runtime import start

T0 = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
P = AwayParams()


def run(people_at, state=None):
    """Step through ``[(minutes from T0, [(name, where)])]``; returns the last result."""
    state, result = state or AwayState(), None
    for minutes, people in people_at:
        result = away_step(state, people, T0 + timedelta(minutes=minutes), P)
        state = result[0]
    return result


def both(where_a, where_b=None):
    return [("a", where_a), ("b", where_b or where_a)]


# -- rules --------------------------------------------------------------------


def test_nothing_while_anyone_is_home():
    assert away_step(AwayState(), both(HOME, OUT), T0, P) == (AwayState(), 0.0, "home")
    assert away_step(AwayState(), [], T0, P)[1:] == (0.0, "home")


def test_the_setback_comes_in_two_stages():
    assert run([(0, both(OUT)), (29, both(OUT))])[1:] == (0.0, "waiting")
    assert run([(0, both(OUT)), (30, both(OUT))])[1:] == (0.5, "away")
    assert run([(0, both(OUT)), (119, both(OUT))])[1:] == (0.5, "away")
    assert run([(0, both(OUT)), (120, both(OUT))])[1:] == (1.0, "deep")


def test_a_short_trip_or_a_location_blip_changes_nothing():
    # Out for 20 minutes, home for a moment, out again: the clock starts afresh.
    state, k, status = run(
        [(0, both(OUT)), (20, both(HOME, OUT)), (21, both(OUT)), (45, both(OUT))]
    )
    assert (k, status) == (0.0, "waiting")
    assert state.away_since == T0 + timedelta(minutes=21)


def test_the_first_arrival_ends_it_at_once():
    assert run([(0, both(OUT)), (200, both(OUT)), (201, both(HOME, OUT))])[1:] == (0.0, "home")


def test_leaving_work_lifts_the_setback_until_someone_is_home():
    steps = [(0, both(WORK, OUT)), (240, both(WORK, OUT))]
    assert run(steps)[1:] == (1.0, "deep")
    steps.append((241, both(OUT)))  # a leaves the work zone
    state, k, status = run(steps)
    assert (k, status) == (0.0, "heading_home")
    assert state.heading_home_since == T0 + timedelta(minutes=241)
    # Still lifted an hour later; home ends it.
    assert run([*steps, (300, both(OUT))])[1:] == (0.0, "heading_home")
    assert run([*steps, (300, both(HOME, OUT))])[1:] == (0.0, "home")


def test_nobody_home_after_leaving_work_brings_it_back():
    steps = [(0, both(WORK, OUT)), (240, both(WORK, OUT)), (241, both(OUT)), (331, both(OUT))]
    assert run(steps)[1:] == (1.0, "deep")


def test_leaving_work_before_any_setback_does_nothing():
    assert run([(0, both(WORK, OUT)), (10, both(OUT)), (40, both(OUT))])[1:] == (0.5, "away")


def test_state_round_trips_and_bad_data_starts_at_home():
    state = AwayState(T0, T0 + timedelta(hours=3), ("a",))
    assert load_state(dump_state(state)) == state
    assert load_state({}) == AwayState() and load_state({"away_since": "nonsense"}) == AwayState()


# -- settings -----------------------------------------------------------------


def test_config_defaults_off_and_validated():
    cfg = away_config({})
    assert cfg["enabled"] is False and cfg["people"] == []
    assert (cfg["start_minutes"], cfg["first_k"], cfg["deep_after_minutes"], cfg["deep_k"]) == (
        30,
        0.5,
        120,
        1.0,
    )
    # Out of range or inconsistent values fall back; non-person entities are dropped.
    cfg = away_config({"away": {"first_k": 9, "people": ["person.a", "sensor.x"], "enabled": 1}})
    assert cfg["first_k"] == 0.5 and cfg["people"] == ["person.a"] and cfg["enabled"] is False
    cfg = away_config({"away": {"first_k": 1.5, "deep_k": 1.0}})
    assert (cfg["first_k"], cfg["deep_k"]) == (0.5, 1.0)
    good = {**away_config({}), "enabled": True, "people": ["person.a"]}
    assert validation_error(good) is None
    assert validation_error({**good, "people": []}) == "away_needs_people"
    assert validation_error({**good, "deep_k": 0.2}) == "away_invalid"
    assert validation_error({**good, "deep_after_minutes": 30}) == "away_invalid"
    assert validation_error({**good, "work_zones": ["person.a"]}) == "away_invalid"
    assert validation_error({**good, "first_k": True}) == "away_invalid"


# -- rooms ----------------------------------------------------------------------


def with_away(config, **away):
    config = deepcopy(config)
    config["away"] = {"enabled": True, "people": ["person.a", "person.b"], **away}
    return config


def people(hass, a, b):
    hass.states.async_set("person.a", a)
    hass.states.async_set("person.b", b)


async def test_rooms_come_down_while_everyone_is_out_and_back_on_return(  # noqa: F811
    hass, controlled, sources, freezer
):
    people(hass, "home", "home")
    hass.states.async_set("zone.office", "0", {"friendly_name": "Office"})
    entry, c, calls = await start(hass, with_away(controlled, work_zones=["zone.office"]))
    await handover(c)
    await c.set_mode("study", "active")
    room = c.rooms["study"]
    await c.refresh()
    normal = room.data.target_ot
    assert room.data.away_setback_k == 0.0 and c.report()["away"]["status"] == "home"
    # Both out: nothing for half an hour, then 0.5, then 1.0 after two hours.
    people(hass, "not_home", "Office")
    freezer.tick(timedelta(seconds=5))
    await c.refresh()
    freezer.tick(timedelta(minutes=29))
    await c.refresh()
    assert room.data.target_ot == normal and room.data.away_status == "waiting"
    freezer.tick(timedelta(minutes=2))
    await c.refresh()
    assert room.data.away_setback_k == 0.5 and room.data.target_ot == normal - 0.5
    freezer.tick(timedelta(minutes=90))
    await c.refresh()
    assert room.data.away_setback_k == 1.0 and room.data.target_ot == normal - 1.0
    assert c.report()["away"]["status"] == "deep"
    # Leaving the office lifts it before anyone is home.
    people(hass, "not_home", "not_home")
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.target_ot == normal and room.data.away_status == "heading_home"
    people(hass, "not_home", "home")
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert room.data.target_ot == normal and c.report()["away"]["status"] == "home"


async def test_unknown_whereabouts_count_as_home(hass, controlled, sources, freezer):  # noqa: F811
    people(hass, "not_home", "unknown")
    entry, c, calls = await start(hass, with_away(controlled))
    await handover(c)
    await c.set_mode("study", "active")
    await c.refresh()
    freezer.tick(timedelta(hours=3))
    await c.refresh()
    assert c.rooms["study"].data.away_setback_k == 0.0 and c.away.status == "home"


async def test_visitor_mode_and_off_suppress_it(hass, controlled, sources, freezer):  # noqa: F811
    people(hass, "not_home", "not_home")
    entry, c, calls = await start(hass, with_away(controlled))
    await handover(c)
    await c.set_mode("study", "active")
    await c.refresh()
    freezer.tick(timedelta(minutes=45))
    await c.refresh()
    assert c.rooms["study"].data.away_setback_k == 0.5
    c.away._visitor = lambda _now: True
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert c.rooms["study"].data.away_setback_k == 0.0 and c.away.status == "visitor"
    c.away._visitor = lambda _now: False
    c.away._config = lambda: away_config({})
    freezer.tick(timedelta(minutes=1))
    await c.refresh()
    assert c.rooms["study"].data.away_setback_k == 0.0 and c.away.status == "off"


async def test_the_away_clock_survives_a_restart(hass, controlled, sources, freezer):  # noqa: F811
    people(hass, "not_home", "not_home")
    entry, c, calls = await start(hass, with_away(controlled))
    await handover(c)
    await c.set_mode("study", "active")
    await c.refresh()
    freezer.tick(timedelta(minutes=45))
    await c.refresh()
    await hass.async_block_till_done()
    saved = c.away._store.get("state")
    assert load_state(saved).away_since == c.away._state.away_since is not None
    assert dt_util.utcnow() - load_state(saved).away_since >= timedelta(minutes=45)


# -- scorecard ----------------------------------------------------------------


def test_scorecard_counts_away_hours():
    day = empty_day("2026-11-18")
    add_away(day, 60, 0.5)
    add_away(day, 120, 1.0)
    add_away(day, 30, 0.0)  # no setback: not counted
    row = finalise(day)
    assert row["away_hours"] == 3.0 and row["away_setback_k"] == 0.83
    assert finalise(empty_day("2026-11-19"))["away_hours"] == 0.0
