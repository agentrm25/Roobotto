# Safety model

The contracts are defined in `implementation-plan.md` Section 5 and ADR-009 to ADR-014. This
file says where the control spine (T-04) enforces each one, where it is tested, and what is still
open. If this file and the plan disagree, the plan wins and this file is wrong.

## How a cue gets out

```
adapters ──► Runner (process wrapper and event bus)
              stamps each input from the injected clock, records the consumed input trace,
              writes JSONL logs
                 │
                 ▼
             Engine.step(input)            src/roobotto/engine/core.py
              provenance classification    src/roobotto/engine/provenance.py
              scheduler                    src/roobotto/cues/scheduler.py
              decision gate (pure)         src/roobotto/cues/gate.py
                 │ commits
                 ▼
             Supervisor.submit             src/roobotto/cues/supervisor.py
              fired ledger                 src/roobotto/cues/ledger.py
                 │
                 ▼
             mock adapter  |  MIDI adapter (disabled unless given the mido backend)
```

The engine core reads no clock. Every input carries the time it became available, stamped by
the wrapper. The core uses no randomness and does no I/O, so replay and live runs share one
code path. `roobotto run --dry-run` drives the same path with everything going to the mock
adapter.

## What reaches hardware

A command reaches the MIDI adapter only if all of these hold. Otherwise it goes to the mock
adapter. There is no override (I-1, I-2, ADR-009).

- The rig profile is `physical` and the command's port is not a mock port.
- The command is `bench_verified`, cites a bench log, and has a wire message for the value.
- Outputs are armed. Arming is a HUD action for this run only. It is not part of any config, and
  every process starts unarmed.
- The MIDI adapter was constructed with a port backend. The mido backend exists, but nothing
  constructs it by default and no test opens a real port. `roobotto run` without `--dry-run` is
  refused, so no code path reaches hardware yet.

The supervisor also refuses a send when:

- STOP is active, or the commit comes from an older generation.
- The occurrence is already in the fired ledger.
- The device is missing or owned by the performer.
- The port is disconnected. Nothing is queued or resent later.
- The per-command `min_interval_ms` has not passed.
- Any device listening on the port and channel is not the intended target or declared `ignores`.
  The target itself must also list that port and channel as intended. This repeats the rig
  validator's check (I-13).

## Authority

**Performer intent comes from two places only** (ADR-011): the press edge of a press-gesture
switch (GO, STOP, EXTEND) on the footswitch source's authoritative path, and a snapshot
transition that the song maps with `advance_on` from the current section. A snapshot is read
through the device's snapshot report (ADR-015), and counts only if that report is
`bench_verified` or the message arrived on a mock port.

**Never performer intent:**

- anything on a non-authoritative path (a duplicate)
- an adapter-marked automated recall
- a message matching one Roobotto sent within the echo window
- a snapshot within the initialization window after a Program Change
- an unmapped snapshot, or a mapped one whose report is not `bench_verified` on a physical port
- a port that is not exactly one source's input path
- a message that matches no switch and no report record

These still update Roobotto's copy of device state where they say something about it.

**Authorization is tied to occurrences** (Section 5.4):

- A GO authorizes the occurrences of the visit it creates.
- A critical cue takes that authorization only if `requires.corroborate_by` lists the kind of
  action (footswitch or mapped snapshot) and the cue lies within `requires.within_bars`.
- When the tracker enters a visit in ASSISTED mode, its critical cues are only proposed. A GO
  press made after the proposal was armed authorizes that one proposal, and nothing else.

**Tracker evidence alone never fires a critical cue** (ADR-013, I-14). The gate refuses it before
it looks at mode, milestone, posterior or freshness. Decorative and standard cues may fire on
tracker evidence only in ASSISTED or AUTO, with tracker authority enabled and fresh, from M4
(decorative) or M5 (standard), and at or above the cue's posterior threshold.

**Tracker authority** starts off. Only the operator turns it on. A new tracker session, a stale
stream (no fresh packet for `tracker_stale_ms`), or STOP turns it off and drops the engine to
MANUAL. A reconnect never turns it back on (I-8).

## Identity and the fired ledger

An occurrence is (performance, song instance, section visit, cue id). The scheduler prepares an
occurrence once. Cancels, reschedules and relocalization re-stamp or drop pending work; they never
create a second occurrence (I-6, I-7).

Before any send, the supervisor appends an `attempting` record to the ledger and fsyncs it. Any
record blocks a second automatic send, including one whose outcome is unknown (I-5).

Explicit operator resync may resend retryable state, but never a one-shot and never a critical
cue. Nothing is resent automatically after a restart or reconnect.

A restart starts unarmed with nothing pending. Every song instance is recorded in the ledger as it
starts (ADR-016), and a restart continues after the highest one, so no occurrence id is reused
and no past occurrence becomes new work.

## STOP and RESUME

STOP acts on the press edge. It does four things:

- cancels all pending work with a new generation
- clears any proposal
- suspends tracker authority
- refuses every GO and resync until RESUME

RESUME needs an active STOP and an operator confirmation that the H4 is on preset B. It then:

- starts a new generation
- marks every device's state unknown
- releases nothing that was pending
- sends nothing until the next authorized GO (I-18)

## Where it is tested

| Invariant | Tests |
|---|---|
| I-1 to I-16, I-18, I-19 | `tests/property/test_invariants.py`: a Hypothesis state machine, one named check per invariant, run after every step |
| I-1, I-2, I-13, I-15 | `tests/unit/test_supervisor.py` (routing matrix, including a rig that skipped validation) |
| I-9 | `tests/replay/test_trace_replay.py` (future-read guard) |
| I-10 | `tests/unit/test_midi_wire.py` (single conversion function, checked by scanning the source) |
| I-14 | `tests/unit/test_gate.py` (every mode × milestone × posterior); `tests/scenarios/test_audit_scenarios.py` |
| I-19 | `tests/replay/test_trace_replay.py` (loss, reordering, simultaneous input, staleness, tracker restart, clock jump) |
| Audit scenarios F-6, F-7, F-8, F-9 | `tests/scenarios/test_audit_scenarios.py` |

I-17 (gestures) lands with T-05.

## Known gaps

- **Automated recalls (E-21).** An unmarked mid-song Showcase or Command Center recall of a mapped
  snapshot is indistinguishable from a press and would count as a GO. H-2 must resolve this
  before snapshot-mapped GO is used with automation.
- **Unverified input reports.** The template's Helix reports are `unknown` until H-1 and H-2, so
  on real hardware Helix snapshots are tracker evidence only, never a GO (E-25).
- **Rate limits across restarts.** The supervisor's rate-limit memory does not survive a restart.
- **Not yet built.** Tap and hold gestures (BACK, MANUAL), the class L late window and the
  table-driven GO policy come with T-05. EXTEND and tracker-detected repeats come with M4.
- **Not tested on hardware.** No command is bench-verified, so nothing has reached hardware.
- **Not measured on the Mac.** Real-time deadlines and scheduler jitter have not been measured.
