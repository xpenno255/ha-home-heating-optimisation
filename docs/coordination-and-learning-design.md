# Optional bounded coordination and learning — design

**Status: Design, not implemented.** Dated 20 September 2026. Issue #22.

Nothing in this document is enabled, scheduled or partially built. No learning code exists in
the integration. This design is a precondition for later work and gives no permission to tune
the installation. It depends on #14 (commissioning), #17 (journal), #21 (approved bounded
trials) and #25 (metered energy evidence) being delivered and validated first.

## 1. Purpose and non-goals

Purpose: describe how HHO *could* later adjust a small, named set of controller parameters
using observed evidence, without weakening any existing safety invariant, and how such
adjustments would be validated, recorded and reversed.

Non-goals:

- No model-free or black-box controller. Learning only proposes values for parameters that
  already exist in the tested control code (`control/comfort/core/model.py`,
  `control/comfort/core/policy.py`, `control/boiler/core/curve.py`, `control/boiler/core/model.py`).
- No inference of new physics (U-values, emitter sizes, hydraulic topology) from control data.
- No change to actuator ownership (`Controls.can_write`/`guard_reason`), manual-hold handling,
  DHW protection, startup reconciliation or readback confirmation rules.
- No advisor/AI output ever writes a parameter. The advisor may *propose*; only validated
  integration code with an explicit owner approval per trial may *apply*.
- No energy-saving claim without metered energy (section 9).

## 2. Layers that must stay distinct

| Layer | What it is | Where it lives | May learning touch it? |
| --- | --- | --- | --- |
| Physical model | Steady-state surface balance from the house survey; `steady_state_mrt`, `required_air_temperature` | `comfort/core/model.py`, survey geometry | **No.** Geometry and U-values are survey facts, edited only by the owner via the survey. |
| Empirical correction | Scalars applied to the physical answer: `trust_k`, `cap_up`, `cap_down`, `asymmetry_a`; boiler curve and correction gains | `ModelParams`, `CurveParams`, `DemandCorrectionParams`, `ReturnCeilingParams` | **Yes, bounded** (section 3). |
| Measured air | Selected room air sensor after freshness/normalisation | `observations.py`, `air_temp`/`air_temp_source` | **No.** A measurement is never adjusted; only its provenance is recorded. |
| Estimated operative | `operative_temp` = f(measured air, modelled MRT at that air) | coordinator output | **No.** Derived from the two layers above; never fitted to a target. |
| Command lifecycle | requested/sent/pending/confirmed, readback statuses | coordinator, `docs/command-confirmation.md` | **No.** Learning consumes only `confirmed` outcomes as evidence and never relaxes confirmation. |

Rule: a learned value must be attributable to exactly one *empirical correction* parameter. If
an observed discrepancy could equally be explained by a survey error, a sensor placement issue
or an emitter/valve problem, the learner **abstains** and the discrepancy is surfaced as a
recommendation for a person, not absorbed into a parameter.

### Hydraulic topology

**Never infer hydraulic topology from room recovery slopes alone.** Recovery slope confounds
emitter output, valve state, flow temperature, adjacent-room heat exchange, solar gain,
occupancy and DHW priority. Radiator Analytics recovery metrics are used as *eligibility and
outcome evidence* (did the room reach target within the deadline under comparable
conditions), never as an identification signal for which rooms share a circuit or which
emitters are undersized. Any topology statement in the design requires the owner's survey.

## 3. Parameters that may be coordinated or learned

Each parameter has its own enablement flag (default **off**), hard bounds that the learner
can never propose outside, a maximum step per applied change, a minimum interval between
applied changes, and a provenance record. Bounds below are proposals for review; they are
narrower than the existing policy envelopes and the owner's configuration can only narrow them
further, never widen.

Provenance fields (identical for every parameter, stored per parameter):

```
value, previous_value, source ("default" | "config" | "owner" | "learned"),
learning_version, proposal_id, trial_id (#21), applied_at, applied_by ("owner" | "trial"),
evidence_window_start, evidence_window_end, eligible_observations, confidence,
rollback_value, rollback_deadline, journal_event_id (#17)
```

### 3.1 Per room (comfort)

