"""Learn each room's warm-up pace and evening coast rate (pure; runs in the executor).

Warm-up events come from the observations themselves: the target rises by at least half a
kelvin while the room is at least half a kelvin short, and the event ends when the room
arrives and stays there. HHO nudges targets by 0.1-0.3 K every few minutes, which would
censor most analytics episodes, so those small changes are simply ignored here. Events
are kept in the integration's store (newest 60 per room) so learning outlasts the
analytics window. Cool-downs are demand-off stretches, used only for the stop estimate.
"""

import statistics
from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo

from ..analytics.const import MAX_GAP_SECONDS
from .model import RoomModel, fit_model

# A rise of at least this in the target (over RISE_WINDOW, so a schedule step followed a
# minute later by HHO's own offset counts once) starts an event.
RISE_K = 0.5
RISE_WINDOW_SECONDS = 15 * 60
# The room must be short of the arrival line (goal - tolerance) by at least this.
SHORTFALL_K = 0.1
ABANDON_DROP_K = 0.3
HOLD_SECONDS = 600
# The room has responded once it reads this far above where the event began.
RESPONSE_K = 0.1
MAX_EVENT_SECONDS = 4 * 3600
KEEP_EVENTS = 60
# Radiators keep giving heat for a while after demand ends; those minutes are skipped.
AFTERHEAT_SECONDS = 20 * 60
MIN_COOLDOWN_SECONDS = 45 * 60
MIN_REPORTS = 3
# Cool-downs that start in the evening, when the house is not warmed by sun or occupants.
EVENING_HOURS = range(17, 24)
MAX_COAST_RATE = -0.02
HIGH_EVENTS, MEDIUM_EVENTS, HIGH_LOO_MIN = 8, 4, 15.0


@dataclass(frozen=True)
class RoomFit:
    model: RoomModel | None
    status: str  # "ready" | "learning"
    confidence: str  # "high" | "medium" | "low" | "none"
    events: int
    cooldowns: int
    coast_rate: float | None
    coast_reason: str | None
    last_events: list = field(default_factory=list)


def _valid(z, t):
    return (
        isinstance(z.get("temperature"), (int, float))
        and isinstance(z.get("target"), (int, float))
        and z.get("valid_until", t) >= t
    )


def _mean(values):
    values = [v for v in values if isinstance(v, (int, float))]
    return statistics.fmean(values) if values else None


def warmup_events(observations, zone, tolerance=0.3, tz="UTC"):
    """Completed warm-ups of one zone, oldest first.

    Start: target up >= 0.5 K within 15 minutes with the room at least 0.1 K below the
    arrival line (goal - tolerance). A further rise within those 15 minutes raises the goal
    (live 13 Oct: 18.0 -> 19.0 schedule, then 19.3 a minute later). Later target changes
    are ignored unless the target drops 0.3 K below the goal (abandoned),
    as are a data gap over 30 minutes or an event running past 4 hours. Success: the room
    within ``tolerance`` of the goal for 10 minutes; minutes run to the first of those.
    """
    zone_tz = ZoneInfo(tz)
    events, current, previous, recent = [], None, None, []
    for obs in observations:
        t = obs["time"]
        z = obs["zones"].get(zone, {})
        if previous is not None and t - previous["t"] > MAX_GAP_SECONDS:
            current = previous = None
            recent = []
        if not _valid(z, t):
            current = previous = None
            recent = []
            continue
        temp, target = z["temperature"], z["target"]
        recent = [(rt, rv) for rt, rv in recent if t - rt <= RISE_WINDOW_SECONDS]
        ended = False
        if (
            current is not None
            and target > current["goal"]
            and t - current["start"] <= RISE_WINDOW_SECONDS
        ):
            current["goal"] = target
        if current is not None:
            current["outdoor"].append(obs["context"].get("outdoor"))
            current["supply"].append(obs["context"].get("supply"))
            if current["rise"] is None and temp >= current["t0"] + RESPONSE_K:
                current["rise"] = t
            if (
                t - current["start"] > MAX_EVENT_SECONDS
                or target < current["goal"] - ABANDON_DROP_K
            ):
                current = None
            elif temp >= min(current["goal"], target) - tolerance:
                # HHO may trim its target during the warm-up (live 12 Oct, Kitchen 19.5 ->
                # 19.4 at 07:48); arrival is judged against the lower of the two.
                current["arrival"] = min(current["goal"], target)
                current["hit"] = current["hit"] or t
                if (
                    t - current["hit"] >= HOLD_SECONDS
                    and current["arrival"] - current["t0"] < tolerance + SHORTFALL_K - 1e-9
                ):
                    # A trimmed target left almost nothing to climb: not a warm-up.
                    current, ended = None, True
                elif t - current["hit"] >= HOLD_SECONDS:
                    minutes = round((current["hit"] - current["start"]) / 60, 1)
                    rise = current["rise"] or current["hit"]
                    local = datetime.fromtimestamp(current["start"], zone_tz)
                    events.append(
                        {
                            "start": current["start"],
                            "t0": current["t0"],
                            "goal": current["arrival"],
                            "deficit": round(current["arrival"] - current["t0"], 2),
                            "minutes": minutes,
                            # Dead time before the room first responds (relay and boiler lag).
                            "delay_minutes": min(minutes, round((rise - current["start"]) / 60, 1)),
                            "outdoor": _mean(current["outdoor"]),
                            "supply": _mean(current["supply"]),
                            "hour": local.hour,
                        }
                    )
                    current, ended = None, True
            else:
                current["hit"] = None
        if (
            current is None
            and not ended
            and recent
            and target - min(rv for _, rv in recent) >= RISE_K
            and target > previous["target"]
            and target - tolerance - temp >= SHORTFALL_K - 1e-9
        ):
            current = {
                "start": t,
                "t0": temp,
                "goal": target,
                "hit": None,
                "rise": None,
                "outdoor": [obs["context"].get("outdoor")],
                "supply": [obs["context"].get("supply")],
            }
        previous = {"t": t, "target": target}
        recent.append((t, target))
    return events


