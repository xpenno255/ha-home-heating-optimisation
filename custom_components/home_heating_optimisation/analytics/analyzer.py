"""Observed comfort and recovery analytics with explicit coverage and uncertainty.

Adapted from Radiator Analytics (MIT), revision 0958ddbf4861b26c1bfefdd2191f5814c9db62d3.

No inferred pipe position, water flow, radiator power or fuel efficiency.
"""

from __future__ import annotations

import math
import statistics
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from .const import (
    DEFAULT_RECOVERY,
    DEFAULT_TOLERANCE,
    MAX_GAP_SECONDS,
    MIN_COVERAGE,
    MIN_MATCH_DAYS,
    MIN_MATCHES,
    TARGET_HOLD_SECONDS,
)
from .observations import number


@dataclass
class ZoneStats:
    zone_id: str
    heating_rate_avg: float | None = None
    heating_rate_morning: float | None = None
    duty_cycle: float | None = None
    time_to_setpoint_avg: float | None = None
    setpoint_achievement: float | None = None
    coverage: float = 0
    demand_coverage: float = 0
    within_band: float | None = None
    deficit_degree_hours: float | None = None
    overshoot_degree_hours: float | None = None
    observed_hours: float = 0
    total_sessions: int = 0
    total_morning_sessions: int = 0
    completed_recoveries: int = 0
    cancelled_recoveries: int = 0
    ongoing_recoveries: int = 0
    rate_alone: float | None = None
    rate_concurrent: float | None = None
    response_ratio: float | None = None
    matched_pairs: int = 0
    matched_days: int = 0
    response_difference: float | None = None
    response_interval: list[float] | None = None
    response_status: str = "insufficient matched observations"


@dataclass
class SystemStats:
    recommendations: list[str] = field(default_factory=list)
    coverage: float = 0
    status: str = "insufficient_data"


@dataclass
class AnalyticsResult:
    zone_stats: dict[str, ZoneStats] = field(default_factory=dict)
    system: SystemStats = field(default_factory=SystemStats)
    analysis_window_days: int = 7
    window_start: str = ""
    window_end: str = ""
    last_updated: str = ""


@dataclass
class ComparisonResult:
    summary: str = "Insufficient data"
    recommendations: list[str] = field(default_factory=list)
    zone_comparisons: dict = field(default_factory=dict)
    last_updated: str = ""
    current_start: str = ""
    current_end: str = ""
    previous_start: str = ""
    previous_end: str = ""


def _valid(z, time):
    return (
        number(z.get("temperature")) is not None
        and number(z.get("target")) is not None
        and z.get("valid_until", time) >= time
    )


def _segments(observations, zone, start, end):
    """Integrate the last known state, clipped to the window and source freshness.

    No invented linear temperature interpolation across sparse RF updates.
    """
    for i, observation in enumerate(observations):
        t = observation["time"]
        if t >= end:
            break
        z = observation["zones"].get(zone, {})
        until = observations[i + 1]["time"] if i + 1 < len(observations) else end
        left, right = max(t, start), min(until, end, t + MAX_GAP_SECONDS)
        if right > left:
            yield observation, z, left, right


def _episodes(observations, zone, tolerance, horizon):
    """Track first sustained target arrival across demand pulses and afterheat.

    Boundary starts, missing observations and changed targets are censored.
    Success and a fixed configured recovery deadline are the only scored outcomes.
    """
    episodes = []
    current = None
    previous = None
    for observation in observations:
        t = observation["time"]
        z = observation["zones"].get(zone, {})
        valid = _valid(z, t)
        previous_z = previous["zones"].get(zone, {}) if previous else {}
        continuous = (
            previous is not None
            and valid
            and _valid(previous_z, t)
            and t - previous["time"] <= MAX_GAP_SECONDS
        )
        demand_continuous = (
            continuous
            and previous_z.get("active") is not None
            and previous_z.get("demand_valid_until", previous_z.get("valid_until", t)) >= t
        )
        target_changed = continuous and abs(z["target"] - previous_z["target"]) > 0.05
        if current and (not continuous or target_changed):
            current["outcome"] = "target_changed" if target_changed else "missing_data"
            episodes.append(current)
            current = None
        if not valid:
            previous = None
            continue
        deficit = z["target"] - z["temperature"]
        trigger = (
            not continuous
            or target_changed
            or not demand_continuous
            or previous_z.get("active") is False
            or previous_z.get("temperature", -100) >= z["target"] - tolerance
        )
        if (
            current is None
            and z.get("active") is True
            and deficit >= max(0.5, 2 * tolerance)
            and trigger
        ):
            current = {
                "start": t,
                "target": z["target"],
                "start_temp": z["temperature"],
                "deficit": deficit,
                "censored": not continuous or not demand_continuous,
                "points": [],
                "band_start": None,
                "outcome": "ongoing",
            }
        if current:
            current["points"].append(observation)
            # A whole observed interval within tolerance is required, not one crossing.
            in_band = abs(z["temperature"] - current["target"]) <= tolerance
            if in_band and current["band_start"] is None:
                current["band_start"] = t
            elif not in_band:
                current["band_start"] = None
            band = current["band_start"]
            if (
                band is not None
                and t - band >= TARGET_HOLD_SECONDS
                and band - current["start"] <= horizon
            ):
                current.update(outcome="success", time_to_target=(band - current["start"]) / 60)
                episodes.append(current)
                current = None
            elif t - current["start"] > horizon + TARGET_HOLD_SECONDS:
                current["outcome"] = "deadline_missed"
                episodes.append(current)
                current = None
        previous = observation
    if current:
        episodes.append(current)
    return episodes