| Parameter | Current default | Hard bounds | Max step | Min interval | Flag | What it may be learned from |
| --- | --- | --- | --- | --- | --- | --- |
| `trust_k` | 0.8 (`ModelParams.trust_k`) | 0.3 – 1.0 | 0.05 | 7 days | `learn.room.<id>.trust_k` | Confirmed-target episodes: sign-consistent residual between estimated operative at steady state and the scheduled comfort target, under sustained occupancy and no window override. |
| `cap_up` | 1.5 K | 0.5 – 2.0 K | 0.25 K | 14 days | `learn.room.<id>.cap_up` | Fraction of confirmed writes where `capped=True` upward **and** the room still failed to reach target within the analytics deadline. Never raised while any manual-hold episode in the window lowered the setpoint. |
| `cap_down` | 1.5 K | 0.5 – 2.0 K | 0.25 K | 14 days | `learn.room.<id>.cap_down` | Mirror of `cap_up` for downward capping with overshoot evidence. |
| `adaptive_shift` bounds | shift is 0.0 and the legacy adaptive setback is ignored (coordinator retires `adaptive_enabled`) | shift ∈ −0.5 – +0.5 K; bounds themselves fixed, only the applied shift within them may be learned | 0.1 K | 7 days | `learn.room.<id>.adaptive_shift` | Running-mean-outdoor eligibility from `hub.py` (enough full days) plus confirmed comfort outcomes. Proposals only; a non-zero shift is off until #21 approves a trial. |
| `solar_k` (as a per-room multiplier on modelled solar MRT rise, capped by `solar_cap_k`) | multiplier 1.0, `solar_cap_k` 2.0 | multiplier 0.5 – 1.5; `solar_cap_k` never learned | 0.1 | 14 days | `learn.room.<id>.solar_k` | Daytime clear-sky episodes with a measured or estimated GHI, comparing modelled vs observed air rise with heating **off** (demand 0) in that room. Requires an irradiance source or cloud fraction; otherwise abstain. |

Not learnable: `asymmetry_a`, `h_i`, `h_r`, `loft_delta`, `unheated_fraction`,
`zone_setpoint_min/max`, `override_minutes`, `manual_hold_minutes`, window timings,
occupancy offsets. These are either survey physics, safety envelopes or owner intent.

### 3.2 Boiler

| Parameter | Current default | Hard bounds | Max step | Min interval | Flag | What it may be learned from |
| --- | --- | --- | --- | --- | --- | --- |
| `design_flow` | 55 °C (`CurveParams.design_flow`) | 45 – 65 °C and always ≤ configured `flow_max` and ≤ hardware `max_flow_entity` | 2 K | 14 days | `learn.boiler.design_flow` | Sustained demand-correction saturation (`DemandCorrectionState.correction` at ±`max_k` for most of the heating hours in the window) in cold-band conditions, with confirmed flow readbacks. |
| `design_outdoor` | −3 °C | −8 – 0 °C | 1 K | 28 days | `learn.boiler.design_outdoor` | Only from the curve-fit residual across at least three distinct outdoor-temperature bands; never from a single cold snap. |
| `return_ceiling` | 50 °C (`ReturnCeilingParams.return_ceiling`) | 40 – 55 °C; never above `dhw_return_ceiling` − 5 K | 1 K | 14 days | `learn.boiler.return_ceiling` | Fresh return-temperature samples during settled heating with comfort not limited; requires a return sensor with < 10 % stale samples. |
| Demand-correction gains: `step_k`, `max_k`, `sustain_minutes` | 2 K, 8 K, 20 min | `step_k` 1 – 3 K; `max_k` 4 – 10 K; `sustain_minutes` 10 – 40 | 0.5 K / 1 K / 5 min | 28 days | `learn.boiler.demand_gains` | Oscillation and settling evidence in the filtered demand signal; requires at least 20 heating episodes with confirmed flow writes. `high_threshold`/`low_threshold` are not learned. |

Not learnable: `flow_min`, `flow_max`, all DHW parameters (`DhwParams`, `dhw_fallback_flow`,
`dhw_target`, progress/timeout), hysteresis/min-hold, manual-hold minutes, freshness limits,
`room_design`, curve exponent `n`. DHW protection is explicitly out of scope, as in the
readiness decisions.

### 3.3 Coordination (no learning)

Coordination means using one module's state as a bounded input to another's decision without
changing any parameter:

- Boiler curve may receive an aggregate "rooms capped upward and below target" count as an
  additional `comfort_limited`-style flag (already exists for return ceiling). Bounded: it can
  only *suppress* the return-ceiling trim, never raise flow above the curve + demand
  correction.
- Rooms may receive `flow_temp_used` (already exposed) to compute `radiator_output_w`;
  coordination does not let a room request a flow temperature.

Each coordination link has its own flag (`coordinate.boiler.comfort_limited`,
`coordinate.rooms.flow_awareness`), default off, and is validated in shadow like a learned
parameter.

## 4. Eligibility, confidence and abstention

Missing or conflicting data **leaves settings unchanged**. The learner's only outputs are
`propose(parameter, value, evidence)` or `abstain(parameter, reason)`.

Minimum eligible observations before any proposal (per parameter, per evidence window):

