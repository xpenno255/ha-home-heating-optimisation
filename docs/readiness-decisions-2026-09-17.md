# Readiness release decisions — 17 September 2026

## Scope

The owner authorised building, deploying and publishing the readiness changes after the specification audit, and delegated routine decisions while unavailable. This release targets reliable replacement of OT Thermostat Control and Boiler Flow Control: controller configuration, explicit radiator command confirmation, source rename continuity, preserved control behaviour, deployment and rollback verification.

The full original roadmap is not declared complete. AI conversations, notifications, recommendation outcome workflows, controlled experiments and coordinated learning remain later work. Automatic transfer of legacy registry identities, statistics and Radiator Analytics history/notes also remains outstanding. Existing records are retained rather than relabelled as new measurements.

## Decisions for the owner to review

1. **Remain in shadow.** Software deployment does not authorise the unsupervised physical heating trial. Both control engines remain in shadow; the nine old OT room entries, OT hub and Boiler Flow Control stay disabled but installed for rollback. RAMSES/Evohome and EMS-ESP remain required device connections.
2. **Keep the selected room sensors.** Preserve the previously agreed OT room-air sources and fallback behaviour. Observation air and control air may intentionally differ; the configuration interface must label them explicitly rather than silently replacing either selection.
3. **Require deliberate ownership after actuator changes.** New standalone setup and changes to the controlled thermostat/boiler number require the handover action again. Configuration changes are refused while any controller is active/auto, including persisted active selections. Saving configuration leaves controllers in shadow.
4. **Interpret confirmation narrowly.** Service success means HA accepted the call. Confirmation means a subsequent eligible thermostat state report matches the request; it does not prove valve movement or room heat delivery. No assumed confirmations are restored after restart. Failed, pending and timed-out readbacks remain visible.
5. **Preserve old identities and history.** Do not automatically rename legacy output entities or rewrite historical statistics in this readiness release. Update dashboard/automation consumers explicitly when retiring the originals. Source entity renames are a separate operation and update current source mappings only.
6. **Publish a commissioning prerelease.** Use v0.6.1 to distinguish this build from the locally deployed v0.6.0. Publish source and a deterministic install archive, with the lack of physical commissioning stated. Do not describe it as a completed cold-weather performance evaluation.
7. **Keep deployment evidence private.** Household configuration, live states, backups and survey files stay in ignored local-data. Copied survey test fixtures are replaced by synthetic examples before publishing. Retain provenance of the controller code from the owner's existing projects.

8. **Do not revive ignored legacy limits silently.** The effective room bounds use new `zone_setpoint_min` / `zone_setpoint_max` fields, defaulting to the existing 5–35 °C policy envelope. Old `min_setpoint` / `max_setpoint` values were not read by the current engine and are not suddenly applied on upgrade. New bounds constrain consolidated override writes; schedule handback restores the device's own schedule.

## Work split

GPT-5.6 Sol implements configuration, GPT-5.6 Terra implements command confirmation and provenance, and GPT-5.6 Luna implements source rename handling. The primary agent reviews their changes, adds cross-module failure checks, runs the complete suite, deploys the exact tested artifact and verifies GitHub checks.

## Commissioning still required

Follow [the commissioning checklist](commissioning.md) when the owner is available. Automated tests and matching shadow decisions establish software behaviour, not actual heat output, achieved comfort or energy savings. No tuning, AI trial, or hardware setpoint is changed as part of this deployment.
