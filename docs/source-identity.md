# Source entity identity

Home Assistant keeps an entity's registry identity when its entity ID is
renamed, but configuration entries still contain the old ID. HHO listens for
the `entity_registry_updated` event and carries an exact rename through the
configured source fields in each HHO entry.

The update covers the observation mappings (system sources, room climate and
sensor mappings, and the boiler decision sensor), imported control mappings in
the hub, each room, and the boiler, and discovered `mqtt_sources` bindings.
Disabled entries are updated too while the listener is registered in this HA process. Renames that occur before HHO has ever been set up in that process are not replayed.
The update is idempotent: only an actual old-to-new registry rename changes an
entry, and an already-updated entry is left alone.

The rewrite follows explicit entity fields. It changes exact entity ID values
and list members only; names, free text, templates, MQTT topics and fields,
controller seeds, stored history, modes and settings are preserved. A changed source signature may start a new analytics era under the existing history policy; old samples are not rewritten. The imported
`control.observer_before` snapshot is intentionally excluded because it is the
rollback copy of the old observer configuration.

Survey YAML remains read-only. Entity IDs written directly in survey files are
not migrated by this listener; review those files separately if a survey
consumer uses such a reference.

The integration calls `async_register_source_identity(hass)` from its setup path. The helper is process-wide and safely reuses the existing
listener if setup is called again.
