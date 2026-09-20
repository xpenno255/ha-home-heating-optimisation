# Intervention journal

Implemented 2026-09-20 (issue #17). The journal is a complete, event-by-event record of
what the controllers decided, what they asked Home Assistant to do, what the service
layer reported, what the actuator read back, and what people changed. It complements
the sampled controller context in [historical analytics](historical-analytics.md), which
is aligned to five-minute measurements and cannot show actions that start and end
between samples. Nothing in the journal is a measurement of comfort or proof that a
device physically responded; readback events say only what the source reported.

## Event schema (version 1)

Every event is a JSON object with these fields:

| Field | Meaning |
| --- | --- |
| `schema` | Event schema version, currently `1`. Old events are kept as written. |
| `id` | Random 32-character hex identifier. |
| `time` | UTC epoch seconds when the event was recorded. |
| `kind` | One of the kinds below. Unknown historical kinds are preserved on load. |
| `room_id` | Stable HHO room id, or `null` for boiler/system events. |
| `scope` | `boiler`, a room id, `system`, or `null`. |
| `origin` | `controller`, `user`, `service`, `source`, `advisor` or `unknown`. Anything else is stored as `unknown`. |
| `data` | Small JSON-safe dict. Always contains `outcome`; when the producer cannot say, it is `"unknown"`. |
| `provenance` | `controller_version`, `control_schema`, `config_era`, `model_version`, and `schedule_source` when known. |

`config_era` is a short hash of the actuator bindings (flow setpoint entity and
primary climate per room) plus the set of room ids. Renaming a room or changing an
observation sensor does not start a new era; rebinding an actuator does.

Kinds: `decision`, `command_requested`, `command_sent`, `command_result`, `readback`,
`manual_override`, `schedule_change`, `mode_change`, `handover`, `rollback`,
`adjustment_note`, `gateway`, `energy`, `recommendation`, `trial`, `advisor_report`,
`advisor_followup`, `migration`. The last eight are reserved for other features and
are accepted by the API today.

## What is recorded

- **Comfort rooms**: a `decision` whenever state, action, reason or target changes
  (with zone bounds, schedule setpoint, model air setpoint and measured air);
  `command_requested`, then `command_sent` and `command_result` from the single
  `ramses_cc.set_zone_mode` call (outcomes: `service_succeeded`, `service_failed`,
  `blocked`, `no_actuator`, `refused_out_of_bounds`); a `readback` whenever the
  readback status, timeout flag, pending or confirmed target changes (including
  `readback_no_echo` with `timed_out: true`, `confirmed`, `readback_reverted`);
  `manual_override` when a manual hold is set or cleared; `schedule_change` when the
  schedule setpoint or its source changes.
- **Boiler**: a `decision` when mode, action, reason or target changes (with entity
  bounds, curve value, outdoor temperature and demand); a DHW `decision` with
  `subject: "dhw"` on each DHW activity/status transition; command events from the
  single `number.set_value` call; `readback` on write-status transitions
  (`pending_readback`, `unconfirmed_readback`, `confirmed`, `service_failed`);
  `manual_override` when the manual hold is detected or expires.
- **Control runtime**: `mode_change` for every mode selection (from/to), `handover`
  and `rollback` from the ownership services, including interrupted handovers.
- **People**: `record_adjustment` mirrors each note as `adjustment_note`; the note text
  is stored under `data.private_note` and is never exported by default.

Decisions are recorded on change, not every cycle, so an unchanged shadow decision
does not grow the journal each minute. Commands and readbacks are recorded as they
happen, so a write that is sent and reverted within a minute produces both events.

## Persistence, bounds and failure behaviour

Events live in `.storage/home_heating_optimisation.<entry_id>.journal`. Saves are
debounced: 30 seconds after the last record, on Home Assistant stop and on unload.
The store keeps at most 20,000 events and 30 days; older events are pruned at save
time and `truncated` is reported when the cap dropped events. On load the file is
validated; an unreadable or unsupported file is preserved untouched and the journal
runs read-only for that session (`storage_read_only`). A failed save keeps events in
memory (`save_failed`) and is retried with doubling delays (30 s, 1, 2, 4, 8 min
cap); after ten consecutive failures retries stop until the next record, and a
successful save resets the count. Recording is synchronous and
never raises; every producer hook is wrapped so a journal fault cannot block a
heating decision or a command. Restart is idempotent: events are appended with fresh
ids and nothing is re-recorded from the file.

Set **Enable intervention journal** off in the integration options (System step) to
disable recording; the status sensor then reports `disabled`.

## Reading the journal

`sensor.home_heating_optimisation_intervention_journal_status` reports `ready`,
`storage_read_only`, `save_failed` or `disabled`, with `event_count`, `oldest_at`,
`newest_at`, `counts_by_kind` and `truncated` attributes. Diagnostics downloads
contain the same counts and status only.

```yaml
action: home_heating_optimisation.get_journal
data:
  kinds: [command_sent, readback]
  room_id: study
  hours: 48
response_variable: journal
```

`hours` defaults to 24 and is capped at 720. The response is
`{"events", "count", "truncated", "status"}` with at most 2,000 events (the most
recent are kept). `include_private: true` adds the note text; by default
`private_note` and other free-text keys are stripped, which is also how other
features must consume the journal as evidence.

## For other features

```python
journal = getattr(heating, "journal", None)
if journal is not None:
    journal.record("recommendation", room_id="study", scope="study", origin="advisor",
                   data={"outcome": "proposed", "recommendation_id": "..."})
```

`journal.events(kinds=None, room_id=None, since=None, until=None, limit=None)` returns
copies sorted by time; `since`/`until` accept an aware or naive (read as UTC) `datetime`,
an epoch number or an ISO 8601 string, and are compared against the stored epoch `time`.
`journal.export(include_private=False)` returns the allowlisted form. Free text belongs only under `private_note`.
