# Home Heating Optimisation

<img src="custom_components/home_heating_optimisation/brand/icon.png" width="96" alt="Home Heating Optimisation">

A Home Assistant integration for observing room temperatures, heating demand and
boiler operation, forming the foundation for coordinated home heating optimisation.

**Version 0.6.x includes consolidated comfort and boiler control**, with an explicit
legacy import and exclusive handover. Imported controllers start in shadow. It
replaces OT Thermostat Control and Boiler Flow Control after handover, while keeping
RAMSES/Evohome and EMS-ESP as the device connections. Observation, history and the
optional Heating Advisor remain available independently.

See [control setup and handover](docs/control-and-handover.md),
[controller configuration](docs/control-configuration.md), and
[commissioning](docs/commissioning.md). The current release is v0.8.1, a commissioning prerelease: software validation is complete, but physical heating performance still requires a supervised trial. New setup and import both start in shadow. See the [changelog](CHANGELOG.md) and the [GitHub backlog index (#12)](https://github.com/xpenno255/ha-home-heating-optimisation/issues/12) for what has shipped and what remains.

## Install

Requires Home Assistant **2026.9.0 or newer**. Local tests run against 2026.9.0.

### Manual installation

Build the ZIP with `python scripts/build_release.py`, or download the build artifact
from a successful GitHub Actions run. Extract its `custom_components` directory
into your Home Assistant configuration directory, then restart Home Assistant.

Alternatively, copy `custom_components/home_heating_optimisation` directly into
Home Assistant's `custom_components` directory.

### HACS custom repository

1. Open **HACS → ⋮ → Custom repositories**.
2. Add `https://github.com/xpenno255/ha-home-heating-optimisation` with type **Integration**.
3. Find **Home Heating Optimisation**, select a published release, then restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and select
   **Home Heating Optimisation**.

This repository supports HACS custom-repository installation; it is not included
in HACS's default catalogue. It includes standard and high-resolution local brand
icons, following the [Home Assistant branding guidance](https://developers.home-assistant.io/docs/core/integration/brand_images/).

HACS installs the component from the selected release tag. The release ZIP is an alternative for manual installation. The 0.6.x, 0.7.x and 0.8.x releases (currently v0.8.1) are published as prereleases; enable prerelease visibility in HACS or the HACS update entity will keep reporting the last stable 0.5.x tag as latest. A prerelease is not a claim of completed physical commissioning.
Keep the existing controllers installed until the control import, shadow comparison
and handover have been verified. The published v0.5.x releases are observation-only.

## Configure

Add **Home Heating Optimisation** in Settings → Devices & services.

1. Select your room climate entities. A single temperature target is required for
   target/deficit readings; thermostats exposing only a target range are not supported.
2. Give each room a name and optionally select independent room-air and heat-demand
   sensors. Defaults are the climate entity's `current_temperature`, `temperature`
   and `heat_demand` attributes. A different air sensor changes observation only.
3. Optionally select outdoor temperature, actual boiler flow/return, the boiler flow
   setpoint readback, space-heating activity and resolved DHW activity.
4. Enable historical analytics if wanted. Optionally map OT and boiler decision
   sensors to provide context for reports.

Use Boiler Flow Control's resolved **DHW Active** binary sensor where available.
The observer does not infer DHW from aggregate demand. Select a space-heating signal
with the intended meaning; flame/burner activity alone can also include hot water.

Configure options through the integration's cogwheel. You can add/remove rooms,
rename them and clear optional mappings. Controlled room removal/rebinding requires deliberate control configuration and ownership review. Existing observation entity IDs are retained
for a room-name change or an optional-sensor change. Configured source entity ID renames are followed through the HA entity registry; see [source identity](docs/source-identity.md). References written only in survey YAML still need manual updates.

## Estimated boiler efficiency

In **Controller configuration > Boiler sources and limits**, optionally select
**British Gas 430/i - natural gas (estimated)** as the efficiency profile. It is
**disabled by default**, including for existing/imported configurations. This adds
an **Estimated boiler efficiency** diagnostic; it never changes boiler control.
Configure measured return temperature, burner modulation (%) and heating-active
inputs whose off/on transitions represent boiler burns.

This is a **gross-basis, full-load reference**, not measured efficiency or an
estimate corrected for actual burner load. Between 30 and 60 C return temperature:

`efficiency_percent = 100 * 0.901 * (31.8 - 1.8 * (return_c - 30) / 30) / 30.9`

The whole-percent display is about 93% at 30 C, 90% at 45 C and 87% at 60 C.
The [430/i manual, page 6](https://www.freeboilermanuals.com/assets/pdf/British-Gas/BG-430i-Jun.pdf)
provides the 50/30 and 80/60 C full-load test points (20 K differential).
Linear interpolation, ignoring actual temperature differential and part-load
behaviour, is an approximation. The 0.901 natural-gas net-to-gross factor is
[indicative, not measured fuel composition](https://files.bregroup.com/bre-co-uk-file-library-copy/filelibrary/SAP/2016/CALCM-02---SAP-2016-SEASONAL-EFFICIENCY-VALUES-FOR-BOILERS--ALL-FUELS----DRAFT8.pdf).

A value needs an observed uninterrupted burn of at least five minutes, firing
confirmation and return/modulation readings reported within five minutes and
since ignition. Five minutes excludes startup; it does not prove steady state.
Off, unavailable/stale inputs and temperatures outside 30-60 C show **unknown**,
with a reason in the sensor's `status` attribute. Restarting mid-burn waits for the
next observed ignition, as does a gap after valid firing evidence, including
during startup. This is not combustion analysis, daily/seasonal efficiency or
verified energy savings. There
is no invented cycling-loss penalty or confidence interval; actual efficiency
can differ by several percentage points. Long-term-statistics averaging is not
enabled because a mean of these estimates is not fuel-weighted efficiency.

## Entities

| Scope | Sensors |
| --- | --- |
| Each room | Air temperature; commanded air target; positive air target deficit; heat demand |
| System | Outdoor temperature; actual flow and return; flow setpoint readback; observed operating state; input availability |
| Activity | Space heating active; DHW active |

Air target deficit is `max(target − air, 0)` when the thermostat is enabled. It is a
temperature difference in kelvin (1 K = 1 °C difference), not an operative-comfort
measurement. A room above target has zero positive deficit; an off room has no value.
Heat demand is a requested fraction, not measured radiator heat output or water flow.

Operating state is heating, hot water, mixed or idle only when both selected activity
signals are known. An unknown signal produces an unknown state rather than idle.

**Input availability** is the percentage of currently valid configured readings.
It is not historical observation coverage. Each room contributes air, target and
demand readings; a missing fallback demand attribute reduces availability. Unselected
system sources are excluded. Their individual sensors remain unknown with quality
`not_configured`.

## Source quality and units

Each source-derived sensor includes `quality`, `source_entity` and
`source_reported_at` attributes. Quality is `ok`, `not_configured`, `missing`,
`unavailable`, `stale` or `invalid`. Bad inputs publish unknown values.

- Temperature sensors must report °C, °F or K. Calculations use °C; HA can display
  temperature entities in the user's preferred units.
- Separate demand sensors use 0–100 with `%`, or a unitless fraction from 0–1.
- Climate `heat_demand` is treated as a fraction from 0–1.
- Nonfinite values, unsupported units and out-of-range demand are rejected.

| Input | Maximum time since HA last received a report |
| --- | --- |
| Room air / demand | 30 minutes |
| Outdoor temperature | 120 minutes |
| Actual flow / return | 10 minutes |
| Activity signals | 5 minutes |
| Commanded targets / flow setpoint | No age expiry while available |

The integration updates on source state changes and checks expiry every 30 seconds.
Identical repeated reports count as fresh. Sources need periodic reporting to remain
known under these limits; HA report time does not prove a new physical measurement.
Climate attributes share the climate entity's report timestamp, so individual
attribute freshness cannot be established independently.

Download diagnostics returns quality/count information without room names, entity
IDs or temperature history. Source provenance remains visible locally on sensors.

## Historical analytics and reports

See [historical analytics](docs/historical-analytics.md) for metrics, freshness,
Recorder backfill, retention and the `record_adjustment` / `get_report` actions.
History is optional and disabled by default. Reports gate window-wide metrics below
80% known-state coverage. Optional metered energy with weather, DHW and configuration
context is described in [metered energy evidence](docs/energy-evidence.md); it never
produces a savings figure when context is insufficient. Unchanged available values are held during clean Recorder
runs; recent-change coverage is exposed separately. This is not proof of fresh
physical sampling. The prior recent-change expiry policy remains selectable. Controller estimates remain separate from measured room air.

## House survey (`house.yaml`)

The integration can read the existing OT Thermostat Control survey: `house.yaml` and
`rooms/*.yaml`. In the system options, set **House survey directory**, then confirm
which survey room belongs to each thermostat. Unique survey climate bindings are
suggested; mappings never change your selected sensors or controller settings.

For the bundled OT survey, the usual HA-relative directory is:

```text
custom_components/ot_thermostat_control/house
```

Use your OT hub's configured survey directory if you already use an override. The
folder must be inside HA's configuration directory. Observation survey reads do not change source files. For long-term storage, a user-owned folder outside
`custom_components` avoids replacement by updates to the integration that bundles
them; control import copies the survey to an independent user-owned directory while retaining the originals.

The **House model status** sensor shows import/mapping status and counts.
`home_heating_optimisation.get_house_model` returns thermal/layout context even when
analytics is disabled. `get_report` includes the same context alongside historical
metrics. After editing survey files, run `reload_house_model` to refresh the model.

See [house survey documentation](docs/house-survey.md) for validation, privacy,
confidence labels and limitations. The optional advisor can use this survey context; control import and handover are described in [control setup](docs/control-and-handover.md).

## Roadmap and AI

See the [implementation plan](docs/implementation-plan.md),
[migration design](docs/migration-plan.md) and [design notes](docs/design-notes.md).
Open commissioning, reliability and roadmap items are tracked in the
[GitHub backlog index (#12)](https://github.com/xpenno255/ha-home-heating-optimisation/issues/12).

The integration includes operative-temperature comfort control, boiler supervision and an
optional Heating Advisor. The advisor selects Home
Assistant AI Task profiles per task. Extended OpenAI Conversation manages local
Gemma; the built-in Anthropic integration manages Claude credentials, model and
supported effort settings. See [Heating Advisor setup and limits](docs/heating-advisor.md) for profile selection, scheduled reviews and report actions. AI calls require explicit enablement. AI cannot activate the controller or issue actuator commands.

## Development

```bash
python3.14 -m venv .venv
.venv/bin/pip install -r requirements-test.txt
.venv/bin/ruff check custom_components tests scripts
.venv/bin/ruff format --check custom_components tests scripts
.venv/bin/pytest -q
.venv/bin/python scripts/build_release.py
```

Tests use an isolated Home Assistant instance. They cover configuration, units,
quality, activity states, source expiry, options, entity identity, unload/reload,
shadow-mode absence of actuator writes, active-controller service calls, command readback, ownership, failure isolation, historical calculations, storage failures, real
Recorder replay, journal actions and package contents. The GitHub workflow also runs
hassfest and HACS validation. The observer and analytics have been verified on the live installation. Advisor provider evaluation is documented separately.

The GitHub workflow runs the full HACS validation with no skipped checks, plus
hassfest and the test suite. Package tests verify the MIT licence and bundled icon.

Brand artwork is in `assets/icon.svg`. To regenerate its PNGs, install the optional
development dependency `CairoSVG==2.8.2` and run `python scripts/build_icon.py`.

Before removing an active consolidated controller, use the documented rollback to
restore the previous owner. Source device integrations remain independent.

Read the [readiness decisions and remaining scope](docs/readiness-decisions-2026-09-17.md) before the supervised trial. Full advisor conversations, experiments, coordinated learning, and legacy history/registry transfer remain later work.
