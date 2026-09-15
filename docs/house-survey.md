# Read-only house model (0.3.0)

The house model adds surveyed building context to measured heating observations.
It is not a floor-plan renderer or a heat-loss solver, and does not change comfort
or boiler policy. AI calls are not implemented in this release.

## Configure

In the integration's options, retain your existing room/system mappings and enter
**House survey directory**. Use a relative path under your HA configuration directory,
for example `custom_components/ot_thermostat_control/house`, or an absolute path
inside that directory. The folder must contain `house.yaml` and `rooms/*.yaml`.
Files beginning with `_` are ignored, including `_template.yaml`.

The next forms map each configured thermostat to a survey room. An exact, unique
match to `heating.climate_primary` or `heating.climate_backup` is suggested for
confirmation. Names alone never auto-match; duplicate bindings have no suggestion.
One surveyed room can map to at most one configured thermostat. Rooms without a
thermostat remain in the house model for context, including unheated neighbours.
Leave a thermostat unmapped if its survey is missing. Clear the directory to disable
survey loading; this does not delete files or measured history.

## What is retained

- House front-elevation and face bearings, with confidence and measurement date.
- Room ID/name, floor and heated flag.
- Surveyed dimensions, floor area, volume and explicitly assumed height.
- Boundary faces, gross areas, construction references and adjacent room IDs.
- Openings, dimensions, areas, glazing/shading parameters when specified.
- Radiator dimensions and rated output at ΔT50, when specified.
- Construction U-values in W/(m²·K), with surveyed/estimated/unknown provenance.

Missing measurements remain null. Gross wall area is retained as supplied; do not
sum it with opening area as if it were net wall area. Radiator rated output is not
current heat output. No solar gain, heat-loss coefficient or hydraulic position is
inferred by this loader. Bearings and construction values are not silently filled
from defaults.

Adjacency IDs are resolved against the survey room IDs. Legacy comma-separated
neighbours retain unknown area shares; explicit fractions must sum to one. Names
that do not match IDs are flagged as unresolved rather than silently assigned.
No symmetric wall relationships or physical coordinates are invented.

Address hints, network/device details, raw notes, photo paths, occupancy routines,
weather entity mappings and raw YAML are excluded from exported context. Climate
bindings are used privately for mapping suggestions and omitted from the model
response. The model contains room names and thermal properties, so it still belongs
to the household. No data is sent to any AI or cloud provider by this integration.

## Validation and status

Legacy files without `schema_version` are supported as version 1; explicit versions
other than integer 1 are rejected. Structural errors, duplicate room IDs/YAML keys,
unsafe YAML tags/aliases, invalid confidence labels and nonfinite/negative physical
quantities reject the import. Missing optional data, unknown construction references,
unknown boundary types and unresolved neighbours produce warnings and `partial`
status. Valid surveys show `ready`; absent configuration shows `not_configured`.
A removed mapped survey room shows `mapping_invalid`.

The `House model status` sensor exposes counts and error codes, not the household
model. `get_house_model` provides detailed warning codes and the affected room IDs.
Download diagnostics retain only status/counts. A failed startup or reload sets the
house model to `error` and removes old context from reports, while live observations
and historical analytics continue.

Loading runs in an executor at setup, options reload or `reload_house_model`.
There is no filesystem watcher. The loader accepts at most 128 files, 256 KiB per
file, 4 MiB total, and bounded YAML complexity. Symlinks cannot escape the selected
survey directory, and the directory itself must resolve inside the HA config root.

## Actions and history

```yaml
action: home_heating_optimisation.get_house_model
response_variable: house_model
```

This works with analytics disabled. When analytics is enabled, `get_report` includes
the model under `house_model`. After editing source files:

```yaml
action: home_heating_optimisation.reload_house_model
```

A SHA-256 revision identifies the exact set of source files, alongside load time
and explicit room mappings. Reports label this as **current** building context;
the model is not represented as the house's historical state. Even a comment edit
changes the source revision. Survey source/mapping changes do not reset measured
history or alter its definitions. Historical survey snapshots and applying building
physics to the observations are separate future work.

No copies or writes are made to the survey. OT Thermostat Control can continue
reading the same files. Eventually, a shared user-owned directory outside
`custom_components` is preferable so the integration that currently bundles the
survey cannot replace it during an upgrade. Moving files and controller handover
are not part of this feature.
