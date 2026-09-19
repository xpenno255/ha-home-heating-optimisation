# Changelog

## Unreleased — radio fault recovery

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
