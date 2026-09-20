# Release notes for #27

- Added optional gateway monitoring: configure RAMSES gateway online entities and an unresponsive threshold (default 10 minutes) under Options > Gateway monitoring; an empty list disables it.
- Raised one repairs issue and one persistent notification per gateway outage, naming the entity, when it was last online and what still works, and cleared both with a recovery notification (with flap count) when the gateway returns.
- Fired `home_heating_optimisation_gateway_unresponsive` and `home_heating_optimisation_gateway_recovered` events for user automations; no third-party notify service is called.
- Added a diagnostic sensor per gateway (`online`, `unresponsive`, `unavailable`, `unconfigured`, outage and flap counts) and a `gateways` section in `get_control_report`.
- Startup and reload observe a grace period of at least two minutes so a broker's transient offline message never raises a false alert; monitoring failures are isolated from control.
