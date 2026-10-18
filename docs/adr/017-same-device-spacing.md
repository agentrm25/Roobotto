# ADR-017: Spacing two sends to one device within a cue

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 5.6 (I-4, I-15); follows the review of PR #2

## Context

Rate limits are per device (I-15). A cue that set two commands on one device had its second
intent refused at once, and because the fired ledger closes the occurrence, that intent was lost
for good.

## Decision

- Within one cue, the first intent for a device is sent at once. Each later intent for the same
  device is deferred.
- The gap is `timing.same_device_spacing_ms` (default 10 ms) or the device's rate limit, the larger
  of the two commands' `min_interval_ms`, whichever is longer. The rate limit is never shortened.
  For a true 10 ms gap, set both commands' `min_interval_ms` to 10 or less.
- A deferred intent that would land past the cue's `on_miss` window is refused at once (I-4).
- When a deferred intent falls due, it is checked again, and dropped instead of sent in any of
  these cases:
  - STOP is active
  - the generation has changed
  - the performer changed that device in this visit
  - the port is disconnected
  - routing or arming no longer allows it
- The ledger records `attempting` before the first send of the cue, and the result after the
  last send or drop. A restart inside the gap loses the deferred intent; it is never resent
  (at most once).

## Consequences

- Rig schema: a new `timing.same_device_spacing_ms` field.
- The spine flushes due deferred sends on every step, and the wrapper wakes for them, so replay
  stays exact (I-19).
- With today's commands, whose rate limits are 100 to 250 ms, the rate limit sets the gap.
- A cue with `on_miss: skip` cannot space two commands to one device, because the skip window
  closes at the send margin (5 ms). The second intent is refused with a logged reason.
