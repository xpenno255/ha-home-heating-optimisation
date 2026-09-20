# Historical analytics

## What is available

Enable **Historical analytics** in the integration's system options. Defaults:
seven-day analysis window, reports every 15 minutes, ±0.3 °C commanded-air comfort
band, and a 120-minute recovery deadline. The supported window is 3–14 days.

Each room gets temperature/target coverage, demand coverage, time in band, demand
active share, deficit/overshoot degree hours, observed warm-up rate, successful
recovery time, recovery success, scored recovery count and matched concurrent
response ratio. System sensors show analysis status, daily comparison and journal
count. These are rolling-window values, not accumulating energy counters. Metered
energy, when meters are configured, is recorded separately; see
[metered energy evidence](energy-evidence.md).

Time-in-band and degree-hour sensors require at least 80% temperature/target
coverage; demand share requires 80% demand coverage. Below that, sensors and exported
reports return unknown for those metrics. Recovery metrics describe eligible
observed episodes; they are not extrapolated to missing time. A recovery needs a
known start and five minutes continuously in band. Changed targets, unknown starts
and missing temperature periods are censored. Afterheat may complete a recovery.

The response ratio compares matched observed warm-up ramps with and without other
**monitored** room demand. It needs at least five pairs across three independent
baseline and comparison days, known water/outdoor temperatures and resolved
heating/DHW context. It describes an association, not radiator power, hydraulic
balance or the effect of changing a valve. Unmonitored loads remain a limitation.

## Input definitions and reproducibility

Live observation sensors retain their `last_reported` expiry policy. Since 0.4.0,
history defaults to **recorded state availability**: unchanged available readings
remain usable within a clean Recorder run until changed or marked unavailable.
Known Recorder stop/start boundaries clear carried readings, and actual new events
are required after restart. Unclean Recorder runs use the earlier conservative
age limits because the exact failure time is not known. The database run table
is read-only and accessed through Recorder's executor.

Coverage is known-state duration, not proof of fresh physical samples. Reports and
sensor attributes separately expose `recent_change_coverage` and
`demand_recent_change_coverage`: the share with a source update within 30 minutes.
Recorder cannot reconstruct every identical report, so these fields describe
**changes**, not sample acquisition. A frozen available upstream sensor can remain
available in this mode; the live input-quality sensors and recency fields remain
important. Select **Require recent state changes** in options to use the previous
age-limited history policy.

Historical active demand means the selected normalised demand is greater than
zero. Missing/unavailable is never treated as idle. Commanded targets are not
operative-comfort measurements. The policy and observation semantics version form
part of the history era, so a change rebuilds current history from Recorder.

Backfill requests actual events only in daily batches, with one day of leading
context beyond the maximum 14-day window. It does not trust Recorder's synthetic
start state timestamp. It runs in the background and never delays the observer.
If Recorder is absent or fails, live collection continues. There is no automatic
backfill retry until reload.

Since 0.5.1, collection records room and activity events plus five-minute samples aligned to UTC clock boundaries. Numeric sampling uses fixed minute buckets and backfill batches end at UTC midnight. Overlapping reloads therefore reuse the same sample grid instead of creating shifted duplicates. This recording-semantics change rebuilds the current era once, retaining the previous era privately. Numeric
system context is sampled at most once per minute, with availability transitions
retained immediately. Matching-condition averages can therefore differ within this
one-minute sampling resolution. Controller intent alone does not create a whole
measurement snapshot; it is retained separately at five-minute intervals. A clean stop writes an unknown boundary. Abrupt termination is bounded
by the existing freshness limits, but its exact time cannot be recovered without
Recorder evidence. Both daily comparison windows and the primary report share one
calculation time. Daily comparisons are descriptive and require coverage in both
24-hour periods.

## Storage and adjustment journal

The integration owns `.storage/home_heating_optimisation.<entry_id>.history`.
Version 0.4.0 stores historical collections as compressed JSON inside the atomic HA
Store envelope. Compression/decompression runs in an executor; old payloads are
read and migrated. Source files and private external backups are not altered.
It never reads or changes the live Radiator Analytics store. Keep this file private:
it contains household history and notes. Downloadable diagnostics contain counts
and quality only. Notes are not published in entity attributes or copied into
Recorder by the integration; service calls can still appear in HA traces/events.

Current history is limited to 15 days plus a boundary observation and 50,000 points.
Controller context has a separate limit of 4,321 five-minute samples, roughly 15
days, and is not duplicated in every measurement record.
If the point cap is reached, oldest points are dropped and `history_truncated` is
reported; a busy installation may retain less than the selected window. Saves run
with reports, adjustment actions and clean shutdown. An abrupt stop can lose the
unsaved interval; Recorder can refill recorded inputs on restart.

Source mappings, temperature unit and observation semantics identify a history
era. A change rebuilds the current timeline using the new mappings. One prior era
(up to 50,000 points) remains archived in the private store; it is not analysed or
exported as current evidence. Further mapping changes replace that archive. Room
names, report intervals, tolerance and recovery deadline do not change the source
era: retained observations can be recalculated with those settings.

The journal retains up to 1,000 notes and then rejects additional notes instead of
silently deleting them. Removing/disabling the integration preserves this private
store; deleting the store manually removes history and journal. Unsupported or
malformed payloads enter read-only storage mode and are preserved for inspection.
Failed saves retain memory state and retry on the next save.

Use Developer tools → Actions to record a change already made:

```yaml
action: home_heating_optimisation.record_adjustment
data:
  note: "Checked the radiator inlet temperature; no setting changed"
  kind: other
```