| Evidence type | Minimum | Definition |
| --- | --- | --- |
| Confirmed room command episodes | 30 | `write_status == "confirmed"` and the analytics episode not censored (`analyzer._episodes`); `matching_readback_unverified` and timed-out commands are **not** eligible. |
| Distinct days | 10 | Days with ≥ 60 % observation coverage for that room (analytics `quality`). |
| Outdoor bands | 3 (boiler curve) | Bands of 3 K width, each with ≥ 6 h of heating. |
| Occupancy | required for `trust_k`, `adaptive_shift` | Episode overlaps a period the occupancy sensor (if configured) reports occupied; without a sensor, only time-window-active periods count and confidence is capped at "low". |
| DHW context | required for boiler parameters | Episodes overlapping DHW active or the DHW fallback are excluded. |
| Weather context | required for `solar_k`, `design_outdoor` | Outdoor source fresh (< `outdoor_freshness_minutes`) for ≥ 90 % of the episode. |

Abstain when any of the following holds:

- Configuration era changed inside the window (survey edit, source rename, controller
  option change, controller version change). Evidence never spans eras.
- Any manual hold or window override covers > 20 % of the window for that room.
- The sign of the residual is not consistent in ≥ 70 % of eligible episodes.
- The proposed value equals the current value within the parameter's `max step`/2.
- Journal shows an unresolved `rollback` for the same parameter within 60 days.
- Recorder backfill status is not `complete` for the window.
- Two parameters would move in the same cycle for the same scope (one change at a time).

Confidence is categorical (`low`, `medium`, `high`) from eligible count and residual
consistency; only `high` may be proposed for a live trial, `medium` may be proposed for
shadow replay only, `low` is recorded as an abstention with reason.

## 5. Validation pipeline: replay and shadow before any trial

Every proposal passes three gates, in order. A failure at any gate records an outcome and
stops.

