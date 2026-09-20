"""Pure period grouping and comparability rules; never a savings number."""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from .const import (
    ALLOCATION_UNKNOWN_SOFT_LIMIT,
    ASSOCIATION_NOTE,
    BUCKET_SECONDS,
    DEGREE_HOUR_BASE_C,
    DHW_SHARE_TOLERANCE_POINTS,
    MIN_COVERAGE,
    MIN_MEDIUM_CONFIDENCE_DAYS,
    NO_SAVINGS_LIMITATION,
)

BUCKETS_PER_DAY = 86400 // BUCKET_SECONDS
METERED_QUALITIES = ("ok", "rollover")


def local_date(timestamp, timezone_name):
    return datetime.fromtimestamp(timestamp, ZoneInfo(timezone_name)).date().isoformat()


def degree_hours(outdoor):
    if outdoor is None:
        return 0.0
    return max(0.0, DEGREE_HOUR_BASE_C - outdoor) * BUCKET_SECONDS / 3600


def group_days(buckets, timezone_name, slugs):
    """Per-day totals over local calendar days; missing buckets count against coverage."""
    days = {}
    for bucket in buckets:
        date = local_date(bucket["time"], timezone_name)
        day = days.setdefault(
            date,
            {
                "date": date,
                "kwh": dict.fromkeys(slugs, 0.0),
                "metered_buckets": dict.fromkeys(slugs, 0),
                "context_buckets": 0,
                "degree_hours": 0.0,
                "dhw_share_sum": 0.0,
                "dhw_known": 0,
                "allocation_unknown": 0,
                "eras": set(),
                "intervention": False,
                "reset_count": 0,
                "source_changes": 0,
            },
        )
        for slug in slugs:
            reading = bucket.get("meters", {}).get(slug, {})
            if reading.get("kwh") is not None and reading.get("quality") in METERED_QUALITIES:
                day["kwh"][slug] += reading["kwh"]
                day["metered_buckets"][slug] += 1
            if reading.get("quality") in ("reset", "rollover"):
                day["reset_count"] += 1
            if reading.get("source_changed"):
                day["source_changes"] += 1
        if bucket.get("outdoor") is not None:
            day["context_buckets"] += 1
            day["degree_hours"] += degree_hours(bucket["outdoor"])
        if bucket.get("dhw_share") is not None:
            day["dhw_known"] += 1
            day["dhw_share_sum"] += bucket["dhw_share"]
        if bucket.get("allocation") == "unknown":
            day["allocation_unknown"] += 1
        if bucket.get("era"):
            day["eras"].add(bucket["era"])
        if bucket.get("intervention"):
            day["intervention"] = True
    result = []
    for day in (days[d] for d in sorted(days)):
        metered = min(day["metered_buckets"].values()) if slugs else 0
        result.append(
            {
                "date": day["date"],
                "kwh": {slug: round(v, 3) for slug, v in day["kwh"].items()},
                "coverage_percent": round(100 * metered / BUCKETS_PER_DAY, 1),
                "context_coverage_percent": round(
                    100 * day["context_buckets"] / BUCKETS_PER_DAY, 1
                ),
                "degree_hours": round(day["degree_hours"], 2),
                "dhw_share": round(day["dhw_share_sum"] / day["dhw_known"], 3)
                if day["dhw_known"]
                else None,
                "allocation_unknown_share": round(day["allocation_unknown"] / BUCKETS_PER_DAY, 3),
                "eras": sorted(day["eras"]),
                "intervention": day["intervention"],
                "reset_count": day["reset_count"],
                "source_changes": day["source_changes"],
            }
        )
    return result


def summarise_period(days, slugs, expected_days=None):
    """Totals over *days*; coverage is measured against *expected_days* calendar days.

    When *expected_days* is not given the span between the first and last observed
    date is used, so days with no buckets at all still count against coverage.
    """
    if not days:
        return {
            "days": 0,
            "kwh": dict.fromkeys(slugs),
            "degree_hours": 0.0,
            "coverage_percent": 0.0,
            "context_coverage_percent": 0.0,
            "dhw_share": None,
            "allocation_unknown_share": None,
            "eras": [],
            "intervention_days": 0,
            "reset_count": 0,
            "source_changes": 0,
        }
    count = len(days)
    span = max(count, span_days(days) if expected_days is None else int(expected_days))
    known_dhw = [d["dhw_share"] for d in days if d["dhw_share"] is not None]
    return {
        "days": count,
        "expected_days": span,
        "kwh": {slug: round(sum(d["kwh"][slug] for d in days), 3) for slug in slugs},
        "degree_hours": round(sum(d["degree_hours"] for d in days), 2),
        "coverage_percent": round(sum(d["coverage_percent"] for d in days) / span, 1),
        "context_coverage_percent": round(
            sum(d["context_coverage_percent"] for d in days) / span, 1
        ),
        "dhw_share": round(sum(known_dhw) / len(known_dhw), 3) if known_dhw else None,
        "allocation_unknown_share": round(
            sum(d["allocation_unknown_share"] for d in days) / count, 3
        ),
        "eras": sorted({era for d in days for era in d["eras"]}),
        "intervention_days": sum(1 for d in days if d["intervention"]),
        "reset_count": sum(d["reset_count"] for d in days),
        "source_changes": sum(d["source_changes"] for d in days),
    }


