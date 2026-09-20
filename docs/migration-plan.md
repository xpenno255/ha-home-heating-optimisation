# Migration and control handover design

Status (as of v0.6.4): this is the original design document, kept for its dated decisions. Read it alongside [implemented import and handover](control-and-handover.md), which describes current behaviour.

Implemented (0.6.1 onward): configuration/survey import, exclusive actuator handover with rollback, restart ownership reconciliation, manual-hold protection, command lifecycle tracking and following configured source references through registry renames.

Implemented after v0.6.4 ([#23](https://github.com/xpenno255/ha-home-heating-optimisation/issues/23)): previewed, reversible transfer of compatible legacy output entity identities with their history and statistics.

Deferred: import of Radiator Analytics history and adjustment notes, and a general-purpose importer, tracked in [#24](https://github.com/xpenno255/ha-home-heating-optimisation/issues/24). Sections below that describe them as future work still are.

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
OT Thermostat Control. Home Heating Optimisation 0.3.0 reads it optionally and never modifies it.
The proposed consolidated comfort model should use one shared, versioned survey,
with user data outside the HACS-managed component directory (for example
`/config/home_heating_optimisation/house/`). Do not bundle a household's survey in a
public release or overwrite it during an integration update.

A future import must validate house/room schemas, preserve unknown fields and
provenance, preview room matches and retain originals for rollback. AI evidence
should contain only relevant selected thermal properties, excluding unrelated
household/network details. Sharing survey data does not itself transfer actuator
ownership. Read-only survey loading and explicit mappings are implemented in 0.3.0. Control import now copies the survey and the handover service transfers controller ownership; observer-only setup remains read-only.

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

Current status (2026-09-20, [#23](https://github.com/xpenno255/ha-home-heating-optimisation/issues/23)): implemented as an explicit, previewed, reversible identity migration run after handover; see [identity migration](control-and-handover.md#identity-migration). Compatible legacy output entity IDs are transferred by renaming the consolidated entity onto the legacy ID; history and long-term statistics follow the entity ID in the recorder. Estimated or revised metrics are archived under their legacy IDs. Radiator Analytics history import remains deferred ([#24](https://github.com/xpenno255/ha-home-heating-optimisation/issues/24)).

- Transfer eligible registry entries using supported HA registry APIs, with explicit
  platform/config-entry/unique-ID mappings and collision checks.
- Only reuse an entity ID when its physical meaning, units and statistics semantics
  are unchanged. Use new IDs for estimated operative comfort and revised metrics.
- Retain archived history for retired metrics; do not relabel it as a new definition.
- Test user-renamed IDs, disabled entities, dashboard references, long-term statistics,
  restart midway through migration and a repeated importer run.
- A source entity rename must preserve its room mapping through the registry identity
  in the migration implementation. Version 0.6.1 follows configured source references through registry rename events. Survey-file-only references and legacy output registry/history transfer are outside that listener.

## Exclusive actuator handover

Current status: implemented in 0.6.1 as `home_heating_optimisation.handover_controls` / `rollback_controls`, with restart reconciliation added in 0.6.3. The transaction below is the original specification and remains the acceptance reference.

The write-enabled release must implement a handover transaction per actuator:

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
