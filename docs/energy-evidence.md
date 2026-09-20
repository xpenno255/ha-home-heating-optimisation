# Metered energy evidence (issue #25, 20 September 2026)

Metered energy is the evidence foundation that any later savings evaluation must
stand on. This page describes what the integration records, what it refuses to
infer, and how comparability is judged. Nothing here changes control settings,
and no savings, efficiency or delivered-heat figure is produced from demand,
modulation or room metrics.

## Configure meters

Open the integration options and choose **Metered energy evidence**. Select up to
three cumulative `sensor` entities with the `energy` or `gas` device class. For each
meter choose what it measures:

- `fuel_input`: boiler gas or electricity consumed. This is measured input energy.
- `delivered_heat`: a separate heat meter on the primary circuit. Delivered heat is
  only ever taken from such a meter; it is never derived from fuel input.
- `electricity`: pumps, controls or a heat pump.

Units are read from the sensor (`kWh`, `Wh`, `MWh`, `m³`) and may be overridden.
Gas volume is converted with the configured calorific value (default 39.5 MJ/m³)
and volume correction factor (default 1.02264); state these when quoting any
figure. Leaving every meter empty disables the feature: no store file and no
entities are created.

## What is recorded

Readings are sampled on the same five-minute UTC grid as historical analytics.
Each bucket stores, per meter, the counter delta as kWh and a quality:

| quality | meaning |
| --- | --- |
| `ok` | consecutive readings within the cadence |
| `gap` | delta kept, but the interval was longer than 7.5 minutes; excluded from coverage |
| `rollover` | the counter wrapped at a decade boundary; the delta is recovered arithmetically |
| `reset` | the counter went backwards without a plausible wrap; energy lost, none invented |
| `unit_unknown` | the sensor unit is not understood; no kWh |
| `missing` | no usable previous or current reading |

A change of source entity or unit is a `reset` flagged `source_changed`. Any period
containing a source change is not comparable.

Each bucket also carries context: time-weighted space-heating share, DHW share,
mean outdoor temperature, the configuration era, and an intervention flag. The era
is a short hash of the actuator bindings when control is configured, otherwise
`observation`. The intervention flag is set when the journal (issue #17) holds a
`command_sent`, `mode_change`, `manual_override`, `adjustment_note` or `trial`
event in the bucket; without a journal it stays false.

When both heating and DHW were active in a bucket, `allocation` is `unknown`. The
integration never splits fuel between heating and hot water.

Storage is `.storage/home_heating_optimisation.<entry_id>.energy`, compressed and
bounded to 90 days of buckets. A file that fails validation is preserved and the
feature runs read-only in memory. Save failures never block heating control.
Changing the meter set starts a fresh record.

## Entities and actions

- `sensor.home_heating_optimisation_energy_evidence_status`: `ready`, `no_meter`,
  `storage_read_only` or `insufficient_data`, with `meter_count`,
  `coverage_percent_7d`, `last_bucket_at`, `reset_count` and
  `allocation_unknown_share`.
- `sensor.home_heating_optimisation_energy_<meter>_today`: kWh since local midnight
  from `ok`/`rollover` deltas. Reset intervals are excluded, not estimated.
- Action `home_heating_optimisation.get_energy_report` (`days` 1 to 90) returns
  per-day kWh, coverage, heating degree hours (base 15.5 °C), DHW share, eras,
  intervention days and a comparability judgement of the recent half against the
  previous half.

Diagnostics show counts and quality only, never entity IDs or readings.

## Comparability

Two periods are compared only when none of these hard limits applies:

- either period has under 80% meter coverage or under 80% weather context;
- either period has no heating degree hours or spans more than one configuration era;
- the eras differ between periods;
- DHW share is unknown or differs by more than 15 percentage points;
- the daily degree-hour ranges do not overlap;
- a meter source changed.

Intervention days and a mostly unknown allocation are soft limits: the comparison
is still made but confidence is `low`. Otherwise confidence is `medium` when both
periods have at least three days and 95% coverage. When comparable, the report
gives `kwh_per_degree_hour` per period, described as an association, not causal
evidence. When any hard limit applies the conclusion is `insufficient`, no ratio
is produced, and the limitation "No savings conclusion: metering/context
insufficient" is stated. A change in this ratio coinciding with a configuration
change is not evidence that the change caused it.

## Advisor evidence

`build_evidence` adds one allowlisted `energy` fact: status, meter kinds,
seven-day coverage, reset count, unknown-allocation share, comparability limits,
conclusion, confidence and the kWh per degree hour when comparable. Raw readings,
daily totals and meter entity IDs are not sent. The prompt instructs the model that
this is input energy with limits, never a savings figure.

## Kept distinct

Measured input energy (`fuel_input`), measured delivered heat (`delivered_heat`
meter only), the estimated boiler-efficiency diagnostic, room demand and inferred
room heat delivery are separate quantities and are never combined into one number.
