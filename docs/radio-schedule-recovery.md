# RF schedule recovery

Room temperature control uses the configured cloud schedule, falling back to the existing RAMSES schedule/cache when needed. A radio schedule download is optional background work; waiting for it must not hold up a radiator command or boiler calculation.

The hub serializes downloads across rooms. Each room can have only one pending request. Missing, unknown and unavailable thermostat entities are skipped, including a zone that becomes unavailable while waiting for another download. Unloading cancels both queued and running requests. Each running service call is bounded to 45 seconds, and service errors are caught by the downloader.

A missing schedule is retried after five minutes initially. Consecutive unsuccessful downloads use delays of five, ten, twenty, forty and then sixty minutes. A usable cached/live schedule keeps the normal daily refresh cadence. A successful download refreshes cache age even if its content is unchanged. The existing 48-hour limit on offline cached schedules remains in place.

These measures reduce traffic during radio faults; they do not establish gateway connectivity or repair a failed transport. Check gateway online/packet-receipt diagnostics separately. A pool with one working gateway can continue serving both rooms while another gateway is offline.

Command acknowledgements have different requirements from schedule availability; see [command confirmation](command-confirmation.md). A source entity update or a matching setpoint must not be presented as proof of physical valve movement.
