# Implementation plan and reuse inventory

## Milestone 1 — observation foundation (implemented)

An installable, observation-only integration with one configuration flow, stable
room IDs, shared timestamped observations, diagnostic entities, options/reload,
quality-only downloadable diagnostics and an automated install-package build.

No control policy, AI execution, history import or legacy configuration migration
is enabled in 0.1.0. Existing integrations can continue operating alongside it.
This is observation mode, not a shadow simulation of future control decisions.

## Reviewed source baseline

Reviewed local checkouts on 15 September 2026. Commit IDs identify the baseline
for later extraction; future ports must review any intervening changes.

| Project / revision | Reusable components | Important boundaries |
| --- | --- | --- |
| OT Thermostat Control / `d248d75` | `core/model.py` surface/OT calculations; `core/geometry.py` survey parsing; `core/policy.py` overrides and write decisions; associated physics/policy tests | Hub plus room entries; house surveys; climate binding; schedule provenance; restoration gates; separate physical estimates and empirical corrections |
| Boiler Flow Control / `95bdcd2` | `core/curve.py` weather curve; `core/control.py` DHW tracking, room feedback and bounded targets; `core/policy.py` mode/write arbitration; existing tests | DHW fallback and transitions; one-minute reassertion; manual holds; hardware limits; requested/sent/confirmed distinctions |
| Radiator Analytics / `0958ddb` | `analyzer.py` eligible episodes and matched comparisons; `backfill.py`; observation storage and adjustment journal; real HA/Recorder tests | Coverage and censoring; commanded-air definitions; noncausal response ratios; source mapping changes; archived legacy metrics |

Radiator Analytics includes an MIT licence. No root licence file was found in the
two control checkouts during this inventory. Confirm provenance/licensing before
redistributing extracted control code. The initial observer is newly implemented;
it has no runtime imports or dependencies on sibling projects.

## Milestone 2 — shared intent and analytics

Implemented in 0.2.0; see [definitions and validation](historical-analytics.md).
Live observation, backfill, house mappings and storage migration were verified in 0.4.0. Context currently includes allowlisted controller
outputs; complete schedule provenance and controller model versions remain future
work. The optional advisor follows before control ownership migration.

- Add explicit schedule, operative target, corrected-air target and decision-reason
  records. Associate units, provenance and model version with each field.
- Port analytics and their existing tests; add versioned persistence and Recorder
  backfill with historical gaps, source changes and configuration eras preserved.
- Keep current-input availability separate from historical observation coverage.
- Adopt stable room IDs for persistence and include monitored/unmonitored scope in
  system comparisons. Never infer hydraulic topology from room recovery slopes.

Acceptance: historical and live observations produce consistent metrics; missing
data remains unknown; source/definition changes cannot be merged silently.

## Milestone 3 — comfort and boiler control

- Port the pure model/policy code first and run its existing regression suites.
- Adapt coordinators to the shared input model without changing policy outcomes.
- Add independent shadow/active modes and restoration barriers for both modules.
- Preserve hardware constraints, DHW diagnostics, manual overrides and schedule
  handback. Treat mixed operation explicitly.
- Record attempted actions, actual service outcomes and acknowledgements separately.
- Complete the [migration and handover design](migration-plan.md) before enabling
  writes from an imported installation.

Acceptance: trace replay demonstrates behavioural parity; integration tests cover
restart, unload, invalid limits, failed writes, stale inputs and concurrent events;
handover tests demonstrate that only one controller owns each actuator.

## Milestone 4 — heating advisor

Initial AI Task profiles, bounded execution, optional schedules and retained reports
are implemented in 0.5.0; see [Heating Advisor](heating-advisor.md). Notifications,
conversation compatibility, follow-up outcomes and controlled trials remain future work.

- Select an existing Home Assistant AI Task profile per task. Provide conversation
  compatibility only where needed and validate its responses equally strictly.
- Provider integrations own keys, models, prompts and thinking settings. The heating
  integration owns evidence, scheduling, budgets, validated reports and user workflow.
- Add bounded calls, schema validation, report retention, evidence references and
  explicitly selected fallback behaviour. Never switch local work to a cloud
  provider implicitly.
- Evaluate local Gemma and Claude on identical evidence, including confounders,
  incomplete data, prior interventions and cases requiring no action.
- Store recommendations and follow-up outcomes before adding user-approved trials.

Acceptance: a provider failure cannot affect heating; a malformed or unsupported
recommendation cannot become a control command; reports distinguish evidence from
hypotheses and inferred comfort from measured air temperature.

## Milestone 5 — live evaluation and coordinated optimisation

Deploy observation/shadow functions, compare with the existing installation, then
perform controlled handover. Only after baseline validation add bounded coordination
or learning, one change at a time, with comfort criteria and rollback. Metered
energy with weather/DHW context is required for savings claims.
