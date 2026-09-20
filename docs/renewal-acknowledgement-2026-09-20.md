# Same-temperature renewal acknowledgement — investigation (20 September 2026)

Status: **investigation complete; confirmation rules unchanged.** Issue #16.

## Question

When HHO renews a radiator override at the *same* target (only the expiry moves), can any
supported RAMSES surface in Home Assistant prove that the primary thermostat's report came
from the controller *in response to this command*, rather than being the service-local
projection of the transmitted packet or an unrelated later heartbeat?

## Sources inspected

| Source | Version | Notes |
| --- | --- | --- |
| `ramses_rf` / `ramses_tx` (local reference build) | `0.60.5.post1` (`0.60.5` + two cherry-picks for MQTT pool TX) | Same code paths as the installed `0.60.5`; `ramses_cc` manifest pins `ramses-rf==0.60.5` on the live install. |
| `ramses_cc` GitHub `master` | commit `27a88eb5dc` (2026-09-20), manifest `0.60.7`, requires `ramses-rf==0.60.7` | Ahead of the installed `0.60.5`; checked for anything newer HHO could rely on. |
| `ramses_cc` tag `0.60.5` | installed version | `event.py` and `climate.py` verified for parity with master where cited. |
| `ramses_cc` release `0.55.3` (local copy) | older reference | Fired `ramses_cc_message` on the HA bus (`__init__.py:235`); that bus event no longer exists in 0.60.x. |

## Findings

### 1. Zone `mode` / `temperature` attributes carry no provenance

`ramses_cc/climate.py:724-750` (`RamsesZone.extra_state_attributes`) publishes `mode`
(`{mode, setpoint, until}`), `params`, `schedule`, etc. by calling
`resolve_async_attr(self, self._device, "mode")`. The library side,
`ramses_rf/systems/zones.py:699-707` (`Zone.mode`), returns the fields of an immutable
`ZoneState` read-model (`ramses_rf/models/state_climate.py:171-211`). `ZoneState` has one
timestamp, `last_updated`, which is a dataclass default (`field(default_factory=_now_utc)`,
line 211) set whenever the projector replaces the state; it records neither the packet
verb, direction, source address nor RSSI. No per-attribute "last inbound" field exists.

### 2. The projector applies **outbound** writes to the same read-model as inbound reports

- `ramses_tx/transport/base.py:398-415`: every transmitted frame is wrapped as a
  `Packet(..., is_tx=True)` and its DTO is handed to `protocol._msg_received(dto)`, i.e. it
  enters the same handler chain as received packets.
- `ramses_tx/protocol/base.py:538-554`: `_msg_received` forwards to the gateway handler and
  to every `add_msg_handler` callback with no `is_tx` filter.
- `ramses_rf/gateway.py:516-576`: `_msg_handler` builds the `ApplicationMessage`, runs
  `process_msg` and `state_projector.process_message_state(app_msg)`.
- `ramses_rf/state_projector.py:869` and `:988`: the only verb filter is `RQ`. A ` W` (write)
  is projected. `state_projector.py:689-695` copies `mode`, `setpoint` and `until` from any
  non-RQ 2349 payload into `ZoneState`.

So the `until` shown in HA moves as soon as HHO's own ` W 2349` is transmitted, before the
controller's ` I 2349` / `RP 2349` arrives. This is the "service-local projection" that
`docs/command-confirmation.md` already assumes; the code confirms it is a library
behaviour, not an HA timing artefact.

### 3. `last_reported` vs `last_updated` cannot separate projected from inbound state

`ramses_cc/coordinator.py:1107-1124` registers `_on_packet` via `client.add_msg_handler`
and, after `asyncio.sleep(0)`, sends `SIGNAL_UPDATE_<addr1>` and `SIGNAL_UPDATE_<addr2>`
for **every** DTO, including the TX DTO from finding 2 (addr1 is the HGI, addr2 the
controller, so the zone entity is signalled). `ramses_cc/entity.py:191-229` then calls
`async_write_ha_state()`. HA's `last_reported` therefore refreshes on the outbound write, on
the controller's reply, on any heartbeat and on any unrelated packet addressed to the
controller. `last_updated` only differs when an attribute value changed. Neither
distinguishes direction.

`ramses_cc/climate.py:1168-1182` (`async_set_zone_mode`) additionally calls
`self.async_write_ha_state()` right after `await self._device.set_mode(...)` returns, so a
state write is guaranteed inside the blocking service call. HHO's existing rule (require
`last_reported` after service completion) is the tightest usable boundary.

### 4. Command/response correlation exists in the library but does not reach HA

- `ramses_rf/commands/dispatcher.py:87-120`: `send()` takes `wait_for_reply`; the default is
  `DEFAULT_WAIT_FOR_REPLY = None` (`ramses_tx/const.py:20`). `Zone.set_mode`
  (`ramses_rf/systems/zones.py:820-848` → `send_system_intent`, `systems/helpers.py:26-50`)
  does not pass it, so the awaited result is the **echo packet** of HHO's own write
  (`Message._from_packet(packet)`, dispatcher line 120), not the controller's response.
- `ramses_tx/protocol/core.py:381-419`: `_packet_received` resolves the pending future on a
  hardware echo (`_is_echo`) or a header match against the TX header. That is link-layer
  transmit confirmation only.
- `ramses_cc/climate.py:1168-1182` discards the returned `Message` entirely.

There is no HA-visible response object, message id or correlation id for a zone write.

### 5. Packet-level events: only via an opt-in regex entity

