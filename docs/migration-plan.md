# Migration and control handover design

Status: design only. Version 0.2.0 does not migrate or alter legacy integrations.

## Existing identities and storage

| Integration | Identity | Persistent state |
| --- | --- | --- |
| OT Thermostat Control | Hub and per-room entries; entity unique IDs generally `{entry_id}_{key}` | `.storage/ot_thermostat_control_{entry_id}` and hub variant `ot_thermostat_control_hub_{entry_id}`; survey YAML |
| Boiler Flow Control | One entry; entity unique IDs `{entry_id}_{key}` | `.storage/boiler_flow_control_{entry_id}`; write/filter/manual-hold memory |
| Radiator Analytics | Single entry; zone entity IDs include domain, zone slug and metric key | HA Store key `radiator_analytics`, envelope version 1, payload schema 2; observations, source configuration, adjustment journal, archived legacy sessions |

Changing a domain or unique ID does not itself preserve entity IDs or history.
Treat registry transfer and metric compatibility as explicit migration work.

## House survey ownership

The existing survey is `house.yaml` plus `rooms/*.yaml`; it is currently loaded by
OT Thermostat Control. Home Heating Optimisation 0.2.0 does not read or modify it.
The proposed consolidated comfort model should use one shared, versioned survey,
with user data outside the HACS-managed component directory (for example
`/config/home_heating_optimisation/house/`). Do not bundle a household's survey in a
public release or overwrite it during an integration update.

A future import must validate house/room schemas, preserve unknown fields and
provenance, preview room matches and retain originals for rollback. AI evidence
should contain only relevant selected thermal properties, excluding unrelated
household/network details. Sharing survey data does not itself transfer actuator
ownership. This policy is design guidance; no survey migration is implemented yet.

## Proposed importer

1. Read supported legacy entries and effective options, source mappings, survey
   files and storage without modifying them. Capture checksums and schema versions.
2. Produce a preview: matched rooms, conflicting sensors/settings, selected source
   of truth for each field, unsupported versions and entities eligible for transfer.
   Do not silently choose a conflicting mapping from one integration.
3. Assign durable room IDs and persist an old-to-new mapping. Store a versioned
   import journal so retrying an interrupted import is idempotent.
4. Import into inactive/shadow modules. Never restore actuator ownership or pending
   write acknowledgements as if this new integration had performed the old writes.
5. Copy compatible observations with their original definitions/provenance. Archive
   incompatible aggregates. Preserve adjustment notes locally; exclude them from
   downloaded diagnostics and cloud evidence by default.
6. Validate the copied data before any source entry or store is retired. Retain
   rollback snapshots and the original files until migration is verified.

## Entity and history continuity

- Transfer eligible registry entries using supported HA registry APIs, with explicit
  platform/config-entry/unique-ID mappings and collision checks.
- Only reuse an entity ID when its physical meaning, units and statistics semantics
  are unchanged. Use new IDs for estimated operative comfort and revised metrics.
- Retain archived history for retired metrics; do not relabel it as a new definition.
- Test user-renamed IDs, disabled entities, dashboard references, long-term statistics,
  restart midway through migration and a repeated importer run.
- A source entity rename must preserve its room mapping through the registry identity
  in the migration implementation. The initial observer requires reconfiguration for
  renamed source entity IDs; it does not yet implement automatic source rename tracking.

## Exclusive actuator handover

The future write-enabled release must implement a handover transaction per actuator:

1. List all known legacy writers and relevant automations. Explicitly identify each
   thermostat target and boiler flow-setpoint entity to be owned.
2. Put legacy writers in hold/off and unload their write loops; verify their stopped
   state. Cancel or settle pending calls and overrides before transfer. Undiscovered
   external automations remain a commissioning concern, not a solved inference.
3. Re-read current physical state and limits. Initialise the new controller's memory
   from observed state, not a replay of an old pending write.
4. Activate only the selected new module. Persist ownership and fail closed if
   restoration is incomplete or a conflicting writer is detected.
5. Rollback stops the new writer before restoring the previous owner and settings.
   A crash during handover leaves the new controller inactive until reconciled.

DHW transitions, manual holds and Evohome schedule handback need dedicated replay
and HA tests. Boiler hardware protections continue to own appliance safety.

## Migration acceptance tests required before activation

- Supported and unsupported configuration/storage versions; absent/corrupt stores.
- Options overriding entry data; cleared optional entities; conflicting room mappings.
- Survey matching and physically meaningful unit conversion.
- Same-name rooms, source renames, entity collisions and user-renamed entities.
- Historical definition boundaries and retained adjustment periods.
- Interrupted/repeated imports and safe rollback without duplicate writers.
- Independent module enablement, restart restoration and stale source recovery.

None of these legacy-migration acceptance tests is claimed by the 0.2.0 observer.
