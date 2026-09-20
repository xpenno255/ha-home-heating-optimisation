# RF schedule recovery

Room temperature control uses the configured cloud schedule, falling back to the existing RAMSES schedule/cache when needed. A radio schedule download is optional background work; waiting for it must not hold up a radiator command or boiler calculation.

## Precedence

1. Cloud (evohome) entity `status.setpoints.this_sp_temp`, with the next switchpoint applied when the cloud lags. An unavailable cloud entity is ignored even though it retains stale attributes.
2. Live RAMSES `schedule` attribute on the primary thermostat entity. A live schedule is copied to the room store and stamps `ramses_schedule_saved_at`.
3. Cached RAMSES schedule from the store, only while `ramses_schedule_saved_at` is at most 48 hours old. A failed or skipped download never renews that stamp; a successful download of an unchanged schedule does.
4. Otherwise no schedule setpoint: the room reports `schedule setpoint unavailable` and requests a one-off refresh in 60 seconds. It does not invent a target.

## Download behaviour

The hub serializes downloads across rooms. Each room can have only one pending request. Missing, unknown and unavailable thermostat entities are skipped, including a zone that becomes unavailable while waiting for another download. Unloading cancels both queued and running requests and leaves the shared lock free. Each running service call is bounded to 45 seconds, and service errors are caught by the downloader.

A missing schedule is retried after five minutes initially. Consecutive unsuccessful downloads use delays of five, ten, twenty, forty and then sixty minutes (ceiling). A usable cached/live schedule keeps the normal daily refresh cadence. The failure counter caps at five, so the delay never grows beyond one hour.

## Failure classes (added for #15)

Each attempt records a small diagnostic set in the room store and publishes it on the room decision sensor (`sensor.home_heating_optimisation_control_<room>_state`):

| Attribute | Values | Meaning |
| --- | --- | --- |
| `schedule_fetch_status` | `not_attempted`, `fetching`, `ok`, `failed` | Outcome of the latest download attempt. |
| `schedule_fetch_failure_class` | `incomplete_fragments`, `transport_timeout`, `parser_error`, `unavailable`, `unknown`, or null | Why the latest attempt failed (null after success). |
| `schedule_fetch_attempts` | integer | Consecutive attempts since the last success (reset to 0 on success). |
| `schedule_next_retry_at` | ISO timestamp | Earliest time the next background attempt may start. |

The class is derived from the exception type and the message the source integration wraps into `HomeAssistantError`:

- `incomplete_fragments`: fragment assembly or decompression failed on the source side (`Failed to decompress schedule fragments`, `Incomplete schedule fragment payload set`). The controller answered, but the reassembled blob is not a valid zlib schedule. See [ramses-reliability-2026-09-20.md](ramses-reliability-2026-09-20.md).
- `transport_timeout`: no reply within the radio conversation or overall schedule timeout (`Timeout waiting for reply`, `Failed to obtain schedule within N secs`, the 45-second HHO bound, `ProtocolSendFailed`).
- `parser_error`: a reply arrived but its content is invalid (`Invalid schedule switchpoint binary block`, malformed payload, type/value errors).
- `unavailable`: the service or entity is not usable, or the call completed without publishing a schedule.
- `unknown`: any other exception.

These fields are diagnostics only. They never change retry timing, precedence or the 48-hour cache limit, and corrupt stored values fall back to `not_attempted`/0/null.

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
