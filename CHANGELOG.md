# Changelog

Remaining commissioning and roadmap work is tracked in the [GitHub backlog index (#12)](https://github.com/xpenno255/ha-home-heating-optimisation/issues/12). Every 0.6.x release is a commissioning prerelease: software validation is complete, physical commissioning is not.

## 0.11.4 - rolling boiler efficiency

- The Estimated boiler efficiency sensor keeps its realtime behaviour (unknown when the burner is off) and gains `last_burn_efficiency` and `last_burn_ended_at`, so something useful shows between burns.
- Two new diagnostics, Estimated boiler efficiency (24 hours) and (7 days): the mean of the estimate weighted by burner modulation x time, with heating-only and hot-water-only averages and the firing minutes as attributes. Unknown when the window has no valid firing.
- Samples go into 5-minute buckets (7 days) in the boiler control store, so history survives a restart. Same limits as the estimate: a manufacturer full-load reference from return temperature, not metered. The control report's boiler block carries the new figures.

## 0.11.3 - warm-up boost backs off when a room overshoots

- The boost uplift is now trimmed by the most overheated room: no trim up to 0.3 K above that room's commanded air target, fading linearly to none at 0.8 K. The trim shrinks the target uplift, so the uplift still falls at the normal decay rate. It applies in `shadow` and `active`.
- New `boost_overshoot_k`, `boost_overshoot_room` and `boost_trim` on the boiler control entities, in `warmup_boost` journal events and in the control report (with the peak overshoot today).
- Why: on 17 Oct the boost held +15 K from 06:40 to about 07:15 because the big Kitchen and Living Room radiators kept house demand at 0.5, while Bedroom (+1.7 K), Study (+1.1), Hall (+0.7) and Spare (+0.7) overshot.

## 0.11.2 - a replayed thermostat value after a restart no longer looks like a hand change

- After a restart, a room's first thermostat target that contradicts what HHO last wrote, any standing manual hold and the schedule is no longer acted on at once. The same value has to be reported again at least 5 minutes later before it counts as a hand change. Values that agree reconcile straight away, as before, and the room keeps waiting (no writes) in the meantime.
- Why: on 16 Oct, after the restart onto ramses_cc 0.61.0, RAMSES re-emitted old cached targets (Study 17.4, Utility 16.3, Bedroom 18.4, Studio 18.3) with fresh report times. The real values, HHO's own earlier writes, arrived 18 seconds later. Four rooms went into false manual holds and HHO dropped its record of its own writes.

## 0.11.1 - saving the observation mapping keeps the other settings

- Saving **Configure → Mapping** rebuilt the settings from a fixed list of sections and dropped the DHW target schedule, DHW cutoff, optimum predictor and warm-up flow boost sections, which then fell back to off. All sections owned by other options steps are now kept.
- Why: on 16 Oct changing the return-temperature source through the mapping turned all four features off without warning.

## 0.11.0 - warm-up flow boost from whole-house demand

Optional, off by default. Set **Configure → Warm-up flow boost** to `shadow` to try it without changing anything.

- While several rooms call for heat at once, the boiler flow target can rise above the weather curve towards the maximum flow. The size follows the share of the house's radiator capacity being asked for (each room's `heat_demand` weighted by its installed output): nothing below 15 %, full at 40 % (both settable), rising at once and falling no faster than 1 K per minute.
- Modes: `off`, `shadow` (calculates and records `house_demand`, `room_equivalents`, `warmup_boost` and `boost_flow` on the boiler control entities and in the control report, but the written target is unchanged) and `active` (adds the uplift, capped at the maximum flow, and backs off as the filtered return nears its ceiling). Only in heating mode, never while hot water charges.
- Journalled as `warmup_boost` (`started`, `ended`; `updated` at debug level), with a `warmup_boost` summary in `get_control_report` (minutes in boost today, peak demand share and uplift).
- Why: on 14 Oct the Kitchen, Living Room and Hall took 50–75 minutes to arrive while 181–243 % of a room's demand was calling and the flow target stayed at the 50 °C floor; the existing demand correction only reacts to one zone above 70 % for 20 minutes. Return peaked at 35.5 °C, so there is condensing headroom.
- Saved on its own, so changing it never resets the controllers to shadow.

## 0.10.4 - optimum predictor: keep scoring a warm-up until the room arrives

- A warm-up record was closed 30 minutes after the switchpoint whenever the zone setpoint differed from the schedule. HHO's own commanded target sits above the schedule (14 Oct: 19.5 for a 19.0 step), so every morning record closed before the room arrived and scored no arrival, lateness or model error. Records now close early only when the setpoint falls 0.3 K below the warm-up's target; otherwise 120 minutes after the switchpoint.

## 0.10.3 - optimum predictor: catch stepped warm-ups, steadier dead time

- A warm-up now starts on a target rise of 0.5 K or more within 15 minutes, with the room at least 0.1 K below the arrival line; a further rise in that window raises the goal. On 13 Oct the schedule's 19.0 and HHO's 19.3 a minute later were two small steps, and none of that morning's warm-ups were learned.
- Arrival is judged against the lower of the goal and the current target, so HHO trimming its target mid warm-up no longer turns a real arrival into a miss (12 Oct Kitchen). Warm-ups left with under 0.4 K to climb are dropped.
- The dead time is measured only on climbs of 0.8 K or more. With 0.1 K sensor steps the first rise on a small climb is often the arrival itself (13 Oct Kitchen), which made the delay swallow the whole warm-up. Pace now uses the room's dead time rather than each event's own.
- Scores recorded before 0.10.2 are left out of the rolling figures.

## 0.10.2 - optimum predictor: aim at HHO's commanded target, learn the response delay, fix scoring (commissioning prerelease)

- The goal is now HHO's commanded target (next switchpoint plus the room's model offset, limited to -1 to +2 K), not the bare schedule. Records, journal events and room attributes carry both `schedule_target` and `goal`.
- Warm-up events record a `delay_minutes` (event start to the first reading 0.1 K above the start temperature). Pace is measured after that dead time, and a prediction is `delay + pace x climb`. Stored events without a delay count none.
- Scoring fixed. A room already at its goal when the switchpoint arrives is `not_needed` and left out of the rolling scores. Evohome's start is the switchpoint unless the zone setpoint was raised before it (`evohome_early`); arrival is only searched from that start. Model error compares predicted with actual heating minutes; a lead-0 prediction is judged from when it was made. Boost lateness scales only the pace part.
- Why: on 13 Oct the Kitchen and Living Room stepped 18 to 19 °C at 06:30 while HHO commanded 19.3 / 19.4. The predictor aimed at 19.0, said lead 0, and the rooms took 78 and 85 minutes (about 45 of them dead time) to arrive. Scoring then reported a "reached" time before the switchpoint.

