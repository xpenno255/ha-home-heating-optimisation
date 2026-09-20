# RF schedule recovery

Room temperature control uses the configured cloud schedule, falling back to the existing RAMSES schedule/cache when needed. A radio schedule download is optional background work; waiting for it must not hold up a radiator command or boiler calculation.

The hub serializes downloads across rooms. Each room can have only one pending request. Missing, unknown and unavailable thermostat entities are skipped, including a zone that becomes unavailable while waiting for another download. Unloading cancels both queued and running requests. Each running service call is bounded to 45 seconds, and service errors are caught by the downloader.

A missing schedule is retried after five minutes initially. Consecutive unsuccessful downloads use delays of five, ten, twenty, forty and then sixty minutes. A usable cached/live schedule keeps the normal daily refresh cadence. A successful download refreshes cache age even if its content is unchanged. The existing 48-hour limit on offline cached schedules remains in place.

These measures reduce traffic during radio faults; they do not establish gateway connectivity or repair a failed transport. A pool with one working gateway can continue serving both rooms while another gateway is offline. Gateway monitoring (below) reports a gateway whose online entity has gone quiet; packet-receipt diagnostics remain in ramses_cc.

## Gateway monitoring

Added 20 September 2026. Options > Gateway monitoring takes one or more gateway online/availability entities (for example ramses_cc `binary_sensor.<gateway>_online`), an unresponsive threshold (1 to 120 minutes, default 10) and two switches: persistent notifications and bus events. An empty entity list disables the feature; nothing is discovered from device IDs.

A gateway is unresponsive when its entity has been `off`, `unavailable`, `unknown` or missing for at least the threshold. The monitor listens for state changes and also checks every 60 seconds so a gateway that goes quiet without a new event is still caught. After Home Assistant starts or the integration reloads, nothing is reported for the larger of the threshold and two minutes; a broker's transient "offline" last-will message during startup therefore never raises an alert.

Each outage produces exactly one repairs issue (`gateway_unresponsive_<slug>`, severity warning), one persistent notification (`home_heating_optimisation_gateway_<slug>`) and, when events are enabled, one `home_heating_optimisation_gateway_unresponsive` event with `entity_id`, `since`, `duration_minutes` and `flap_count`. The notification names the entity, the last time it was online, how many of the other configured gateways are online and that room and boiler control continue with cached radio schedules. It does not claim that any radio command failed or that a radiator did not move. The monitor never calls third-party notify services; wire your own automation to the events.

Recovery requires the entity to stay `on` for the smaller of the threshold and two minutes, so a flapping stick does not produce a stream of alerts. On recovery the issue is deleted, the outage notification dismissed, a short recovery notification posted (including the flap count in the last hour when it flapped more than once) and `home_heating_optimisation_gateway_recovered` fired.

Each configured gateway has a diagnostic sensor `sensor.home_heating_optimisation_gateway_<slug>` with state `online`, `unresponsive`, `unavailable` (quiet but under the threshold or still in the startup grace) or `unconfigured`, and attributes `entity_id`, `since`, `last_online`, `last_change`, `outage_count` and `flap_count`. `get_control_report` includes the same data under `gateways`. Monitoring failures are logged and shown in `last_error`; they never affect control.

Example automation trigger (synthetic IDs):

```yaml
triggers:
  - trigger: event
    event_type: home_heating_optimisation_gateway_unresponsive
    event_data:
      entity_id: binary_sensor.gateway_a_online
```

Command acknowledgements have different requirements from schedule availability; see [command confirmation](command-confirmation.md). A source entity update or a matching setpoint must not be presented as proof of physical valve movement.
