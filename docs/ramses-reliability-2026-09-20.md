# RAMSES schedule retrieval and gateway reliability: findings, 20 September 2026

Issue [#15](https://github.com/xpenno255/ha-home-heating-optimisation/issues/15). Read-only investigation of the live installation; no services were called, no devices rebooted, no state written. Identifiers are replaced by `<controller>`, `<gateway-a>`, `<gateway-b>`.

## Component versions

| Component | Version |
| --- | --- |
| Home Assistant Core | 2026.9.3 |
| ramses_cc (custom component, MQTT pool bridge build) | 0.60.5 |
| ramses_rf (bundled wheel) | 0.60.5.post1 (`ramses-rf/ramses_rf` tag `0.60.5` + 3 commits; the local reference checkout matches the installed wheel) |
| home_heating_optimisation | 0.6.4 live |

## Gateway connectivity (snapshot 11:37 UTC, ~2 h after the 09:35 restart)

| Entity | State | `last_changed` | `pkts_received` | `consecutive_errors` |
| --- | --- | --- | --- | --- |
| `binary_sensor.<gateway-a>_online` | on | 09:35:12 | 1106 | 0 |
| `binary_sensor.<gateway-b>_online` | on | 09:35:12 | 1074 | 0 |

Both gateways have been online continuously since restart with packets arriving every minute (`last_pkt_time` within the last minute of the snapshot). The `MqttPoolBridge: HGI <gateway> offline (LWT)` warning fired twice (09:38:58, 10:30:00) without the online sensors flipping off, so each LWT drop was followed by an online message before the next sensor update. This is short MQTT-session flapping of one gateway, not an outage; the pool continued on the other child. The earlier outage that needed a reboot is not present.

## Schedule retrieval evidence

`ramses_cc` refreshes every zone schedule periodically (`_refresh_schedules` -> `get_schedule(force_io=True)`), independent of HHO; on this installation the cycles were observed hourly. Warnings at 09:36, 10:01 and 11:01 line up with those refreshes:

| Time (UTC) | Logger | Message (sanitised) |
| --- | --- | --- |
| 09:36:31, 10:01:01, 11:01:01 | `ramses_rf.systems.schedule` | `<controller>_01 (RAD) (schedule): Fragment 1 fetch retry 1 failed: Failed to decompress schedule fragments` and `Fragment 2 fetch retry 2 failed: ...` (3 occurrences) |
| 09:36:43 to 11:01:13 | `ramses_rf.pipeline.conversation` | `Conversation for <controller>:0006:<uuid> timed out after 3 retries.` (7 occurrences) |

Zone attribute state at 11:37: zones 00, 02, 04, 05 and 08 hold a 7-day `schedule` with `schedule_version` 0; zones 01, 03, 06 and 07 have `schedule: null`. Zone 06 held a full schedule at the 11:05 snapshot and lost it after the 11:01 refresh cycle.

All nine HHO rooms reported `schedule_source: evohome` throughout, so the RF path was never on the control path during the observation.

## Origin of the two warnings (upstream code, 0.60.5.post1)

**`Failed to decompress schedule fragments`** is raised in `ramses_rf/systems/schedule.py::Schedule._proc_payload_set` when the fragment set is complete but `zlib.decompress()` of the concatenated hex fails. `_fetch_schedule` catches the resulting `ScheduleError`, logs `Fragment N fetch retry M failed`, sleeps with exponential backoff (0.5 s base) and retries up to `MAX_FETCH_ATTEMPTS = 5`, then raises `ScheduleFlowError`, which `ramses_cc.climate.async_get_zone_schedule` wraps as `HomeAssistantError("Failed to get zone schedule: ...")`. Because the failure happens after the *last* fragment arrives, it is a reassembly/content problem, not a missing packet: the controller answered with the requested fragment count, but the blob was not a valid zlib stream. HHO classifies this as `incomplete_fragments`.

Two upstream behaviours matter for HHO:

- `_get_schedule` clears `_full_schedule` before fetching whenever the schedule is dated. If the fetch then fails, the entity's `schedule` attribute becomes null (observed on zone 06). HHO's 48-hour store cache covers this window; the cache stamp is only renewed by a live attribute or a successful download.
- Passive `0404` fragments seen on air are also merged (`process_schedule_msg`). A fragment set that does not decompress is dropped and the state machine moves to `Faulted` (`Dropped corrupted schedule fragments`). With two gateways in one pool, every reply is received twice; duplicates land in the same slot, so this alone should be harmless, but interleaving with an eavesdropped set from a changed schedule is a plausible corruption path that only a packet log can confirm.

**`Conversation ... timed out after 3 retries`** comes from `ramses_rf/pipeline/conversation.py::ConversationManager._handle_timeout`. Each request/reply conversation waits `DEFAULT_RPLY_TIMEOUT = 1.0` s, re-sends up to `MAX_RETRY_LIMIT = 3` times, then fails the future with `ProtocolTimeoutError`. The key `<controller>:0006:<uuid>` shows every timed-out conversation was an `RQ|0006` schedule-version request to the controller, issued by `tcs._schedule_version(force_io=True)` at the start of each zone refresh. Seven timeouts across roughly 27 refreshes (three cycles, nine zones) is a few per cent loss with a 1-second reply budget over RF via MQTT; the schedule fetch fails fast in that case and HHO would classify it as `transport_timeout`.

## Distinguishing the three failure types on this installation

| Type | Seen today | Evidence |
| --- | --- | --- |
| Incomplete fragments / decompression | Yes, zone 01 only, every hourly cycle | Three `Failed to decompress` cycles while both gateways were online with packets flowing; five other zones decoded normally in the same cycles. |
| Transport timeout | Yes, intermittent | Seven `0006` conversation timeouts at 1 s budget; no zone lost its schedule only because of these. |
| Parser error | No | No `Invalid schedule switchpoint binary block` or payload validation errors in the log. |

## Upstream issue search (read-only)

Issue search on `ramses-rf/ramses_rf` and `ramses-rf/ramses_cc` (the repositories `zxdavb/*` redirect to) for "decompress", "schedule fragment", "conversation timed out" and "0006" found no open report matching this failure. Related closed design work: [ramses_rf#922](https://github.com/ramses-rf/ramses_rf/issues/922) (reactive stateful schedule model, the code path above) and [ramses_cc#884](https://github.com/ramses-rf/ramses_cc/issues/884) (schedule management exposed to HA). Open [ramses_rf#669](https://github.com/ramses-rf/ramses_rf/issues/669) covers multi-packet array reassembly generally but not `0404`.

No upstream report has been posted. A sanitised reproducer is prepared at [upstream/ramses-schedule-fragments-reproducer.md](upstream/ramses-schedule-fragments-reproducer.md); it still needs a packet-level capture of the zone 01 fragments before it is worth filing, because the decompression failure could equally be a genuinely odd schedule stored on the controller for that zone.

## What changed in HHO (#15)

- Every download attempt records `schedule_fetch_status`, `schedule_fetch_failure_class`, `schedule_fetch_attempts` and `schedule_next_retry_at` and exposes them on the room decision sensor. Retry timing, precedence and the 48-hour cache limit are unchanged; tests now pin all of them (`tests/control/test_schedule_fetch.py`).
- Documentation describes the failure classes and the precedence order explicitly.

## Recommendation: observation period

1. Observe for 7 days from 20 September 2026 without rebooting gateways. Track per room: `schedule_fetch_failure_class` history, the daily count of `Failed to decompress schedule fragments` warnings by zone, `0006` conversation timeouts, and `MqttPoolBridge ... offline (LWT)` events versus the online sensors.
2. If zone 01 keeps failing every cycle while the other zones decode and both gateways are online, capture a packet log for one refresh (owner action; `packet_log` in the ramses_cc configuration) and complete the reproducer with the hex fragments before filing upstream. If the failure clears after the owner re-saves that zone's schedule on the controller, treat it as controller-side schedule content and close without an upstream report.
3. If LWT offline events cluster on one gateway, look at that gateway's MQTT keepalive/Wi-Fi rather than the radio path. Gateway monitoring is issue #27.
4. Because every HHO room currently uses the cloud schedule, none of this affects live control today. It matters only for the cloud-outage fallback, which the 48-hour cache covers.

Nothing in this note claims physical commissioning, measured efficiency or energy savings.
