# Home Heating Optimisation

A Home Assistant integration for observing room temperatures, heating demand and
boiler operation, forming the foundation for coordinated home heating optimisation.

**Version 0.1.0 is observation-only.** It publishes sensors and diagnostics. Existing
OT Thermostat Control, Boiler Flow Control and Radiator Analytics continue operating.
Control migration, historical analytics and AI reports are planned milestones.

## Install

Requires Home Assistant **2026.9.0 or newer**. Local tests run against 2026.9.0.

### Manual installation

Build the ZIP with `python scripts/build_release.py`, or download the build artifact
from a successful GitHub Actions run. Extract its `custom_components` directory
into your Home Assistant configuration directory, then restart Home Assistant.

Alternatively, copy `custom_components/home_heating_optimisation` directly into
Home Assistant's `custom_components` directory.

### HACS custom repository

Add `https://github.com/xpenno255/ha-home-heating-optimisation` as an Integration
custom repository in HACS. This is a development version, not a claim of inclusion
in the default HACS catalogue or a published stable release.

## Configure

Add **Home Heating Optimisation** in Settings → Devices & services.

1. Select your room climate entities. A single temperature target is required for
   target/deficit readings; thermostats exposing only a target range are not supported.
2. Give each room a name and optionally select independent room-air and heat-demand
   sensors. Defaults are the climate entity's `current_temperature`, `temperature`
   and `heat_demand` attributes. A different air sensor changes observation only.
3. Optionally select outdoor temperature, actual boiler flow/return, the boiler flow
   setpoint readback, space-heating activity and resolved DHW activity.

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

## Roadmap and AI

See the [implementation plan](docs/implementation-plan.md),
[migration design](docs/migration-plan.md) and [design notes](docs/design-notes.md).

Planned modules: operative-temperature comfort control, boiler supervision, historical
heating analytics and an optional Heating Advisor. The advisor will select Home
Assistant AI Task profiles per task. Extended OpenAI Conversation manages local
Gemma; the built-in Anthropic integration manages Claude credentials, model and
supported effort settings. Version 0.1.0 makes no AI calls.

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
absence of device-service calls and package contents. The GitHub workflow also runs
hassfest and HACS validation. No live heating deployment has been performed.

Remove the integration through Settings → Devices & services. It owns only its
observation entities; source controls and the three existing integrations remain
independent.