## 0.10.1 - optimum predictor: learn after history loads, show the next warm-up (commissioning prerelease)

- Learning waits until the analytics history has finished loading after a restart. On 12 Oct the first pass ran 20 seconds before the history loaded, so every room showed no events and no prediction for 10 minutes. The status report shows `waiting` (`history loading` / `no history yet`) meanwhile.
- A room's optimum start sensor stays `unknown` until its next warm-up is within the maximum lead (3 h by default). It now also shows `next_warmup`, `next_warmup_from`, `next_warmup_to` and `predicting_from`, found from the RAMSES weekly schedule when the next switchpoint is a step down.

## 0.10.0 - shadow optimum start / stop predictor (commissioning prerelease)

Optional, off by default. Nothing changes until it is enabled in **Configure → Optimum start predictor (shadow)**. Shadow only: it predicts, journals and scores, and never writes to any device (the only service it calls is the read-only `weather.get_forecasts`).

- Learns each room's warm-up pace from what happened: every time the target rose by 0.5 K or more with the room at least 0.5 K short, how many minutes the room took to arrive (events are kept, newest 60 per room, so learning outlasts the 15-day analytics window). The start lead is the recency-weighted median minutes per kelvin times the climb still needed, plus the margin; an outdoor term is used only with 8 or more events and only if it beats the plain median on leave-one-out error. The evening coast rate from demand-off cool-downs gives the stop estimate, and only when the house is actually cooling.
- Why not a physics model: a thermal fit was not identifiable on the live house in mild weather, with 0.1–0.3 K target nudges and a pulsing burner.
- Scores every start against what Evohome's Optimum Start actually did: when Evohome started, when the room arrived, the model's warm-up error, an estimate of how late HHO's own start would have been, and a what-if of a hotter flow (default 65 °C, from the room's usual supply temperature) to show whether boosting would have fixed a late morning. The what-if is never applied.
- Optional hourly weather forecast for the outdoor temperature; falls back to the current outdoor reading, and each prediction records which it used.
- `sensor.home_heating_optimisation_optimum_start` (disabled / needs_analytics / learning / ready, rolling scores), per-room optimum start and stop timestamp sensors, an `optimum` block in `get_control_report`, and `optimum` journal events (`start_predicted`, `start_scored`, `stop_predicted`). The journal kind list in the `get_journal` service now also offers `dhw_schedule` and `dhw_cutoff`.
- Why: on 12 Oct Evohome's Optimum Stop silently dropped the Spare Room to 18 °C and was turned off, and morning warm-ups are slow (Kitchen and Living Room 0.4–1.5 K under target until 08:00–09:00). HHO's own predictor is built in shadow first so it can be scored against Evohome's Optimum Start before anything is activated.

