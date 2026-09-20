# Release notes: issue #19

- Added the `ask_advisor_followup` action: one bounded question about a retained advisor report, answered from that report's saved evidence snapshot with cited fact IDs, flagged unsupported claims and named missing data.
- Added a **Follow-up question AI profile** advisor option; follow-ups refuse to run without it and never fall back to another task's profile.
- Retained up to 10 follow-up conversations of 8 turns each in the private advisor store, restored across restarts; older stores load unchanged.
- Listed conversation IDs and turn counts per report in `get_advisor_reports`, and added `get_advisor_followup` to return one conversation's text; text stays out of attributes and diagnostics.
- Follow-ups share the advisor's daily call limit and one-minute spacing, and record an `advisor_followup` journal event when a journal is present.
