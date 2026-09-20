"""Allowlisted report summaries and rendered text; raw evidence stays in the store."""

TASK_TITLES = {
    "daily_summary": "Daily summary",
    "weekly_review": "Weekly review",
    "investigation": "Investigation",
}
HEADLINE_LIMIT = 120


def headline(report):
    findings = report.get("report", {}).get("findings") or []
    if not findings:
        return None
    title = " ".join(str(findings[0].get("title", "")).split())
    return title if len(title) <= HEADLINE_LIMIT else title[: HEADLINE_LIMIT - 1] + "…"


def latest_attributes(report):
    """Entity attributes: identifiers and counts only, never report or evidence text."""
    body = report.get("report", {})
    return {
        "report_id": report.get("id"),
        "task": report.get("task"),
        "profile_name": report.get("profile", {}).get("name"),
        "finding_count": len(body.get("findings") or []),
        "limitation_count": len(body.get("limitations") or []),
        "headline": headline(report),
    }


def coverage_summary(evidence):
    facts = evidence.get("facts", {}) if isinstance(evidence, dict) else {}
    window = facts.get("window") or {}
    system = window.get("system") or {}
    live = facts.get("quality.live") or {}
    history = facts.get("quality.history") or {}
    rooms = {}
    for key, identity in facts.items():
        parts = key.split(".")
        if len(parts) != 3 or parts[0] != "room" or parts[2] != "identity":
            continue
        prefix = f"room.{parts[1]}."
        rooms[identity.get("name") or identity.get("id") or parts[1]] = {
            "coverage_percent": facts.get(prefix + "coverage"),
            "demand_coverage_percent": facts.get(prefix + "demand_coverage"),
            "recent_change_coverage_percent": facts.get(prefix + "recent_change_coverage"),
            "unavailable_metrics": list(facts.get(prefix + "unavailable_metrics") or []),
            "suppressed_metrics": list(facts.get(prefix + "suppressed_metrics") or []),
        }
    return {
        "analysis_window_days": window.get("analysis_window_days"),
        "window_start": window.get("window_start"),
        "window_end": window.get("window_end"),
        "system_coverage_percent": system.get("coverage"),
        "system_status": system.get("status"),
        "live_input_availability_percent": live.get("availability_percent"),
        "history_backfill": history.get("backfill"),
        "history_state_policy": history.get("history_state_policy"),
        "rooms": rooms,
        "omitted_evidence": list(evidence.get("omitted", [])) if isinstance(evidence, dict) else [],
    }


def summarise(report):
    body = report.get("report", {})
    profile = report.get("profile", {})
    findings = [
        {
            "title": f.get("title"),
            "kind": f.get("kind"),
            "detail": f.get("detail"),
            "evidence_ids": list(f.get("evidence_ids") or []),
            "next_check": f.get("next_check"),
        }
        for f in body.get("findings") or []
    ]
    references = sorted({ref for f in findings for ref in f["evidence_ids"]})
    summary = {
        "report_id": report.get("id"),
        "task": report.get("task"),
        "task_title": TASK_TITLES.get(report.get("task"), report.get("task")),
        "created_at": report.get("created_at"),
        "profile_name": profile.get("name"),
        "profile_provider": profile.get("provider"),
        "profile_model": profile.get("model"),
        "profile_effort": profile.get("effort"),
        "prompt_version": report.get("prompt_version"),
        "evidence_hash": report.get("evidence_hash"),
        "question": report.get("evidence", {}).get("question") or None,
        "conclusion": body.get("conclusion"),
        "summary": body.get("summary"),
        "evidence_references": references,
        "coverage": coverage_summary(report.get("evidence", {})),
        "findings": findings,
        "limitations": list(body.get("limitations") or []),
        "follow_up_actions": [f["next_check"] for f in findings if f.get("next_check")],
        "validation": report.get("validation"),
    }
    summary["text"] = render(summary)
    return summary


def _percent(value):
    return "unavailable" if value is None else f"{value}%"


def render(summary):
    lines = [
        f"# Heating Advisor: {summary['task_title']}",
        "",
        f"- Created: {summary['created_at']}",
        f"- Report ID: {summary['report_id']}",
        "- Profile: "
        + " / ".join(
            str(v)
            for v in (
                summary["profile_name"],
                summary["profile_provider"],
                summary["profile_model"],
            )
            if v
        ),
        f"- Prompt version: {summary['prompt_version']}; evidence hash: {summary['evidence_hash']}",
        f"- Conclusion: {summary['conclusion']}",
    ]
    if summary["question"]:
        lines.append(f"- Question: {summary['question']}")
    lines += ["", "## Summary", "", summary["summary"] or "(no summary)"]
    coverage = summary["coverage"]
    lines += ["", "## Coverage", ""]
    if coverage["analysis_window_days"] is not None:
        lines.append(
            f"- Analysis window: {coverage['analysis_window_days']} days"
            f" ({coverage['window_start']} to {coverage['window_end']})"
        )
    lines.append(
        f"- System coverage: {_percent(coverage['system_coverage_percent'])}"
        + (f" ({coverage['system_status']})" if coverage["system_status"] else "")
    )
    lines.append(
        f"- Live input availability: {_percent(coverage['live_input_availability_percent'])}"
    )
    for name, room in coverage["rooms"].items():
        line = (
            f"- {name}: coverage {_percent(room['coverage_percent'])},"
            f" demand coverage {_percent(room['demand_coverage_percent'])}"
        )
        missing = room["unavailable_metrics"] + room["suppressed_metrics"]
        if missing:
            line += f"; unavailable: {', '.join(missing)}"
        lines.append(line)
    if coverage["omitted_evidence"]:
        lines.append(f"- Omitted from evidence: {', '.join(coverage['omitted_evidence'])}")
    lines += ["", "## Findings", ""]
    if not summary["findings"]:
        lines.append("No findings were reported.")
    for index, finding in enumerate(summary["findings"], 1):
        lines += [
            f"### {index}. {finding['title']} ({finding['kind']})",
            "",
            finding["detail"] or "",
            "",
            f"Evidence: {', '.join(finding['evidence_ids'])}",
            "",
            f"Next check: {finding['next_check']}",
            "",
        ]
    lines += ["## Limitations and uncertainty", ""]
    lines += [f"- {item}" for item in summary["limitations"]] or ["- None stated by the report."]
    lines += ["", "## Follow-up actions", ""]
    lines += [f"{i}. {action}" for i, action in enumerate(summary["follow_up_actions"], 1)] or [
        "No follow-up actions were proposed."
    ]
    if summary["validation"]:
        lines += ["", f"_{summary['validation']}_"]
    return "\n".join(lines).rstrip() + "\n"