def _ramp(episode, zone):
    """A 10–60 minute demand-dominated ramp with at least three actual reports."""
    if episode["censored"] or episode["outcome"] in ("missing_data", "target_changed", "ongoing"):
        return None
    limit = min(episode["start"] + 3600, episode.get("band_start") or math.inf)
    points = [p for p in episode["points"] if p["time"] <= limit]
    if len(points) < 3 or points[-1]["time"] - points[0]["time"] < 600:
        return None
    readings = {}
    for p in points:
        z = p["zones"][zone]
        readings.setdefault(z.get("temperature_updated", p["time"]), (p["time"], z["temperature"]))
    if len(readings) < 3:
        return None
    samples = sorted(readings.values())
    rate = statistics.linear_regression(
        [(t - episode["start"]) / 3600 for t, _ in samples], [v for _, v in samples]
    ).slope
    duration = points[-1]["time"] - points[0]["time"]
    totals = {k: 0.0 for k in ("outdoor", "supply", "demand", "active", "alone", "loaded")}
    known = {k: 0.0 for k in ("outdoor", "supply", "demand")}
    heat_known = dhw_clear = concurrency_known = True
    for a, b in zip(points, points[1:]):
        dt = b["time"] - a["time"]
        z = a["zones"][zone]
        context = a["context"]
        for k in known:
            value = z.get(k) if k == "demand" else context.get(k)
            valid_until = (
                z.get("demand_valid_until", math.inf)
                if k == "demand"
                else a.get("context_valid_until", {}).get(k, math.inf)
            )
            valid_dt = max(0, min(b["time"], valid_until) - a["time"])
            if value is not None:
                totals[k] += value * valid_dt
                known[k] += valid_dt
        totals["active"] += (
            max(0, min(b["time"], z.get("demand_valid_until", math.inf)) - a["time"])
            if z.get("active") is True
            else 0
        )
        others = [v for k, v in a["zones"].items() if k != zone]
        all_known = all(
            v.get("active") is not None
            and v.get("demand_valid_until", v.get("valid_until", a["time"])) >= b["time"]
            for v in others
        )
        concurrency_known &= all_known
        if all_known:
            count = sum(v["active"] is True for v in others)
            totals["alone" if count == 0 else "loaded"] += dt
        heat_known &= (
            context.get("heating_active") is True
            and a.get("context_valid_until", {}).get("heating_active", math.inf) >= b["time"]
        )
        dhw_clear &= (
            context.get("dhw_active") is False
            and a.get("context_valid_until", {}).get("dhw_active", math.inf) >= b["time"]
        )
    if totals["active"] / duration < 0.8:
        return None
    exposure = (
        "alone"
        if totals["alone"] / duration >= 0.9
        else "loaded"
        if totals["loaded"] / duration >= 0.8
        else "mixed"
    )
    conditions = {k: totals[k] / duration if known[k] >= duration - 0.01 else None for k in known}
    return {
        "rate": rate,
        "start": episode["start"],
        "start_temp": episode["start_temp"],
        "deficit": episode["deficit"],
        "exposure": exposure,
        **conditions,
        "comparable": concurrency_known
        and heat_known
        and dhw_clear
        and all(v is not None for v in conditions.values()),
    }


