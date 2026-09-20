# Consolidated control (0.6.1)

This commissioning prerelease includes both existing control engines. See [controller configuration](control-configuration.md) for standalone setup and editing, [command confirmation](command-confirmation.md) for readback semantics, and [commissioning](commissioning.md) before activation. Comfort control adjusts RAMSES radiator-zone setpoints; boiler supervision adjusts the EMS-ESP flow-temperature number. RAMSES/Evohome and EMS-ESP remain the device integrations. The old OT Thermostat Control and Boiler Flow Control integrations are no longer runtime dependencies after import and handover.

The imported engines retain the existing weather curve, DHW handling, hardware limits, manual holds, room survey model, occupancy settings and selected room-air sensors. AI reports cannot select control modes or issue actuator commands.

## Import existing controllers

An existing imported installation does not need to import again when upgrading from local v0.6.0. Its settings and ownership stores are preserved. Standalone setup is also available through integration options; it requires explicit ownership handover before activation.

Run `home_heating_optimisation.preview_control_import` to inspect the import. The supported source configuration versions are OT v2 and BFC v1 (the current OT 2.1.2 and BFC 0.3.0 releases). All existing controlled rooms must map uniquely to the selected HHO thermostat entities. Load the original controllers and survey before importing.

Run `home_heating_optimisation.import_controls`. It copies configuration, live tunables, enable/occupancy settings and usable cached schedules into independent HHO storage. It never adopts legacy last-write memory or actuator ownership. Existing source configuration is fingerprinted; changes after import block handover.

The survey is copied into `home_heating_optimisation/house` beneath the HA configuration directory. Existing differing destination files are preserved and block import. Original files and integrations remain intact for rollback. A repeated import returns `already_imported` rather than overwriting the consolidated configuration.