Kinds: `lockshield`, `boiler_setting`, `sensor_change`, `other`. An optional `room_id`
uses the stable ID returned by `get_report`. Notes are limited to 500 characters.
Every journal entry separates comparison regimes conservatively, including scoped
room changes because other rooms may experience changed load. This action writes
only a note; it does not adjust a valve, thermostat or boiler setting.

## Importing Radiator Analytics history

Status (2026-09-20): implemented for issue #24; not yet run against a household
store. Recorder backfill is not a substitute for the legacy store, which holds
per-sample observations and the adjustment journal.

Three response-only actions handle the legacy `.storage/radiator_analytics` file
(HA envelope version 1, payload schema 2). Any other version, a missing file or
undecodable JSON is reported as a status and nothing is imported.

1. `preview_history_import` reads the file without changing it and returns its
   SHA-256 checksum, each legacy zone with observation count and time span, note
   and archived-session counts, and a proposed mapping from legacy zone to HHO room
   ID: the configured climate entity first, then the room ID, then a unique
   case-insensitive name match, otherwise `unmapped`. Two rooms sharing a name give
   `needs_mapping` with the candidates. Pass `mapping: {"climate.x": "room_id"}` to
   override (`null` skips a zone). Counts separate importable observations and notes
   from skipped invalid/duplicate points and unmapped zones.
2. `import_history` with `confirm: true` copies compatible observations, with their
   original timestamps and field values, into a separate `imported_eras` collection
   in the private history store. The live `observations` list, and therefore the
   sensors and reports, are unchanged. Legacy `legacy_sessions` aggregates are
   archived inside the era for inspection, never analysed. Notes become adjustments
   with `kind: "imported"`, `source: "radiator_analytics"` and their original time;
   they appear in `get_report` like other notes and stay out of diagnostics, entity
   attributes and AI evidence. Because they are real past adjustments they also
   separate comparison regimes for ramps recorded after them. Each import is
   idempotent by checksum (`already_imported`), builds the full payload before a
   single save, and rolls memory back if the save fails, so an interruption leaves
   both stores as they were. At most three eras and 50,000 points per era are kept;
   older points are dropped and `truncated` reported.
3. `retire_legacy_store` with `confirm: true` renames the file to
   `.storage/radiator_analytics.retired-<date>` only after the Radiator Analytics
   integration is no longer loaded and an import matching the current file checksum
   has been saved. Content is never rewritten; delete the retired file yourself
   after checking the import.

Legacy observations used `hvac_action` for demand activity and 30-minute freshness
from `last_updated`; the era signature records this so imported points are never
mistaken for current-semantics history. Imported eras are counted in diagnostics
(`imported_era_count`, `imported_observation_count`) without content.

## Structured report and controller context

```yaml
action: home_heating_optimisation.get_report
response_variable: heating_report
```

This response contains room IDs/names, gated metrics, coverage, recommendations,
comparison windows, settings, notes, storage/backfill status and the last 100
samples of optional controller context. It is ready as evidence for a later advisor;
it does not call an AI provider or schedule AI reports.

Optional room inputs: OT comfort target, corrected air target, estimated operative
temperature and decision state. Optional system input: Boiler Flow Control's Flow
Setpoint decision sensor. Context retains state, unit, update time and an allowlist
of decision attributes such as reason, requested/sent/confirmed target and write
status. It is controller-reported intent and estimates, with original timestamps,
not a new measurement of comfort or proof an actuator responded.

Since 0.6.1 the allowlist also captures `schedule_source`, `model_version`, the
command lifecycle timestamps (`requested_at`, `sent_at`, `pending_since`,
`confirmed_at`, `readback_at`) and the requested/sent/pending/confirmed targets.
These are sampled on the five-minute grid, so short-lived states between samples
are not recorded here. Since 2026-09-20 the separate
[intervention journal](intervention-journal.md) records each decision change,
command, service result, readback transition, manual hold, schedule change, mode
change, handover and adjustment note as an event with controller, configuration and
model provenance, so short-lived actions between samples are retained. The sampled
context remains the measurement-aligned view; the journal is the action record.
Only the last 100 samples are exported; the full bounded timeline stays in private storage.

## Validation and legacy differences

The engine is adapted from Radiator Analytics, MIT, revision
`0958ddbf4861b26c1bfefdd2191f5814c9db62d3` (Copyright 2026 xpenno255).
Its original regression cases are retained, with added ingestion, context-expiry,
unknown-demand start, persistence, lifecycle and real Recorder tests.

A private saved trace of **12,061 observations across nine rooms** produced identical
zone and system results between both engines for **1-, 3-, 7- and 14-day** windows.
Reproduce with a trusted legacy checkout and private history:

```bash
.venv/bin/python -m scripts.compare_legacy \
  --legacy-component ../RadiatorAnalytics/ha-radiator-analytics/custom_components/radiator_analytics \
  --history local-data/radiator-store.json
```

This establishes calculation parity on identical stored input, not identical
end-to-end outputs. New ingestion intentionally differs:

- Selected demand replaces `hvac_action` for demand activity and uses its own expiry.
- Available commanded targets persist when separate air readings remain fresh.
- System context follows the selected availability/age policy across complete ramp intervals.
- Unknown demand starts cannot produce scored recoveries.
- Recorder synthetic start states cannot refresh old measurements.

State availability and recent-change coverage should be reviewed together over
real heating cycles before drawing optimisation conclusions. High availability
alone does not establish measurement accuracy or fresh physical samples.