def merge_events(stored, new):
    """Union by start time, newest ``KEEP_EVENTS``, oldest first."""
    merged = {e["start"]: e for e in [*stored, *new] if isinstance(e, dict) and "start" in e}
    return [merged[k] for k in sorted(merged)][-KEEP_EVENTS:]


def _finish(points, zone):
    """A cool-down record from consecutive observations, or None when unusable."""
    start = points[0]["time"] + AFTERHEAT_SECONDS
    kept = [p for p in points if p["time"] >= start]
    if not kept or kept[-1]["time"] - kept[0]["time"] < MIN_COOLDOWN_SECONDS - AFTERHEAT_SECONDS:
        return None
    readings = {}
    for p in kept:
        z = p["zones"][zone]
        readings.setdefault(z.get("temperature_updated", p["time"]), (p["time"], z["temperature"]))
    if len(readings) < MIN_REPORTS:
        return None
    samples = sorted(readings.values())
    t0 = samples[0][0]
    xs = [(t - t0) / 3600 for t, _ in samples]
    if xs[-1] <= 0:
        return None
    fit = statistics.linear_regression(xs, [v for _, v in samples])
    return {"start": t0, "hours": xs[-1], "slope": fit.slope}


def cooldowns(observations, zone):
    """Demand-off segments of one zone: at least 45 minutes, three reports, no DHW.

    A gap in the data, a target increase, demand returning or a hot water charge ends
    a segment; the segment is then dropped when it is too short.
    """
    out, current, previous = [], [], None

    def close():
        nonlocal current
        if current and (c := _finish(current, zone)):
            out.append(c)
        current = []

    for obs in observations:
        t = obs["time"]
        z = obs["zones"].get(zone, {})
        if previous is not None and t - previous["time"] > MAX_GAP_SECONDS:
            close()
        previous = obs
        usable = (
            _valid(z, t)
            and z.get("active") is False
            and z.get("demand_valid_until", z.get("valid_until", t)) >= t
        )
        if not usable or obs["context"].get("dhw_active") is not False:
            close()
            continue
        if current and z["target"] > current[-1]["zones"][zone]["target"] + 0.05:
            close()
        current.append(obs)
    close()
    return out


def coast_rate(downs, tz, minimum):
    """(median evening slope K/h, None) or (None, reason) when it cannot be trusted."""
    zone_tz = ZoneInfo(tz)
    evening = [
        c["slope"]
        for c in downs
        if datetime.fromtimestamp(c["start"], zone_tz).hour in EVENING_HOURS
    ]
    if len(evening) < minimum:
        return None, "insufficient cool-downs", len(evening)
    rate = statistics.median(evening)
    if rate >= MAX_COAST_RATE:
        return None, "house holding heat", len(evening)
    return rate, None, len(evening)


def build_fit(events, downs, params, now_ts):
    """A room's fit from its stored events and cool-down segments."""
    model = fit_model(events, now_ts, params["tolerance"])
    rate, reason, count = coast_rate(downs, params["timezone"], params["min_cooldowns"])
    enough = model is not None and len(events) >= params["min_warmups"]
    if not enough:
        confidence = "none"
    elif len(events) >= HIGH_EVENTS and (model.loo_error_min or 1e9) <= HIGH_LOO_MIN:
        confidence = "high"
    elif len(events) >= MEDIUM_EVENTS:
        confidence = "medium"
    else:
        confidence = "low"
    return RoomFit(
        model=model if enough else None,
        status="ready" if enough else "learning",
        confidence=confidence,
        events=len(events),
        cooldowns=count,
        coast_rate=None if rate is None else round(rate, 3),
        coast_reason=reason,
        last_events=[
            {
                "start": e["start"],
                "deficit": e["deficit"],
                "minutes": e["minutes"],
                "delay_minutes": e.get("delay_minutes"),
            }
            for e in events[-3:]
        ],
    )


def fit_room(observations, zone, params, stored_events=(), now_ts=None):
    """Fit one room from observations: ``(RoomFit, merged events)``.

    ``params``: tolerance, timezone (IANA name), min_warmups, min_cooldowns. ``now_ts``
    (epoch seconds) anchors recency; it defaults to the last observation.
    """
    events = merge_events(
        stored_events, warmup_events(observations, zone, params["tolerance"], params["timezone"])
    )
    if now_ts is None:
        now_ts = observations[-1]["time"] if observations else 0
    fit = build_fit(events, cooldowns(observations, zone), params, now_ts)
    return fit, events


def fit_all(observations, zones, params, stored, now_ts):
    """``({zone: RoomFit}, {zone: events})`` for the executor."""
    fits, kept = {}, {}
    for zone in zones:
        fits[zone], kept[zone] = fit_room(observations, zone, params, stored.get(zone, ()), now_ts)
    return fits, kept