def _response(stats, ramps, tz):
    """No-reuse nearest matches and uncertainty across independent local days.

    This is a conditional association, not a hydraulic or causal model.
    """
    alone = [r for r in ramps if r["comparable"] and r["exposure"] == "alone"]
    loaded = [r for r in ramps if r["comparable"] and r["exposure"] == "loaded"]
    pairs = []
    for r in loaded:
        matches = []
        for a in alone:
            if r.get("regime", 0) != a.get("regime", 0):
                continue
            differences = [
                abs(r[k] - a[k]) / width
                for k, width in (
                    ("outdoor", 3),
                    ("supply", 3),
                    ("start_temp", 1),
                    ("deficit", 0.5),
                    ("demand", 0.2),
                )
            ]
            if max(differences) <= 1:
                matches.append((sum(differences), a))
        if matches:
            a = min(matches, key=lambda pair: pair[0])[1]
            alone.remove(a)
            pairs.append((r, a))
    stats.matched_pairs = len(pairs)
    by_day = {}
    for r, a in pairs:
        day = datetime.fromtimestamp(r["start"], tz).date().isoformat()
        by_day.setdefault(day, []).append(r["rate"] - a["rate"])
    stats.matched_days = len(by_day)
    if len(pairs) < MIN_MATCHES or len(by_day) < MIN_MATCH_DAYS:
        return
    # Require separate baseline days too: one day's alone episodes are not replication.
    if len({datetime.fromtimestamp(a["start"], tz).date() for _, a in pairs}) < MIN_MATCH_DAYS:
        stats.response_status = "insufficient independent baseline days"
        return
    stats.rate_alone = round(statistics.mean(a["rate"] for _, a in pairs), 3)
    stats.rate_concurrent = round(statistics.mean(r["rate"] for r, _ in pairs), 3)
    daily = [statistics.mean(v) for v in by_day.values()]
    difference = statistics.mean(daily)
    # Conservative two-standard-error descriptive interval; not a causal significance test.
    margin = 2 * statistics.stdev(daily) / math.sqrt(len(daily))
    stats.response_difference = round(difference, 3)
    stats.response_interval = [round(difference - margin, 3), round(difference + margin, 3)]
    if stats.rate_alone >= 0.2:
        stats.response_ratio = round(stats.rate_concurrent / stats.rate_alone, 3)
        stats.response_status = "matched association; unmeasured gains and loads may differ"
    else:
        stats.response_status = "baseline warm-up too small for a stable ratio"