`ramses_cc` 0.60.x has an `event` platform (`ramses_cc/event.py`, present in tag `0.60.5`).
`RamsesRegexEvent` (`event.py:213-285`) registers `client.add_msg_handler` and, when the
user has configured `advanced_features.message_events` (a regex, `const.py:52`) and
`regex.search(repr(msg))` matches, triggers an HA event entity whose `data` attribute
contains `dtm`, `src`, `dst`, `verb`, `code`, `payload`, `packet`. Because the handler chain
delivers TX DTOs too (finding 2), `verb`/`src` are the only fields that identify an inbound
controller report (`src == controller id`, verb ` I`/`RP`); `is_tx`/`is_echo` are **not**
copied into the event data.

This is usable in principle but is:

- opt-in per installation (user must enter a regex in ramses_cc advanced options);
- documented as a debugging aid, not an API; the fields and entity id are unversioned;
- a single entity for all matching packets, so under a broad regex HA may drop events
  (`EventEntity` keeps only the last event; HHO would have to subscribe to state changes
  and can miss consecutive packets in one tick);
- absent in versions where the platform did not exist and had a different bus-event form
  in 0.55.x (`ramses_cc_message`).

## Conclusion

**(2) — a trustworthy inbound signal exists only through an unsupported/private surface.**
No supported entity attribute or timestamp identifies inbound provenance; the only
packet-level path is the opt-in regex event entity, which is undocumented, unversioned and
lossy. Therefore:

- HHO's confirmation rules are **not changed**. A same-temperature renewal whose only visible
  change is the expiry remains `matching_readback_unverified`, and `readback_timed_out`
  continues to flag it after three minutes. Service-local projection exclusion (v0.6.2) and
  no acknowledgement restoration after reload (v0.6.3) are preserved.
- HHO adds a `readback_hint` attribute (below) so the operator can tell an unverified
  renewal from a genuine problem without HHO claiming failed delivery or valve movement.

## HHO changes made for #16

- `OTCoordinatorData.readback_hint` (room decision sensor attribute, `get_control_report`).
  Populated for `matching_readback_unverified` and whenever `readback_timed_out` is true
  (except `readback_reverted`, which has its own narrow meaning). Empty for `confirmed`,
  `pending_readback` and other statuses. Text is fixed in `READBACK_HINTS`
  (`control/comfort/coordinator.py`) and states: unverified is not evidence of a lost
  command; HA attributes cannot show packet direction; what to check (gateway status, zone
  mode/expiry); bounded retry and manual holds are unchanged.
- Tests (`tests/control/test_runtime.py`): hint per status and timeout combination; forbidden
  phrasing (no "not delivered", "lost", "valve moved/did not"); the full renewal flow stays
  `matching_readback_unverified` with the hint published on the entity; the timeout adds the
  timeout hint without a new write; a genuine manual change still becomes `manual`; the
  reversion retry budget is untouched.

## Upstream capability request (drafted, not posted)

> **Expose inbound provenance for zone/DHW mode state in ramses_cc**
>
> `climate.<zone>` exposes `mode: {mode, setpoint, until}`, but the underlying `ZoneState` is
> replaced both by inbound ` I`/`RP 2349` packets and by the integration's own transmitted
> ` W 2349` (state_projector applies every non-RQ verb; the TX DTO is fed through
> `protocol._msg_received`). Consumers cannot tell whether the attribute reflects the
> controller's acknowledgement or the optimistic projection of a command that may still be
> in flight.
>
> Request, smallest useful form: add to the zone climate `mode` dict (or a sibling attribute)
> two read-only fields, `reported_at` (ISO timestamp of the last **inbound** ` I`/`RP 2349`
> from the controller for that zone) and `source` (`"controller"` | `"transmitted"`),
> populated from `PacketDTO.is_tx` / `Packet._is_echo` which the transport already sets.
> Optionally include `verb`. No behavioural change to projection is requested.
>
> Acceptance: after `ramses_cc.set_zone_mode`, `mode.source` is `"transmitted"` until the
> controller's 2349 arrives, then `"controller"` with `reported_at` advanced; a heartbeat
> that does not carry 2349 leaves both unchanged.

## HHO compatibility path once available

Feature-detect, never version-sniff on the HA side:

1. In `_primary_status`, if `mode` contains `source` and `reported_at`, return them as extra
   fields; otherwise behave exactly as today.
2. In `_has_command_evidence` for `Action.WRITE`, accept a same-target report as evidence
   **only** when `source == "controller"` and `reported_at` is strictly after
   `_service_completed_at` and the reported `until` equals the expiry HHO requested (within
   the controller's minute resolution). Everything else keeps the current rules.
3. Keep `matching_readback_unverified` as the outcome whenever the fields are absent, so an
   older ramses_cc or a partial upgrade cannot widen confirmation.
4. Record `readback_provenance: "controller" | "transmitted" | "unavailable"` alongside the
   existing lifecycle fields so analytics/journal can separate eras.
5. Tests required before enabling (mirroring the issue's list): real controller ack;
   service-local projection with `source: transmitted`; expiry-only change with
   `source: controller` but stale `reported_at`; unrelated heartbeat (fields unchanged);
   stale data (> 5 min); radio outage (`unavailable`); missing fields → unchanged behaviour.

Until upstream ships such fields, the regex event entity is deliberately **not** used: it is
opt-in, unversioned and lossy, and coupling live control confirmation to a debugging aid
would be less trustworthy than the current explicit "unverified" outcome.
