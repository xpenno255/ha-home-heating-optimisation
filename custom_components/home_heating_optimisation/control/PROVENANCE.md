# Controller provenance

These modules were consolidated from this household's existing projects on 17 September 2026:

- OT Thermostat Control 2.1.2 (`d248d75` baseline): comfort/core, coordinator and hub.
- Boiler Flow Control 0.3.0 (`95bdcd2` baseline): boiler/core, coordinator and hub.

The pure model/policy regression suites are retained under tests/control. The coordinators use the parent HHO config entry, explicit imported configuration, shared source reads, separate persistent state and a final write-ownership gate. No runtime import of either legacy integration is required. Both source repositories belong to the same owner (`xpenno255`), and their recorded commits are authored by the owner. The owner explicitly authorised publishing the consolidated changes to this repository. The consolidated distribution uses this repository's MIT licence; retain these source/revision references. Household survey files and runtime configuration are not part of the distribution.
