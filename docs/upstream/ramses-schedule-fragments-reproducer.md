# Draft upstream report: `Failed to decompress schedule fragments` on one zone every refresh

Status: prepared 20 September 2026, **not posted**. Needs the packet capture in step 3 before filing. All identifiers sanitised.

Target repository: `ramses-rf/ramses_rf` (code path) with a cross-reference to `ramses-rf/ramses_cc`.

## Environment

- ramses_rf 0.60.5.post1 (wheel bundled with ramses_cc 0.60.5), Home Assistant 2026.9.3.
- One Evohome controller, nine radiator zones, two ESP gateways in one MQTT pool (`MqttPoolBridge`).
- Both gateways `online`, `consecutive_errors: 0`, packets received every minute during every failing cycle.

## Observed

On every hourly schedule refresh (`_refresh_schedules` -> `get_schedule(force_io=True)`), zone `01` logs:

```
<controller>_01 (RAD) (schedule): Fragment 1 fetch retry 1 failed: Failed to decompress schedule fragments
<controller>_01 (RAD) (schedule): Fragment 2 fetch retry 2 failed: Failed to decompress schedule fragments
```

and ends with `ScheduleFlowError`; the zone's `schedule` attribute becomes `null`. Five other zones decode a 7-day schedule in the same cycles. Intermittently, `RQ|0006` conversations also log `timed out after 3 retries` (1 s reply timeout, 3 retries), which is a separate transport symptom.

## Why this looks like reassembly rather than loss

`Schedule._proc_payload_set` raises only after the fragment set is complete (`None not in payload_set`), so the controller returned every requested fragment and the concatenated hex was not a valid zlib stream. The retry loop re-requests the same fragment number, gets the same bytes, and fails the same way, which matches a deterministic content or ordering problem rather than RF loss.

Candidate causes to rule in or out with a capture:

1. The stored schedule for that zone is genuinely not zlib-compressed as the parser expects (controller-side content). Re-saving the zone schedule on the controller would change the bytes.
2. Fragments from two schedule versions interleaved: an eavesdropped `I|0404` or a second gateway's copy of an `RP|0404` merged by `process_schedule_msg` into the active set after `_update_payload_set` reset it.
3. Fragment ordering/`total_frags` mismatch when the controller reports a different total on fragment 2 than on fragment 1.

## Minimal reproducer (to complete)

1. Enable `ramses_rf.systems.schedule` and `ramses_tx` debug logging and a `packet_log` in ramses_cc.
2. Wait for one hourly refresh or call `ramses_cc.get_zone_schedule` for the failing zone once (owner action; not done in this investigation).
3. From the packet log, collect every `0404` frame for `zone_idx 01` in that window, sanitising `<controller>` and `<gateway>` addresses. Note which gateway received each frame and whether any frame appears twice.
4. Feed the payload hex strings offline:

```python
import zlib
from ramses_rf.systems.schedule import fragments_to_full_schedule

fragments = ["<frag1 hex>", "<frag2 hex>"]  # in fragment_number order, from the packet log
try:
    print(fragments_to_full_schedule(fragments))
except zlib.error as err:
    print("decompress failed:", err)
```

If the concatenated fragments from a single gateway decompress but the merged set from both gateways does not, the report is about duplicate/interleaved fragment handling in `process_schedule_msg`/`_update_payload_set`. If they never decompress, the report is about controller content and belongs with the schedule parser hardening (return a clear `ScheduleError` instead of retrying five times).

## Ask upstream

- Do not retry a completed-but-undecodable fragment set five times with backoff; fail fast with the fragment total and lengths in the message.
- Keep the previous `_full_schedule` when a forced refresh fails, or expose that the schedule is `Faulted` instead of publishing `null`, so consumers can distinguish "no schedule" from "fetch failed".
- Consider making the `0006` conversation timeout configurable or larger than 1 s for MQTT-bridged gateways.
