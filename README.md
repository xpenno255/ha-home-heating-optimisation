# Home Heating Optimisation

<img src="custom_components/home_heating_optimisation/brand/icon.png" width="96" alt="Home Heating Optimisation">

A Home Assistant integration for observing room temperatures, heating demand and
boiler operation, forming the foundation for coordinated home heating optimisation.

**Version 0.2.0 is observation-only, with optional historical analytics.** It publishes
sensors, structured reports and a private adjustment journal. Existing
OT Thermostat Control, Boiler Flow Control and Radiator Analytics continue operating.
Control migration and AI-generated reports remain future milestones.

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
3. Find **Home Heating Optimisation**, download release **v0.2.0**, then restart Home Assistant.
4. Go to **Settings → Devices & services → Add integration** and select
   **Home Heating Optimisation**.

This repository supports HACS custom-repository installation; it is not included
in HACS's default catalogue. It includes standard and high-resolution local brand
icons, following the [Home Assistant branding guidance](https://developers.home-assistant.io/docs/core/integration/brand_images/).

HACS installs the component from the selected release tag. The attached
`home_heating_optimisation-0.2.0.zip` is an alternative for manual installation.
Keep the existing heating integrations enabled: this release observes them and
makes no thermostat or boiler commands.

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
rename them and clear optional mappings. Existing observation entity IDs are retained
for a room-name change or an optional-sensor change. Source entity ID renames require
reconfiguration; automatic source rename tracking is a later enhancement.

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
80% coverage. Controller estimates remain separate from measured room air.

## House survey (`house.yaml`)

The existing OT Thermostat Control survey consists of `house.yaml` plus
`rooms/*.yaml`. Version 0.2.0 of this integration does not load, migrate or modify
those files. It can observe OT's published targets, estimates and decisions through
optional sensor mappings.

The planned consolidation will use a shared house survey for the comfort model
and relevant report context. The intended location is a user-owned directory
outside `custom_components`, for example `/config/home_heating_optimisation/house/`,
so integration updates cannot replace household data. Survey import, schema
validation and control handover are future work; existing files remain in use by
OT Thermostat Control until that migration is implemented and verified.

## Roadmap and AI

See the [implementation plan](docs/implementation-plan.md),
[migration design](docs/migration-plan.md) and [design notes](docs/design-notes.md).

Planned modules: operative-temperature comfort control, boiler supervision and an
optional Heating Advisor. The advisor will select Home
Assistant AI Task profiles per task. Extended OpenAI Conversation manages local
Gemma; the built-in Anthropic integration manages Claude credentials, model and
supported effort settings. Version 0.2.0 makes no AI calls.

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
absence of device-service calls, historical calculations, storage failures, real
Recorder replay, journal actions and package contents. The GitHub workflow also runs
hassfest and HACS validation. No live heating deployment has been performed.

The GitHub workflow runs the full HACS validation with no skipped checks, plus
hassfest and the test suite. Package tests verify the MIT licence and bundled icon.

Brand artwork is in `assets/icon.svg`. To regenerate its PNGs, install the optional
development dependency `CairoSVG==2.8.2` and run `python scripts/build_icon.py`.

Remove the integration through Settings → Devices & services. It owns only its
observation entities and private history/journal storage; source controls and the three existing integrations remain
independent.