Both engines start in shadow. `get_control_report` returns per-room predictions and boiler targets, with ownership blockers. Control sensors expose selected room air separately from the original thermostat-air observations: the two measurements are not assumed identical. Existing HHO observation entity IDs are retained. New control entities start with their own IDs; legacy entity IDs and their history are not transferred or deleted by import. After handover, the optional [identity migration](#identity-migration) can move compatible legacy output IDs onto the consolidated entities.

## Boiler telemetry freshness

For the imported EMS-ESP heating-active, burner-power and actual-flow sources, HHO discovers the simple field mappings from MQTT discovery and subscribes to their payload topic. It uses receipt of a non-retained payload containing the relevant valid field as the report timestamp. Repeated off/zero values therefore remain fresh even when HA's MQTT entities suppress unchanged state writes.

Retained startup messages do not establish liveness, malformed/missing fields cannot refresh a value, underlying entity unavailability remains unavailable, and silent inputs still expire. HHO does not publish MQTT messages or alter the source entities/discovery configuration. MQTT is required only when these telemetry bindings are configured.

## Exclusive handover and rollback

Leave every controller in shadow for comparison. Then run `home_heating_optimisation.handover_controls`. This journals the transition, disables and unloads the retained legacy config entries, verifies they are stopped, and only then records completed ownership. **Handover still leaves the consolidated controls in shadow.**

Each room has a shadow/active select. The boiler has shadow/auto/hold. Active selections are refused until ownership is complete; every actuator call checks ownership again. Enabled legacy entries block writes even when those entries report shadow. Relevant enabled automation writers and currently running writer scripts also block activation where their loaded action configuration can be identified. Idle manual scripts (including Heating Extra Boost) remain usable; externally scheduled clients and dynamically generated calls still require a commissioning inventory.

There are independent room/boiler enable switches, occupancy switches, a global comfort switch, comfort trust/cap numbers, boiler design/return/DHW tuning numbers and a DHW diagnostic reset button. Mode and enable settings are saved before taking effect. A restart restores active selections only after completed ownership and initialization; interrupted handovers and corrupt/unwritable controller storage cannot grant permission to write.

Run `home_heating_optimisation.rollback_controls` to stop new writes and restore the legacy entries disabled by handover. Legacy settings remain as they were; if they were in shadow, rollback restores shadow. An in-flight actuator call is allowed to settle before the old owner can be restored. Retain the old integrations until the active trial is complete.

## Identity migration

Status: implemented 2026-09-20 ([#23](https://github.com/xpenno255/ha-home-heating-optimisation/issues/23)); not yet run on a live installation.

Identity migration is separate from actuator activation. It runs only after `handover_controls` has recorded `ownership: ready`, while every legacy entry is disabled and unloaded and the consolidated controls are in shadow. It never changes ownership, modes or actuator settings, and the one-writer handover guards are unchanged.

`preview_identity_migration` (read-only) lists every registry entity belonging to a supported legacy entry (OT v2, BFC v1) with an explicit mapping:

- `transfer`: the physical meaning, unit and statistics semantics are unchanged. Room `state`, `air_setpoint`, `would_write`, `air_temp`, `schedule_setpoint` and `flow_temp_used`; boiler `mode`, `flow_setpoint`, `return_temperature_used`, `cycles_10min`, `cycling_status`, `dhw_status` and the DHW active binary sensor.
- `archive`: estimated or revised metrics (`target_ot`, `operative_temp`, `offset_final`), metrics with no consolidated equivalent, legacy write memory, and any entity whose registry unit differs from the consolidated one. Their legacy registry entries and history stay exactly as they are under the legacy ID and are never relabelled as the new definition.
- `skip`: control inputs, legacy entities disabled by the user, rooms without a consolidated match, entities already transferred.

Each mapping shows the legacy and consolidated unique IDs and current entity IDs, whether the legacy ID is held by another entity (`collision`), whether long-term statistics exist for it (`has_statistics`, `null` when the recorder is unavailable) and its known consumers: enabled or disabled loaded automations and scripts, storage and YAML dashboards that can be read, and HHO's own configuration. Consumers are inventoried, never edited; templates and YAML files must be reviewed by hand. An unsupported legacy version reports `status: unsupported` and blocks migration. A user-renamed legacy entity is followed through its registry identity and its current ID is the one transferred.

`migrate_identities` (`confirm: true`) runs only when the preview is `ready` and no collision exists. For each transfer it journals the intent and a snapshot of the legacy registry entry in `home_heating_optimisation.<entry_id>.identity_migration` (schema 1, bounded, read-only on corruption), removes the legacy registry entry, and renames the consolidated entity onto the legacy entity ID with the standard registry API. Home Assistant's recorder moves states and long-term statistics with an entity ID rename, so the legacy series continues under the transferred ID without a gap in the statistics table. Progress is journalled per item and to the migration journal where available; an interrupted run resumes from the recorded stage and a repeated run reports `nothing_to_transfer` rather than creating duplicates. The integration reloads once after a transfer so its own references follow the new IDs; ownership and shadow modes are retained across the reload.

`rollback_identity_migration` renames the consolidated entities back to their previous IDs and recreates the legacy registry entries from the snapshots (name, area, labels, icon and device). Rolled-back items may be migrated again. Retain the legacy integrations until the transferred IDs have been checked on dashboards and in history.

## Schedule fallback and commissioning

Missing RAMSES schedules are retried in the background rather than deferring a failed retrieval for a day. Since 0.6.2 the first retry is after five minutes and consecutive failures back off through ten, twenty and forty minutes to a one-hour ceiling; see [radio schedule recovery](radio-schedule-recovery.md). Valid cached schedules have a 48-hour offline limit; cloud/live schedule sources remain preferred. The Utility RF schedule warning still requires checking against live retrieval: retries cannot repair an RF/device failure by themselves. Residual retrieval and gateway reliability work is tracked in [#15](https://github.com/xpenno255/ha-home-heating-optimisation/issues/15).

Before activation, compare cold-weather shadow decisions, exercise heating/DHW transitions, confirm room sensor placement and controller limits, and record a supervised trial with a rollback point. Historical coverage and passing software tests do not demonstrate the house's heating performance. Activate one function at a time; do not change tuning and ownership simultaneously.

## Validation

The consolidated test suite retains the existing observation/advisor tests, pure comfort and boiler regression suites, and 27 boiler release regressions run through HHO. New integration tests cover both actuator calls, shadow isolation, ownership conflicts, restart restoration, rollback, failed storage, failed writes, stale inputs, fresh repeated MQTT values, retained-message rejection, schedule retries and independent survey copying. Live deployment checks are retained privately under `local-data`.
