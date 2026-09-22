# Control configuration

Home Assistant exposes **Settings → Devices & services → Home Heating Optimisation → Configure → Comfort and boiler control**. A new standalone setup walks through house/shared sources, boiler sources and limits, then every observed room. Existing control setups provide separate menu entries for those three areas.

This release configures the comfort and boiler engines as a pair. Each engine can be disabled independently, as can each room and its occupancy offsets, but both configurations must be complete. Required fields are the control survey and shared outdoor/flow sources, the boiler setpoint actuator and outdoor source, and each room's thermostat and survey room ID. The forms also expose bounded heating/DHW limits and explicit manual-change hold periods.

Observation and control mappings have different jobs. An observation air sensor supplies reports and analytics. A control air sensor is an explicit override for comfort decisions; when omitted, control uses the survey's preferred air source and then its thermostat fallbacks. The control sensor reports the source actually selected. Editing an observation source never silently changes its control counterpart, and both forms show the other selection. Controlled thermostats cannot be removed through observation options, although additional observation-only rooms can be added. The control survey directory must be changed explicitly in control options before its observation mapping is changed.

Standalone setup has no dependency on OT Thermostat Control or Boiler Flow Control. It creates the same schema used by an import, with empty legacy entry IDs and independent controller storage. Imported configurations keep their provenance, source fingerprint, rollback snapshot, learned seeds and live persistent controller state when edited. Selected room-air sensors are retained unless that room's control form explicitly changes them.

Every configuration save leaves the engines in shadow and clears persisted active modes. Editing is refused while a live or persisted room mode is `active`, or the boiler mode is `auto`. Rebinding the boiler setpoint actuator or a room thermostat resets ownership to `unclaimed`; run **Hand over controllers / Claim ownership** again after reviewing shadow output. Source and tuning edits made in shadow keep completed ownership, but do not activate either engine.

Initial setup never grants actuator ownership. For standalone setups, the handover action checks that no enabled legacy controller conflicts exist, journals ownership, and leaves all modes in shadow. For imports it additionally verifies and disables the retained legacy entries. Activation remains a separate, supervised step; use each room's mode select and the boiler mode select only after shadow comparison.

## DHW target schedule

**Configure → DHW target schedule** sets a normal cylinder target plus a higher target for a short local-time window on selected weekdays (default 50°C normal, 60°C higher, 04:00–06:00, no weekdays). It is off by default and independent of the comfort and boiler engines' modes.

Evohome keeps its DHW schedule and decides when the cylinder heats. HHO only changes the cylinder setpoint through `ramses_cc.set_dhw_params`, always resending the current overrun and differential. It never uses DHW mode, boost, reset or schedule services and never forces a charge.

Fields: the RAMSES water heater, a physical DHW-demand source (relay or demand sensor, not the water heater's on/off state), a measured cylinder-temperature source, the two targets (35–85°C in 0.5°C steps, higher above normal), weekdays and the window (start before end, same day). Enabling requires the water heater to report its current parameters. With no weekdays selected nothing is raised.

Enabling, or changing the normal target, permits one write of the normal target if the controller differs. Other saves and restarts do not repeat it. When enabled, the boiler engine reads its cylinder target from the selected water heater and marks it unconfirmed while a change is pending.

How a session runs:

- At window start on a selected day, if handover is complete, no automation or script writes that water heater, no manual DHW override is active and the target is the normal value, HHO records the date and its intent, then raises the target. Starting HA mid-window skips that day. Each date is used at most once, including across DST changes.
- It restores the normal target early once demand was seen, the measured temperature reached the higher target and demand has stayed off for 10 continuous minutes, or when Evohome's schedule turns DHW off after charging. Otherwise it restores at window end. Outcomes are `complete`, `incomplete`, `no_charge`, `target_not_reached`, `insufficient_evidence` or `interrupted`.
- A target changed by anyone else is left alone: the schedule pauses (`paused`) until it is disabled and re-enabled or the normal target changes.
- Unload, reload or restart restores an owned raised target first and never re-raises that session. Recovery still runs if the feature was disabled or removed.

`sensor.home_heating_optimisation_dhw_schedule` shows the state (`disabled`, `armed`, `elevated`, `restoring`, `recovery_pending`, `paused`, `storage_read_only`, `misconfigured`), targets, next window, charge evidence and last outcome; the control report includes the same under `dhw_schedule`.

Limits: HA shows the controller's reply about a minute after a change, and a successful service call is not RF confirmation. RAMSES offers no compare-and-set, so a same-value manual change or a simultaneous external write cannot always be detected. Nothing can restore the target while HA or the RF link is down. Unconfirmed restores are retried a few times, then raise a repair and retry slowly. This is target scheduling only; it does not guarantee thermal disinfection or legionella protection. UK HSE guidance is to store hot water at 60°C or above.