## 0.9.2 - DHW target schedule ignores a stale inactive mode (commissioning prerelease)

- The DHW target schedule no longer ends a higher-target session as `incomplete` while a charge is running. A water heater mode that reads inactive during a charge is treated as stale; schedule-off is judged once demand has stopped.
- Why: on 10 Oct Evohome started the 04:40 charge while the RAMSES water heater still showed the previous day's inactive mode. The session was restored to 50 °C in the same second, so the cutoff stopped the charge at 51.8 °C instead of 60 °C.

## 0.9.1 - DHW cutoff leaves boosts alone after a restart (commissioning prerelease)

- New optional **Evohome cloud water heater** field in **Configure → DHW measured-temperature cutoff** (suggested automatically when exactly one is loaded). While it shows any mode other than `FollowSchedule`, or the schedule has DHW off, the cutoff stays in standby.
- Why: on 4 Oct HA restarted in the middle of a user boost. Afterwards the RAMSES water heater showed `follow_schedule` while the controller stayed in its permanent override, so the cutoff treated the boost as a scheduled charge and held DHW off at 52 °C. The cloud entity kept the real mode throughout.
- If the cloud entity is missing or unavailable, the cutoff falls back to the RAMSES mode check as before.
- Replayed on 26 Sep – 4 Oct history: all 9 scheduled morning charges still stop at 51.2–52.6 °C; all 7 boosts are left alone.

## 0.9.0 - DHW measured-temperature cutoff (commissioning prerelease)

Optional, off by default. Nothing changes until it is enabled in **Configure → DHW measured-temperature cutoff**.

- Stops a stored hot water charge when HA's own cylinder reading reaches the Evohome target plus a margin (default 1 °C). On the live house the controller misses 5–30 % of the cylinder sensor's reports. When it missed the ones just above 50 °C it kept charging until the sensor's next report, about an hour later, and the cylinder reached 61 °C (1–4 Oct). HA's gateways heard 155 of 156 of the same reports.
- Uses `ramses_cc.set_dhw_mode` only: a 60-minute temporary override with DHW off. The controller ends it by itself, so HA stopping can never leave DHW off for longer. While the cylinder is still above the controller's reheat point (target minus differential) the hold is renewed, up to 4 hours; once HA measures it below that point, DHW is handed back to the schedule.
- Acts only while the water heater follows its schedule, a charge is running (demand entity) and the reading is under 10 minutes old. Boosts and manual overrides are left alone. An unconfirmed stop is sent once more, then dropped until that charge ends. The same automation/script guard as the DHW target schedule applies.
- On a higher-target day from the DHW target schedule the cutoff follows that target.
- `sensor.home_heating_optimisation_dhw_cutoff` (watching / holding / unconfirmed / standby) and a `dhw_cutoff` block in `get_control_report`; every stop, renewal and release is journalled as `dhw_cutoff`.
- Replayed against 26 Sep – 4 Oct radio logs: every charge would have stopped at 51.2–52.6 °C, including the four that reached 61 °C.

## 0.8.3 - journal detail level, quiet-sensor freshness, switchpoint timing (commissioning prerelease)