def compute_analytics(
    observations,
    monitored_zones,
    analysis_window_days,
    zone_names=None,
    *,
    now=None,
    timezone_name="UTC",
    tolerance=DEFAULT_TOLERANCE,
    recovery_minutes=DEFAULT_RECOVERY,
    adjustment_times=None,
):
    """Compute a bounded window using all eligible observed intervals."""
    now = now or datetime.now(timezone.utc)
    start = now - timedelta(days=analysis_window_days)
    tz = ZoneInfo(timezone_name)
    result = AnalyticsResult(
        analysis_window_days=analysis_window_days,
        window_start=start.isoformat(),
        window_end=now.isoformat(),
        last_updated=now.isoformat(),
    )
    start_ts, end_ts = start.timestamp(), now.timestamp()
    history = sorted((o for o in observations if o["time"] <= end_ts), key=lambda o: o["time"])
    zone_names = zone_names or {}
    for zone in monitored_zones:
        zs = ZoneStats(zone)
        result.zone_stats[zone] = zs
        observed = band = deficit = overshoot = demand_known = active = 0.0
        for o, z, left, right in _segments(history, zone, start_ts, end_ts):
            duration = right - left
            demand_duration = max(
                0, min(right, z.get("demand_valid_until", z.get("valid_until", left))) - left
            )
            if z.get("active") is not None:
                demand_known += demand_duration
                active += demand_duration if z["active"] else 0
            if not _valid(z, left):
                continue
            duration = max(0, min(right, z.get("valid_until", left)) - left)
            observed += duration
            error = z["target"] - z["temperature"]
            band += duration if abs(error) <= tolerance + 1e-9 else 0
            deficit += max(0, error - tolerance) * duration / 3600
            overshoot += max(0, -error - tolerance) * duration / 3600
        zs.coverage = round(100 * observed / (end_ts - start_ts), 1)
        zs.demand_coverage = round(100 * demand_known / (end_ts - start_ts), 1)
        zs.observed_hours = round(observed / 3600, 2)
        if demand_known:
            zs.duty_cycle = round(active / demand_known * 100, 1)
        if observed:
            zs.within_band = round(band / observed * 100, 1)
            zs.deficit_degree_hours = round(deficit, 3)
            zs.overshoot_degree_hours = round(overshoot, 3)
        episodes = [
            e
            for e in _episodes(history, zone, tolerance, recovery_minutes * 60)
            if start_ts <= e["start"] < end_ts
        ]
        zs.total_sessions = len(episodes)
        scored = [
            e
            for e in episodes
            if not e["censored"] and e["outcome"] in ("success", "deadline_missed")
        ]
        zs.completed_recoveries = len(scored)
        zs.cancelled_recoveries = sum(
            e["censored"] or e["outcome"] in ("target_changed", "missing_data") for e in episodes
        )
        zs.ongoing_recoveries = sum(
            e["outcome"] == "ongoing" and not e["censored"] for e in episodes
        )
        successful = [e["time_to_target"] for e in scored if e["outcome"] == "success"]
        if successful:
            zs.time_to_setpoint_avg = round(statistics.mean(successful), 1)
        if scored:
            zs.setpoint_achievement = round(100 * len(successful) / len(scored), 1)
        ramps = [r for e in episodes if (r := _ramp(e, zone)) is not None]
        if ramps:
            zs.heating_rate_avg = round(statistics.median(r["rate"] for r in ramps), 3)
        morning = [r["rate"] for r in ramps if 5 <= datetime.fromtimestamp(r["start"], tz).hour < 9]
        zs.total_morning_sessions = len(morning)
        if morning:
            zs.heating_rate_morning = round(statistics.median(morning), 3)
        for ramp in ramps:
            ramp["regime"] = sum(t <= ramp["start"] for t in (adjustment_times or []))
            if any(ramp["start"] < t <= ramp["start"] + 3600 for t in (adjustment_times or [])):
                ramp["comparable"] = False
        _response(zs, ramps, tz)
        name = zone_names.get(zone, zone)
        if zs.coverage < MIN_COVERAGE:
            result.system.recommendations.append(
                f"{name}: Only {zs.coverage}% temperature/target coverage. Collect more valid observations before judging performance."
            )
        elif (
            zs.within_band is not None
            and zs.within_band < 70
            and (zs.deficit_degree_hours or 0) > 2
        ):
            result.system.recommendations.append(
                f"{name}: Below the commanded comfort band for a material part of this window. Check schedule, room sensor, actual water temperature and radiator heat arrival before changing a valve."
            )
        if (
            zs.completed_recoveries >= 5
            and zs.setpoint_achievement is not None
            and zs.setpoint_achievement < 60
        ):
            result.system.recommendations.append(
                f"{name}: Reached the comfort band within {recovery_minutes} minutes in {zs.setpoint_achievement}% of completed recoveries. Compare starting conditions and check heat delivery; this does not identify a hydraulic cause."
            )
        if zs.response_interval and zs.response_interval[1] < -0.1:
            result.system.recommendations.append(
                f"{name}: Slower recovery with other monitored demands in {zs.matched_pairs} matched pairs. This is an association; verify radiator inlet/outlet behaviour in a repeatable comparison."
            )
    if result.zone_stats:
        result.system.coverage = round(
            statistics.mean(z.coverage for z in result.zone_stats.values()), 1
        )
        if all(z.coverage >= MIN_COVERAGE for z in result.zone_stats.values()):
            result.system.status = "observations_available"
    return result


def compare_windows(current, previous, zone_names=None):
    """Descriptive rolling-window changes, never intervention or efficiency claims."""
    result = ComparisonResult(
        last_updated=current.window_end,
        current_start=current.window_start,
        current_end=current.window_end,
        previous_start=previous.window_start,
        previous_end=previous.window_end,
    )
    compared = 0
    missing = 0
    names = zone_names or {}
    for zone, cur in current.zone_stats.items():
        prev = previous.zone_stats.get(zone)
        if prev is None or min(cur.coverage, prev.coverage) < MIN_COVERAGE:
            missing += 1
            continue
        if cur.within_band is None or prev.within_band is None:
            missing += 1
            continue
        compared += 1
        delta = round(cur.within_band - prev.within_band, 1)
        result.zone_comparisons[zone] = {
            "within_band_change_percentage_points": delta,
            "current_coverage": cur.coverage,
            "previous_coverage": prev.coverage,
            "current_within_band": cur.within_band,
            "previous_within_band": prev.within_band,
        }
        if abs(delta) >= 5:
            result.recommendations.append(
                f"{names.get(zone, zone)}: Time in the commanded comfort band changed by {delta:+.1f} percentage points. Weather, targets and occupancy may differ; no adjustment or energy benefit is inferred."
            )
    result.summary = (
        f"{compared} zones compared; {missing} with insufficient data"
        if compared
        else "Insufficient data for comparison"
    )
    return result
