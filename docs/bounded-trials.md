# Bounded heating trials

Status (20 September 2026): implemented and tested in code (issue #21); not yet
exercised on the live installation. A trial changes exactly one allowlisted tuning
parameter for a bounded time and then restores the baseline. It is the only way the
integration itself changes a tuning value on a schedule, and every step of it is an
explicit owner action through a service. Advisor or AI output never creates, approves
or starts a trial.

## What a trial may change

| Scope | Parameter | Bounds | Largest change per trial | Applied through |
|---|---|---|---|---|
| room | `trust_k` | 0 to 1 | 0.2 | `OTCoordinator.set_tunable` |
| room | `cap_up` | 0 to 3 K | 0.5 K | `OTCoordinator.set_tunable` |
| room | `cap_down` | 0 to 3 K | 0.5 K | `OTCoordinator.set_tunable` |
| boiler | `design_flow` | 30 to 80 °C | 5 °C | `BFCCoordinator.set_tunable` |
| boiler | `design_outdoor` | -15 to 10 °C | 2 °C | `BFCCoordinator.set_tunable` |
| boiler | `return_ceiling` | 30 to 70 °C | 5 °C | `BFCCoordinator.set_tunable` |

These are the same values the `number.*_control_*` entities expose. The allowlist is
closed: anything else is rejected with a validation error before any coordinator is
touched. Excluded on purpose: `dhw_delta` and every other DHW protection setting,
room and boiler modes, enable and occupancy switches, schedules, setpoints, and every
actuator service (`climate.*`, `number.set_value`, `ramses_cc.*`). The existing
controllers keep deciding and writing exactly as before; a trial only changes one
input to their decision.

## Lifecycle

`proposed` → `approved` → `starting` → `running` → `completed` | `stopped` |
`expired`, with `rejected` available from proposed/approved and `rollback_failed` →
`rolled_back` when a restore has to be retried. `starting` is the short-lived state
saved before the value is applied. Exactly one trial may be starting or running at a
time; approve, start and stop run under one lock so concurrent calls cannot race.

1. `propose_trial` (`scope` room ID or `boiler`, `parameter`, `target_value`,
   private `rationale`, `duration_hours` 1 to 168, optional `comfort_floor_c`,
   optional `recommendation_id`). Captures the current value as baseline and
   rollback value, checks bounds and max step, snapshots the current analytics
   window metrics for the affected rooms (all rooms for a boiler trial) and derives
   predeclared criteria: success = deficit/overshoot degree-hours not above baseline
   and within-band not below; stop = deficit or overshoot degree-hours more than
   1 K·h above baseline, optional comfort floor on measured room air, manual
   override, mode change, stale or guarded source. DHW activity is noted, never a
   stop and never touched.
2. `approve_trial` (`trial_id`, `confirm: true`). Refused unless controls are
   configured, ownership is `ready`, the scope is already `active` (room) or `auto`
   (boiler) with no boiler manual hold active, no activation blocker (`guard_reason`)
   exists and no other trial is running. A trial never activates control.
3. `start_trial` (`trial_id`, `confirm: true`). Re-checks the same conditions and
   that the current value still equals the baseline, then saves the trial as
   `starting` (baseline, rollback value, `started_at`, `expires_at`) and waits for
   that save to succeed before anything changes; if it cannot be persisted the start
   is refused and nothing is written. Only then is the target applied via the tuning
   write gate under the controls lock, read back and the trial saved as `running`.
   Readiness and the baseline are re-checked under that lock immediately before the
   write; if either changed, the saved intent ends `stopped` (reason
   `conditions_changed`) and nothing is written. The apply-and-persist step is
   shielded from cancellation; a start cancelled after the write is rolled back at
   once (reason `interrupted_start`).
4. Supervision runs every 60 s while starting or running (a `starting` trial is
   supervised exactly like a running one). A trial still `starting` more than two
   minutes after `started_at` was interrupted mid-start and is rolled back with
   reason `interrupted_start`. First breached condition wins:
   controls unavailable, expiry (→ `expired`), mode left active/auto, manual
   override (room `manual_setpoint` present, or boiler `manual_hold_active`; the
   hold itself is never touched), any `guard_reason`, comfort floor on measured air,
   deficit/overshoot stop criteria from the analytics report. All result in rollback.
5. `stop_trial` (`trial_id`, optional `complete: true`, private `note`) rolls back
   on demand.
6. `evaluate_trial` (`trial_id`, `outcome` improved/no_change/worse/inconclusive,
   private `note`) is allowed only for ended trials and records the predeclared
   criteria next to the measured window metrics. Measured room air (`air_temp`) and
   estimated operative comfort (`operative_temp`) are labelled separately. Every
   evaluation is tagged `evidence_type: association`; inconclusive is retained.
7. `get_trials` (`state`, `scope`, `include_private` default false) lists retained
   trials; private rationale and notes are excluded by default.

Every tunable write (start, rollback, restart rollback) passes one gate that
re-checks the scope is a controlled room or `boiler`, the parameter is on the
allowlist for that scope and the value is finite and within `min`/`max`; persisted
trial fields are never trusted. Rollback means the gated `set_tunable(parameter,
baseline)`, a successful controller store save and a matching `get_tunable`
readback. If any of these fails, the trial becomes `rollback_failed` and a Repairs
issue (`trial_rollback_failed`) is raised; supervision retries the rollback once a
minute up to 10 attempts, and `stop_trial` may be called again at any time. A
rollback is only counted as done once the baseline is persisted, so a restart cannot
bring the trial value back.

## Restart, unload and storage

- On startup any trial persisted as `starting`, `running` or `rollback_failed` is
  rolled back immediately and marked `stopped` with reason `restart`. A trial is
  never resumed automatically. Stored entries naming a parameter outside the
  allowlist are dropped if inactive, or kept only so the gate can refuse their
  rollback and flag them.
- On unload running trials are rolled back before the controllers stop.
- Storage is `.storage/home_heating_optimisation.<entry_id>.trials` (schema 1),
  bounded to 100 trials and 365 days. It is validated on load; a corrupt file makes
  trials read-only until reload and leaves heating control unaffected. A save failure
  keeps the trial in memory and never blocks control.
- Every transition is recorded as a `trial` journal event when the journal exists;
  journal failures are ignored.

## Entity

`sensor.home_heating_optimisation_trials` reports the running count with counts per
state, the running scope/parameter and `expires_at`. No rationale or note text
appears in entity attributes or diagnostics.

## What this does not claim

The analytics metrics used for criteria are window totals that include time outside
the trial and are affected by weather, occupancy and DHW. An evaluation is an
association, not a causal effect, and never a measured efficiency or energy saving.
Physical commissioning of the underlying controllers is described in
[commissioning](commissioning.md) and is a prerequisite for any trial.