1. **Replay against the journal.** Re-run the pure functions (`required_air_temperature`,
   `decide`, `heating_target`) over the evidence window with the proposed parameter using the
   recorded inputs from the journal (#17) and analytics observations. Compare with the recorded
   decisions. Pass criteria: no new writes outside the configured bounds; no additional
   manual-override classifications; predicted setpoint change ≤ max step in every cycle;
   estimated operative residual improves in ≥ 60 % of episodes and worsens in none by more
   than 0.3 K.
2. **Shadow run.** Apply the proposed parameter to a shadow copy of the coordinator for a
   minimum of 7 days (rooms) or 14 days (boiler) while the live controller keeps its current
   value. Shadow decisions are journalled as `decision` with `origin: "learning_shadow"` and
   the `proposal_id`. Pass criteria: same as replay, evaluated on live data; shadow never
   attempted a write (shadow can't, by existing invariant).
3. **Owner-approved trial (#21).** The passed proposal becomes a trial specification: exact
   parameter, current and proposed value, evaluation window, success criteria, comfort
   guardrails, rollback value and deadline. Application happens only through the #21 trial
   code path, with the owner's explicit approval recorded. The trial applies at most one
   parameter change per scope at a time.

During a trial, the following remain in force and are unchanged by this design:

- Manual holds are detected and honoured exactly as today; a manual change during a trial
  pauses evaluation for that room and never counts against or for the proposal.
- DHW constraints, fallback flow and cylinder protection are untouched.
- Readback confirmation rules are unchanged; unverified renewals remain unverified.
- Exclusive actuator ownership and the guard reasons are unchanged.

## 6. Rollback

- Every applied change stores `rollback_value` (the value before the change) and a
  `rollback_deadline`. If the trial's success criteria are not met by the deadline, or any
  guardrail trips (room below target for > 2 h during scheduled comfort with confirmed
  commands; boiler flow at `flow_max` for > 4 h; return above ceiling + 5 K; any repairs issue
  from the controllers), the value reverts automatically and a `rollback` journal event is
  recorded with the reason.
- Owner rollback is one service call (`rollback_learning`, planned) that reverts *all*
  learned values in a scope to their `rollback_value` and disables that scope's learning
  flags. It never touches survey, sources, ownership or DHW.
- Reload/restart: learned values persist in a dedicated store
  (`home_heating_optimisation.<entry_id>.learning`), validated on load, read-only on
  corruption, bounded (≤ 50 parameters × 20 history entries). On corruption the controllers
  use configured/default values and a repairs issue is raised; live control continues.
- A learned value is never restored if its `learning_version` is newer than the running
  integration or if its `config_era` no longer matches; it falls back to `rollback_value`.

## 7. Recording: learning version and outcomes

- `learning_version` is a string constant in the (future) learning module, bumped whenever
  eligibility rules, bounds or the proposal function change. It is written into every
  proposal, abstention, shadow decision, applied change and rollback, alongside the existing
  `controller_version`, `control_schema`, `model_version`, `schedule_source` and
  `config_era` journal provenance.
- Journal kinds used: `recommendation` (proposal/abstention with evidence summary),
  `trial` (approved spec, start, end, outcome), `adjustment_note` (applied change with
  provenance), `rollback`. Evidence summaries are allowlisted numbers only; no free text
  beyond an optional `private_note`.
- Outcomes are one of `proposed`, `abstained`, `replay_failed`, `shadow_failed`,
  `trial_rejected`, `trial_running`, `trial_succeeded`, `trial_failed`, `rolled_back`,
  each with timestamps and the criteria values that decided it.
- Diagnostics export includes learned values and provenance but never evidence rows or
  private notes.

## 8. Entities and services (planned, not built)

- `sensor.home_heating_optimisation_learning` — count of active proposals, running trials,
  rollbacks in 30 days; attributes list parameter, scope, confidence, outcome (no evidence
  rows).
- Options-flow section "Learning (advanced)" with one switch per flag from section 3, all
  default off, plus "rollback all learned values" button.
- Services: `get_learning_proposals` (read, `SupportsResponse.ONLY`), `rollback_learning`
  (scope, requires the controller for that scope to be in shadow or the owner flag
  `allow_live_rollback`).

All follow the existing patterns in `services.py`, `strings.json`/`translations/en.json`,
`services.yaml` and the store house style.

## 9. Energy claims

Learning outcomes are judged on **comfort and control criteria only** (target attainment,
overshoot, capping frequency, flow/return behaviour, cycling as a diagnostic). Any statement
of energy saving requires the metered-energy evidence from #25: gas/electric meter or boiler
energy counter, degree-day or outdoor-temperature normalisation, DHW volume/energy separated,
and comparable coverage in both windows. Demand percentages, burner power samples,
estimated efficiency or cycling counts are never presented as savings. If #25 is unavailable
for a trial, the outcome record states `energy: not_evaluated`.

## 10. Phased implementation plan

Each phase ships behind flags that default off, with the full suite green and focused tests
listed. No phase may start before #14, #17 and #21 are merged and commissioned.

### Phase 0 — Evidence readiness (depends on #17, #25)

- Journal exports include the fields the learner needs (`config_era`, confirmed lifecycle,
  occupancy, window/manual flags, DHW active, outdoor freshness).
- Tests: journal fields present in synthetic fixtures; era change splits windows; unverified
  commands excluded from eligible counts.

### Phase 1 — Pure eligibility and proposal functions

- `learning/core/eligibility.py` and `learning/core/propose.py`: pure functions with no HA
  imports, one per parameter, returning `Proposal | Abstention`.
- Tests: bounds never exceeded across randomised inputs; max-step clamp; every abstention
  rule in section 4 triggers on a fixture; consistent-sign threshold; one-change-per-scope;
  confidence categories; `learning_version` embedded.

### Phase 2 — Replay harness

- `learning/replay.py`: reruns pure control functions over journal + analytics windows with a
  proposed parameter; produces the section 5 gate-1 metrics.
- Tests: replay of unchanged parameter reproduces recorded decisions exactly (parity);
  proposed change never yields out-of-bound writes; residual metrics computed on fixtures;
  replay failure is recorded and stops the pipeline.

### Phase 3 — Shadow evaluation and storage

- `learning/coordinator.py` running shadow copies; `learning/store.py` with validation,
  bounds, read-only fallback.
- Tests: shadow never calls services (patched `_perform` asserts zero calls); store corruption
  → read-only, controllers use defaults, repairs issue raised, control continues (failure
  isolation); restart with mismatched `learning_version`/era falls back to `rollback_value`;
  size bound enforced.

### Phase 4 — Trial hand-off and rollback (depends on #21)

- Convert passed proposals into #21 trial specs; automatic guardrail rollback; owner
  `rollback_learning` service; sensor and options flags.
- Tests: application only via #21 path with recorded approval; guardrail trips revert and
  journal `rollback`; manual hold during trial pauses evaluation and leaves policy untouched;
  DHW parameters unchanged by any code path (assert on `DhwParams`); one parameter per scope;
  strings/en.json parity; diagnostics excludes evidence rows.

### Phase 5 — Live evaluation (owner-scheduled)

- Cold-weather shadow comparison, then a single approved trial per scope. Outcomes recorded
  per section 7; energy statements only per section 9.

## 11. Open decisions for the owner

1. Whether `adaptive_shift` should be in scope at all, given the legacy adaptive setback was
   deliberately retired.
2. Preferred hard bounds for `design_flow` relative to the hardware `max_flow_entity` (this
   design proposes ≤ min(`flow_max`, hardware max)).
3. Whether shadow duration minimums (7/14 days) are acceptable or should be expressed in
   heating hours.
