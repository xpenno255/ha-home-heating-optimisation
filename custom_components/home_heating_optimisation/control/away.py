"""Away setback: lower every room's target a little while everyone is out.

Nobody is at home when every tracked person is somewhere else; a person whose whereabouts
are unknown counts as at home. The setback comes in two stages: nothing for the first
``start_minutes`` (short trips and location blips), then ``first_k``, then ``deep_k`` once
the house has been empty for ``deep_after_minutes``. It ends the moment anyone is home.

Phones report too few fixes on the way home to see a return coming, so the one usable
early sign is a person leaving a work zone: the setback then lifts at once so the house
can recover, and comes back if nobody has arrived after ``resume_minutes``.

Visitor mode suppresses it: guests are in the house while the owners are out.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime

from homeassistant.util import dt as dt_util

SECTION = "away"
DEFAULTS = {
    "enabled": False,
    "people": [],
    "work_zones": [],
    # Continuous absence before any setback: short trips and location blips change nothing.
    "start_minutes": 30,
    "first_k": 0.5,
    # After this long away the setback deepens.
    "deep_after_minutes": 120,
    "deep_k": 1.0,
    # Someone left work but nobody came home within this time: the setback resumes.
    "resume_minutes": 90,
}
LIMITS = {
    "start_minutes": (5, 240),
    "first_k": (0.0, 2.0),
    "deep_after_minutes": (30, 720),
    "deep_k": (0.0, 2.0),
    "resume_minutes": (15, 240),
}
INTEGERS = ("start_minutes", "deep_after_minutes", "resume_minutes")
HOME, WORK, OUT = "home", "work", "out"
UNKNOWN = (None, "", "unknown", "unavailable")


def _entities(value, domain):
    if not isinstance(value, (list, tuple)):
        return []
    return [e for e in value if isinstance(e, str) and e.startswith(domain + ".")]


def away_config(config):
    """The away section with defaults; anything out of range falls back to its default."""
    raw = config.get(SECTION) if isinstance(config, dict) else None
    raw = raw if isinstance(raw, dict) else {}
    out = {**DEFAULTS, **{k: raw[k] for k in DEFAULTS if k in raw}}
    out["enabled"] = out["enabled"] is True
    out["people"] = _entities(out["people"], "person")
    out["work_zones"] = _entities(out["work_zones"], "zone")
    for key, (low, high) in LIMITS.items():
        try:
            value = float(out[key])
            if not math.isfinite(value) or not low <= value <= high:
                raise ValueError(key)
        except TypeError, ValueError:
            value = DEFAULTS[key]
        out[key] = int(value) if key in INTEGERS else value
    if out["deep_k"] < out["first_k"] or out["deep_after_minutes"] <= out["start_minutes"]:
        for key in ("start_minutes", "first_k", "deep_after_minutes", "deep_k"):
            out[key] = DEFAULTS[key]
    return out


def validation_error(values):
    """An error key for values that cannot be saved, or None."""
    for key, (low, high) in LIMITS.items():
        value = values.get(key)
        if (
            not isinstance(value, (int, float))
            or isinstance(value, bool)
            or not math.isfinite(value)
            or not low <= value <= high
        ):
            return "away_invalid"
    if values["deep_k"] < values["first_k"]:
        return "away_invalid"
    if values["deep_after_minutes"] <= values["start_minutes"]:
        return "away_invalid"
    people, zones = values.get("people") or [], values.get("work_zones") or []
    if len(_entities(people, "person")) != len(people):
        return "away_invalid"
    if len(_entities(zones, "zone")) != len(zones):
        return "away_invalid"
    if values.get("enabled") and not people:
        return "away_needs_people"
    return None


@dataclass(frozen=True)
class AwayParams:
    start_minutes: float = 30
    first_k: float = 0.5
    deep_after_minutes: float = 120
    deep_k: float = 1.0
    resume_minutes: float = 90


@dataclass(frozen=True)
class AwayState:
    """Since when the house has been empty, a lift for someone heading home, who was at work."""

    away_since: datetime | None = None
    heading_home_since: datetime | None = None
    at_work: tuple[str, ...] = ()


def away_step(state: AwayState, people, now: datetime, params: AwayParams):
    """``(next state, setback K, status)`` for ``people`` = ``[(name, home / work / out)]``.

    Status is one of ``home``, ``waiting`` (out, but not long enough), ``away``, ``deep``
    and ``heading_home``.
    """
    if not people or any(where == HOME for _, where in people):
        return AwayState(), 0.0, "home"
    since = state.away_since or now
    minutes = (now - since).total_seconds() / 60
    at_work = tuple(sorted(name for name, where in people if where == WORK))
    heading = state.heading_home_since
    if minutes >= params.start_minutes and any(name not in at_work for name in state.at_work):
        heading = now
    if heading is not None and (now - heading).total_seconds() / 60 >= params.resume_minutes:
        heading = None
    new = AwayState(since, heading, at_work)
    if minutes < params.start_minutes:
        return new, 0.0, "waiting"
    if heading is not None:
        return new, 0.0, "heading_home"
    if minutes >= params.deep_after_minutes:
        return new, round(params.deep_k, 1), "deep"
    return new, round(params.first_k, 1), "away"


def dump_state(state: AwayState) -> dict:
    return {
        "away_since": state.away_since.isoformat() if state.away_since else None,
        "heading_home_since": (
            state.heading_home_since.isoformat() if state.heading_home_since else None
        ),
        "at_work": list(state.at_work),
    }


def load_state(raw) -> AwayState:
    """The saved state; anything unreadable starts from an occupied house."""
    try:
        times = []
        for key in ("away_since", "heading_home_since"):
            value = dt_util.parse_datetime(str(raw[key])) if raw.get(key) else None
            times.append(dt_util.as_utc(value) if value is not None else None)
        return AwayState(times[0], times[1], tuple(str(n) for n in raw.get("at_work") or ()))
    except Exception:  # noqa: BLE001 - absent or malformed
        return AwayState()


class AwayTracker:
    """Follows the tracked people and answers how much setback applies right now."""

    def __init__(self, hass, store, config, visitor=None):
        self.hass = hass
        self._store = store
        self._config = config
        self._visitor = visitor or (lambda _now: False)
        self._state = load_state(store.get("state") or {})
        self.setback_k = 0.0
        self.status = "off"
        self._stepped_at: datetime | None = None

    def _where(self, entity, work_names):
        state = self.hass.states.get(entity)
        value = state.state if state is not None else None
        if value in UNKNOWN or value == "home":
            return HOME  # not knowing where someone is counts as at home
        return WORK if value.lower() in work_names else OUT

    def _work_names(self, zones):
        names = set()
        for zone in zones:
            state = self.hass.states.get(zone)
            if state is not None and state.name:
                names.add(state.name.lower())
        return names

    def step(self, now: datetime) -> float:
        """Advance to ``now`` (at most once a second) and return the setback in K."""
        if self._stepped_at is not None and abs((now - self._stepped_at).total_seconds()) < 1:
            return self.setback_k
        self._stepped_at = now
        cfg = self._config()
        before = self._state
        if not cfg["enabled"] or not cfg["people"]:
            self._state, self.setback_k, self.status = AwayState(), 0.0, "off"
        else:
            work = self._work_names(cfg["work_zones"])
            people = [(p, self._where(p, work)) for p in cfg["people"]]
            params = AwayParams(
                cfg["start_minutes"],
                cfg["first_k"],
                cfg["deep_after_minutes"],
                cfg["deep_k"],
                cfg["resume_minutes"],
            )
            self._state, self.setback_k, self.status = away_step(before, people, now, params)
            if self.setback_k and self._visitor(now):
                self.setback_k, self.status = 0.0, "visitor"
        if self._state != before:
            self._store.set("state", dump_state(self._state))
            self.hass.async_create_task(self._save(), "hho_away_save")
        return self.setback_k

    async def _save(self):
        try:
            await self._store.async_save()
        except Exception:  # noqa: BLE001 - the next change tries again
            pass

    def report(self) -> dict:
        return {
            "status": self.status,
            "setback_k": self.setback_k,
            "away_since": self._state.away_since.isoformat() if self._state.away_since else None,
            "heading_home_since": (
                self._state.heading_home_since.isoformat()
                if self._state.heading_home_since
                else None
            ),
        }