def span_days(days):
    """Calendar days from the first to the last observed date, inclusive."""
    if not days:
        return 0
    first = datetime.fromisoformat(days[0]["date"]).date()
    last = datetime.fromisoformat(days[-1]["date"]).date()
    return (last - first).days + 1


def degree_hour_range(days):
    values = [d["degree_hours"] for d in days if d["context_coverage_percent"] >= MIN_COVERAGE]
    return (min(values), max(values)) if values else None


def comparability(days_a, days_b, slugs, expected_days=None):
    """Limits that stop two periods being compared; hard limits give 'insufficient'.

    *expected_days* is the requested calendar length of each period; wholly missing
    days inside it count against coverage.
    """
    a = summarise_period(days_a, slugs, expected_days)
    b = summarise_period(days_b, slugs, expected_days)
    hard, soft = [], []
    for label, period in (("a", "a"), ("b", "b")):
        summary = a if period == "a" else b
        if summary["days"] == 0:
            hard.append(f"period_{label}_no_data")
            continue
        if summary["coverage_percent"] < MIN_COVERAGE:
            hard.append(f"period_{label}_coverage_below_{MIN_COVERAGE}")
        if summary["context_coverage_percent"] < MIN_COVERAGE:
            hard.append(f"period_{label}_weather_context_below_{MIN_COVERAGE}")
        if summary["degree_hours"] <= 0:
            hard.append(f"period_{label}_no_heating_degree_hours")
        if summary["intervention_days"]:
            soft.append(f"period_{label}_contains_interventions")
        if len(summary["eras"]) > 1:
            hard.append(f"period_{label}_spans_configuration_eras")
        if (
            summary["allocation_unknown_share"] is not None
            and summary["allocation_unknown_share"] > ALLOCATION_UNKNOWN_SOFT_LIMIT
        ):
            soft.append(f"period_{label}_allocation_mostly_unknown")
    if a["days"] and b["days"]:
        if a["eras"] != b["eras"]:
            hard.append("configuration_era_mismatch")
        if a["dhw_share"] is None or b["dhw_share"] is None:
            hard.append("dhw_share_unknown")
        elif abs(a["dhw_share"] - b["dhw_share"]) * 100 > DHW_SHARE_TOLERANCE_POINTS:
            hard.append("dhw_share_differs")
        range_a, range_b = degree_hour_range(days_a), degree_hour_range(days_b)
        if range_a and range_b and (range_a[1] < range_b[0] or range_b[1] < range_a[0]):
            hard.append("degree_hour_ranges_do_not_overlap")
        if a["source_changes"] or b["source_changes"]:
            hard.append("meter_source_changed")
    result = {
        "periods": {"a": a, "b": b},
        "limits": hard + soft,
        "hard_limits": hard,
        "conclusion": "insufficient" if hard else "comparable",
        "kwh_per_degree_hour": None,
        "confidence": None,
        "note": ASSOCIATION_NOTE,
    }
    if hard:
        result["limitation"] = NO_SAVINGS_LIMITATION
        return result
    result["kwh_per_degree_hour"] = {
        label: {
            slug: round(summary["kwh"][slug] / summary["degree_hours"], 4)
            if summary["kwh"][slug] is not None
            else None
            for slug in slugs
        }
        for label, summary in (("a", a), ("b", b))
    }
    result["confidence"] = (
        "medium"
        if not soft
        and min(a["days"], b["days"]) >= MIN_MEDIUM_CONFIDENCE_DAYS
        and min(a["coverage_percent"], b["coverage_percent"]) >= 95
        else "low"
    )
    return result


def split_periods(days, since_date, until_date, timezone_name):
    """Days in [since, until) and the equal-length span immediately before."""
    length = until_date - since_date
    before = since_date - length
    a = [d for d in days if since_date.isoformat() <= d["date"] < until_date.isoformat()]
    b = [d for d in days if before.isoformat() <= d["date"] < since_date.isoformat()]
    return a, b


def since_periods(days, since, now, timezone_name):
    """Period a = [since, today], period b = the equal-length span before; plus its length."""
    zone = ZoneInfo(timezone_name)
    since_date = since.astimezone(zone).date()
    until_date = now.astimezone(zone).date() + timedelta(days=1)
    a, b = split_periods(days, since_date, until_date, timezone_name)
    return a, b, (until_date - since_date).days
