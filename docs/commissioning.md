# Supervised heating commissioning

This checklist is the remaining physical validation for the consolidated controller. Deployment alone leaves all controllers in shadow. Keep the original integrations installed and disabled until this trial is accepted.

## Before selecting an active mode

- Save the current control report, current configuration and ownership/rollback state. Confirm each selected room sensor, schedule, room survey mapping and radiator limits. Check boiler CH/DHW bounds and the appliance's actual maximum-flow setting.
- Review `home_heating_optimisation.get_control_report`: ownership must be ready, with no legacy conflicts or room activation blockers. Check that old controller entries remain disabled. Inspect other automatic writers and any externally scheduled calls; static automation inspection cannot discover every external client.
- Check incoming temperature/demand reports are fresh. Compare shadow predictions during real demand and a schedule transition, including the selected separate room sensors and unavailable-sensor fallback. Resolve unexplained differences before activating.
- Identify consumers of the old OT/BFC output entities. Dashboards and automations do not automatically migrate to the new output IDs.
- Configure gateway monitoring (Options > Gateway monitoring) with each RAMSES gateway's online entity, then confirm every `sensor.home_heating_optimisation_gateway_<slug>` reads `online` before the trial. A repairs issue or notification from it during the trial describes the online entity only; check the gateway link and ramses_cc diagnostics rather than assuming a command failed (see [RF schedule recovery](radio-schedule-recovery.md)).

## Trial

1. Record a baseline with actual room air, target, selected boiler flow, actual supply/return, demand, burner activity, outdoor temperature and resolved DHW state.
2. Activate one function at a time without simultaneously retuning it. Start with a supervised limited room-control trial; verify service result, subsequent matching thermostat readback and physical response. A successful service call alone is not confirmation.
3. Verify schedule handback and a manual thermostat override. Check the temporary correction ends correctly and the manual hold remains respected.
4. Trial boiler auto separately. Compare requested/effective/sent/confirmed setpoints and actual supply temperature. Verify the configured and appliance limits hold. Observe a genuine DHW charge and return to space heating, including charge progress/timeout diagnostics.
5. Observe a sustained heating period for recovery, overshoot and cycling. Compare measured room air separately from estimated operative comfort. Recheck after a normal HA restart only while someone can supervise the restored mode.

Stop the trial for unexpected targets, repeated unconfirmed commands, competing writers, stale critical inputs, failure to respect manual control, or poor DHW/room response. Select shadow to stop further consolidated writes. Shadow does not itself undo an already issued temporary thermostat override; check the device/schedule state.

## Rollback

`home_heating_optimisation.rollback_controls` stops consolidated control and restores only the legacy entries disabled by handover. It retains their existing settings; if those were shadow, rollback restores shadow, not automatic heating optimisation. The device system's underlying scheduled heating remains separate. Check actual device modes and targets after rollback.

Record trial time, enabled functions, observations, failures and any chosen tuning changes in the private commissioning record. Do not claim energy savings without metered energy and suitable weather/DHW context.

## Bounded tuning trials

Once a room is active or the boiler is in auto and commissioning is accepted, single-parameter tuning changes can be run as explicitly approved, self-reverting trials; see [Bounded heating trials](bounded-trials.md). A trial never activates control, never changes modes or DHW protection, and rolls back on expiry, on any stop criterion, on restart and on unload.