- **Journal detail level.** New **Configure → Journal detail** option. *Standard* (default) no longer records the boiler's unchanged once-a-minute re-assertion or repeats of a room write the guard keeps blocking; target changes, failures, readbacks and manual holds are still recorded. *Debug* keeps everything. On the live house the boiler re-assertion was about 4,300 of 5,600 events a day, so the 20,000-event cap held only about three days; standard keeps about two weeks.
- **Quiet room sensors.** A room air sensor counts as fresh when it, or any other entity on the same device (humidity, battery), reported in the last 30 minutes. Battery sensors that only report on change were blocking writes for hours on steady days (live: Hall, Bedroom and Spare Room blocked roughly half the time). An unavailable sibling is not evidence.
- **Switchpoint grace.** The schedule-change time is now stamped with the control cycle's own clock. It was read a few microseconds later than the cycle time, so in the cycle that first saw a switchpoint the "zone still on the previous value" grace window was negative and every room reaching a switchpoint before its thermostat reported the new value was flagged as set by hand for up to 5 minutes.
- **Version.** The runtime version (device info, journal provenance) now matches the manifest; it had stayed at 0.8.1 in 0.8.2.

## 0.8.2 - only external openings affect room control (commissioning prerelease)

- Internal door contacts no longer change room control. Previously any contact under `sensors.contacts` that was not an opening's `contact_sensor` was treated as an adjacent door and made the room write its plain schedule target (`door_open`), so an open kitchen-to-hall door stopped the kitchen optimising.
- Only contacts attached to external openings (windows and outdoor doors, via `openings.items[].contact_sensor`) count. They drive the existing window override: after the open delay the room drops to the window setpoint (default 10°C) and returns after the close delay.
- The `door_open` room state and the `adjacent_door_open` diagnostic are removed.

## 0.8.1 - DHW schedule allows on/off automations (commissioning prerelease)

- Automations and scripts that only switch DHW on, off or to boost (`water_heater.set_operation_mode`, `ramses_cc.set_dhw_mode`/`reset_dhw_mode`/`set_dhw_boost`, on/off device actions) no longer block the DHW target schedule. Only target writers (`ramses_cc.set_dhw_params`/`reset_dhw_params`, `water_heater.set_temperature`, temperature device actions) block it.
- A boost active at window start still delays the raise until it ends, within the window.

## 0.8.0 - DHW target schedule (commissioning prerelease)

Optional, off by default. Nothing changes until it is enabled in **Configure → DHW target schedule**.

- Normal cylinder target (default 50°C) plus a higher target (default 60°C) in a local-time window (default 04:00–06:00) on selected weekdays (none by default). Evohome keeps its DHW schedule and on/off control; HHO only changes the setpoint via `ramses_cc.set_dhw_params`, always resending the observed overrun and differential. No mode, boost, reset or schedule calls.
- The raise happens once per date at window start, only with handover complete, no competing automation/script writer, no manual DHW override and the normal target in place. Intent is saved before any write; a save failure means no write. Starting mid-window skips that day.
- Normal is restored after observed charging, the measured cylinder temperature reaching the higher target and 10 minutes of continuous off demand, or when Evohome's schedule turns DHW off, or at window end. Outcomes: `complete`, `incomplete`, `no_charge`, `target_not_reached`, `insufficient_evidence`, `interrupted`.
- Any external target change pauses the schedule rather than being overwritten. Unload, reload and restart restore an owned raised target and never re-raise; recovery runs even if the feature was disabled or removed. Unconfirmed restores retry, then raise a `dhw_schedule_restore` repair.
- `sensor.home_heating_optimisation_dhw_schedule` and a `dhw_schedule` block in `get_control_report`. When enabled, the boiler engine reads the cylinder target from the selected water heater and flags it unconfirmed while a change is pending.
- Target scheduling only: not guaranteed thermal disinfection or legionella protection. See `docs/control-configuration.md`.

## 0.7.1 - bounded actuator calls (commissioning prerelease)

Patch for a live incident class: a wedged `ramses_cc` service pipeline left blocking `set_zone_mode` calls awaiting forever, so active room coordinators sat inside their control cycle for hours and the mode-selection service hung behind their refresh. No control decision or confirmation rule changes.

- Every actuator service call (`ramses_cc.set_zone_mode`, `number.set_value`) is bounded at 30 s. A call that does not return is treated as a failed write: `write_status` and the journal `command_result` outcome are `service_timeout`, memory is not advanced, and the existing bounded retry applies on the next cycle.
- Coordinators are self-diagnosing: a control cycle exceeding 120 s is abandoned with an error log and a journal `decision` with `outcome: cycle_timeout`; the last good data is returned with the note `cycle timed out; showing last good data` so scheduling continues and the cycle lock is always released.
- `set_mode` and the startup refresh persist and apply the mode first and wait at most 30 s for the coordinator refresh; a wedged refresh logs a warning and the service still returns.

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
