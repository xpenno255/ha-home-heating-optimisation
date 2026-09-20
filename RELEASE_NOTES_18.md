# Release notes: issue #18

- Added the `get_advisor_report_summary` action, returning one retained advisor report (latest by default) as rendered Markdown plus structured task, profile, evidence-reference, coverage, finding, limitation and follow-up fields.
- Added the **Advisor latest report** diagnostic sensor with the latest report's creation time and identifier/count attributes only; report text and evidence stay out of entity attributes and diagnostics.
- Added opt-in advisor notifications with configurable `notify.*` targets (persistent notification fallback), event selection and optional finding titles; a report is announced once and failures at most once per task per day, persisted across restarts.
- Isolated notification delivery failures from review results and heating control, and recorded retained reports in the journal when one is enabled.
