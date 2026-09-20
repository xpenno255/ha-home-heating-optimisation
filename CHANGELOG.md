# Changelog

Remaining commissioning and roadmap work is tracked in the [GitHub backlog index (#12)](https://github.com/xpenno255/ha-home-heating-optimisation/issues/12). Every 0.6.x release is a commissioning prerelease: software validation is complete, physical commissioning is not.

## 0.7.0 - evidence, advisor workflow, reliability and migration (commissioning prerelease)

Closes the remaining software items of the [handover backlog (#12)](https://github.com/xpenno255/ha-home-heating-optimisation/issues/12). Physical commissioning ([#13](https://github.com/xpenno255/ha-home-heating-optimisation/issues/13) 24-hour review, [#14](https://github.com/xpenno255/ha-home-heating-optimisation/issues/14) boiler/DHW) is still outstanding; nothing here activates control or claims measured savings.

### Reliability
- Gateway monitoring ([#27](https://github.com/xpenno255/ha-home-heating-optimisation/issues/27)): optional list of RAMSES gateway online entities with an unresponsive threshold (default 10 minutes). One repairs issue and one persistent notification per outage, cleared with a recovery notification and flap count; `home_heating_optimisation_gateway_unresponsive` / `_gateway_recovered` events for automations; a diagnostic sensor per gateway and a `gateways` block in `get_control_report`. Startup and reload observe a two-minute minimum grace so a broker's transient offline message never alerts.
- Schedule retrieval diagnostics ([#15](https://github.com/xpenno255/ha-home-heating-optimisation/issues/15)): each room decision sensor now reports `schedule_fetch_status`, `schedule_fetch_failure_class` (`incomplete_fragments`, `transport_timeout`, `parser_error`, `unavailable`, `unknown`), `schedule_fetch_attempts` and `schedule_next_retry_at`. Retry timing and cloud/live/cache precedence are unchanged and now pinned by tests. Findings from the 20 September read-only investigation are in `docs/ramses-reliability-2026-09-20.md`.
- Same-temperature renewals ([#16](https://github.com/xpenno255/ha-home-heating-optimisation/issues/16)): confirmed that ramses_cc exposes no inbound packet provenance, so such renewals stay `matching_readback_unverified`. Added a `readback_hint` attribute for unverified and timed-out outcomes that explains what to check without claiming failed delivery or valve movement. Confirmation rules are unchanged.

### Evidence
- Intervention journal ([#17](https://github.com/xpenno255/ha-home-heating-optimisation/issues/17)): every decision change, command request, service result, readback transition, manual hold, schedule change, mode change, handover, rollback and adjustment note is a versioned event with controller, configuration-era, model and schedule-source provenance. `get_journal` action, `Intervention journal status` sensor, `Enable intervention journal` option (on by default). Bounded to 20,000 events / 30 days; storage faults never affect control.
- Metered energy evidence ([#25](https://github.com/xpenno255/ha-home-heating-optimisation/issues/25)): up to three energy or gas counters recorded as five-minute kWh buckets with reset, rollover, gap and unit handling plus heating/DHW share, outdoor temperature, configuration era and intervention context (90 days). `get_energy_report` action, energy status and per-meter daily kWh sensors. Comparisons return `insufficient` rather than a number when periods are not comparable; kWh per degree hour is labelled association, not causal evidence. Advisor evidence gains an allowlisted `energy` fact.

### Advisor workflow
- Report reader and notifications ([#18](https://github.com/xpenno255/ha-home-heating-optimisation/issues/18)): `get_advisor_report_summary` renders one retained report as Markdown plus structured fields; `Advisor latest report` sensor carries identifiers and counts only. Opt-in notifications to `notify.*` targets or a persistent notification, announced once per report and at most once per task per day on failure.
- Follow-up questions ([#19](https://github.com/xpenno255/ha-home-heating-optimisation/issues/19)): `ask_advisor_followup` asks one bounded question about a retained report, answered from its saved evidence snapshot with cited fact IDs, flagged unsupported claims and named missing data. Requires a dedicated follow-up AI profile; no fallback. Up to 10 conversations of 8 turns, restored across restarts; `get_advisor_followup` returns one conversation.
- Recommendation decisions ([#20](https://github.com/xpenno255/ha-home-heating-optimisation/issues/20)): every stored finding becomes a `proposed` recommendation with a stable ID. `decide_recommendation`, `mark_recommendation_applied`, `evaluate_recommendation` and `get_recommendations` record owner decisions and outcomes; accepting a recommendation changes no heating setting. Association-only follow-up eligibility (7 days, 80% coverage, unchanged configuration era, energy comparability when available).
- Bounded trials ([#21](https://github.com/xpenno255/ha-home-heating-optimisation/issues/21)): `propose_trial`, `approve_trial`, `start_trial`, `stop_trial`, `reject_trial`, `evaluate_trial`, `get_trials`. One allowlisted tuning parameter per trial (room `trust_k`/`cap_up`/`cap_down`, boiler `design_flow`/`design_outdoor`/`return_ceiling`) with bounds, max step, baseline, predeclared criteria, expiry and rollback. Trials run only on a scope already active/auto with ownership ready; they never change modes, DHW protection or issue actuator commands. Supervised every minute and rolled back with readback verification on expiry, comfort or degree-hour breach, manual override, mode change, guard, unload or restart; an unverified rollback raises a `trial_rollback_failed` repairs issue. See `docs/bounded-trials.md`.
- Coordination and learning design ([#22](https://github.com/xpenno255/ha-home-heating-optimisation/issues/22)): `docs/coordination-and-learning-design.md`, design only, nothing implemented.

### Legacy migration
- Identity migration ([#23](https://github.com/xpenno255/ha-home-heating-optimisation/issues/23)): `preview_identity_migration`, `migrate_identities` (`confirm: true`) and `rollback_identity_migration` move compatible legacy OT/BFC output entity IDs onto the consolidated entities through a registry rename so history and long-term statistics follow; revised or estimated metrics are archived. Refused while legacy entries are enabled, before handover, or while any control is active.
- Radiator Analytics import ([#24](https://github.com/xpenno255/ha-home-heating-optimisation/issues/24)): `preview_history_import`, `import_history` (`confirm: true`) and `retire_legacy_store` (`confirm: true`) copy compatible legacy observations and notes into a separate private imported era, keyed by checksum so repeats are skipped; current analysis is unchanged.

## 0.6.4 - estimated boiler efficiency (commissioning prerelease)

- Add an optional **Estimated boiler efficiency (%)** diagnostic for the British Gas 430/i natural-gas profile. Disabled by default; select the profile in the boiler-control options to enable it.
- Estimate from return temperature and manufacturer full-load reference data on a gross-energy basis. It is guidance, not metered efficiency, load-adjusted efficiency, seasonal efficiency or proof of savings.
- Require configured return-temperature, burner-modulation and heating-active sources, fresh readings and an observed uninterrupted burn of at least five minutes; report unknown while off, warming up, outside the 30–60 °C return range or missing reliable data.
- Expose status and model assumptions as sensor attributes. Boiler control, setpoints, energy counters and reported savings are unchanged.

## 0.6.3 - restart ownership reconciliation (commissioning prerelease)

- Preserve room policy ownership, manual holds and window timers until room and hub restoration finish, including decisions that would send no command.
- Reconcile saved ownership/manual history only after a two-minute startup settling interval and a fresh primary thermostat report at or after that interval, with a readable schedule reference.
- Keep rooms awaiting reconciliation in `no_data` with an explicit reason, no actuator commands and a retry every minute; missing or stale inputs keep the room deferred.
- Preserve genuine manual changes, off settings and existing hold deadlines; retain runtime-only command acknowledgements.
- Do not automatically clear previously saved manual holds, which cannot safely be distinguished from genuine user adjustments.

## 0.6.2 — radio fault recovery (commissioning prerelease)

- Exclude state projected during a thermostat service call from command confirmation.
- Recognise a return to an exact previous, still-owned override separately from a new manual adjustment, with a bounded recovery retry.
- Keep same-temperature renewals explicitly unverified when the source cannot prove acknowledgement; an expiry change alone is insufficient.
- Serialize optional radio schedule downloads in the background, skip unavailable zones, and back off repeated failures from five minutes to an hour.
- Refresh the offline cache timestamp after a successful download even when the schedule is unchanged.

## 0.6.1 — controller readiness (commissioning prerelease)

- Configure and edit consolidated controllers independently of legacy integrations, with explicit shadow/ownership safeguards and separate observation/control sensor choices.
- Track requested, sent, pending and confirmed radiator targets using subsequent thermostat readback; distinguish service success, missing echoes and failures.
- Include command timing, schedule source and model-version provenance in sampled controller history.
- Follow source entity registry renames across configured observation/control/MQTT references while retaining room identities and rollback snapshots.
- Validate control continuity through optional Recorder, history-save and AI failures.
- Publish the consolidated controller regression suites using synthetic survey fixtures, with a documented supervised trial and retained legacy rollback.

This includes the previously local 0.6.0 control consolidation below. Physical commissioning remains outstanding; new setups and imports remain in shadow.

## 0.6.0 — consolidated control (local development release)

- Port the existing comfort and boiler engines with their regression suites.
- Import room sensors, tunables, occupancy settings and surveys into independent shadow control.
- Add journalled handover, legacy-writer checks, persistent independent modes and rollback.
- Use actual non-retained EMS-ESP MQTT payload receipt for telemetry freshness.
- Retry missing RAMSES schedules and bound offline schedule-cache use.
- Keep original observation measurements separate from selected comfort-control room air.


## 0.5.2 — consistent advisor report requirements

- Share report limits between prompt instructions, schema field descriptions and local validation, including the 12-reference limit per finding and required follow-up checks.
- Keep provider schemas compatible with Anthropic and local grammar backends; reject out-of-bounds reports locally without trimming content or making automatic retry calls.
- Expose report rejection categories without private response text.
- Clarify the difference between recovery episodes and burner cycles, state changes and physical sensor freshness, and current controller state and historical behaviour.
- Interpret Recorder's naive database timestamps as UTC, keeping restart-gap boundaries correct in non-UTC HA timezones. Rebuild the current history era once and retain the previous era privately.

## 0.5.1 — provider diagnostics and repeatable history sampling

- Align sampling and daily backfill boundaries so overlapping reloads reuse the same sample grid.
- Rebuild the current recording era once while retaining the prior era privately.
- Expose provider error categories without private error text.
- Document the verified local profile output allowance, including Home Assistant prompt overhead.

## 0.5.0 — optional Heating Advisor

- Per-task AI Task profiles for daily summaries, weekly reviews and investigations.
- Extended OpenAI and Anthropic profile checks with tools disabled; provider-owned model/effort settings.
- Bounded evidence, strict report/reference validation and private retention of 20 reports.
- Optional local-time schedules, persisted call limits, timeouts and unload cancellation.
- Separate advisor options, status sensor and run/list report actions.

## 0.4.0 — history retention and survey compatibility

- Separate recorded-state availability from recent-change coverage; retain the previous conservative policy as an option.
- Exclude known Recorder downtime and use conservative expiry for unclean runs.
- Keep room/activity events; sample numeric boiler context every minute and controller intent every five minutes.
- Store controller context separately and compress historical payloads; migrate older stores with a distinct observation era.
- Honour supplied internal-wall/floor survey defaults with explicit provenance.
- Represent unsurveyed neighbours as named references, preserve qualifiers, and expose missing wall shares as advisories.

## 0.3.0 — read-only house survey

- Optional house.yaml and room-file loading with explicit thermostat mappings.
- Thermal/layout context in reports: dimensions, boundaries, neighbours, openings, construction U-values and radiator ratings.
- Preserved confidence labels, survey revision and validation warnings.
- House model status, standalone report and reload actions.
- Address, network, photo and occupancy-routine fields excluded from evidence.
- Survey changes preserve measured history; source files are never written.

## 0.2.0 — historical analytics

- Optional shared history, background Recorder backfill and bounded private storage.
- Coverage-gated comfort/demand metrics, observed recoveries and matched comparisons.
- Daily comparison, adjustment journal and structured report actions.
- Optional OT/boiler decision context, isolated from measured comfort metrics.
- Corrected selected-demand activity, independent expiry and Recorder boundary handling.
- Prior source era retained; corrupt/unsupported history preserved without overwrite.
- Legacy engine parity on 12,061 observations, nine rooms and four window sizes.

This version remains observation-only and makes no AI calls.

## 0.1.0 — observation foundation

- One setup/options flow for room and optional boiler/system sources.
- Shared timestamped observations with unit conversion and explicit source quality.
- Room temperature, commanded target, demand and positive-deficit sensors.
- Actual flow/return, flow-setpoint readback, heating/DHW and input diagnostics.
- Timed source expiry, event updates, unload cleanup and retained room identities.
- Quality-only downloadable diagnostics and deterministic manual-install ZIP.
- Implementation inventory, migration/handover design and future AI-profile architecture.

This development version has no control writes, legacy imports, history analytics
or AI calls. Existing integrations remain the controllers.
