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
- The device was sent to too recently. Rate limits are per device: two sends to one device,
  through any of its commands and within one commit too, must be at least the larger of the
  two commands' `min_interval_ms` apart (I-15).
- Any device listening on the port and channel is not the intended target or declared `ignores`.
  The target itself must also list that port and channel as intended. This repeats the rig
  validator's check (I-13).

**Two commands to one device in one cue** (ADR-017). The first goes at once. Each later one is
deferred by `timing.same_device_spacing_ms` (10 ms) or the device's rate limit, whichever is
longer. It is refused at once if that would land past the cue's `on_miss` window. When it falls
due, it is checked again, and dropped instead of sent in any of these cases:

- STOP is active
- the generation has changed
- the performer changed that device in this visit
- any send-time check fails

## Authority

**Performer intent comes from two places only** (ADR-011): the press edge of a press-gesture
switch (GO, STOP, EXTEND) on the footswitch source's authoritative path, and a snapshot
transition that the song maps with `advance_on` from the current section. A snapshot is read
through the device's snapshot report (ADR-015), and counts only if that report is
`bench_verified` or the message arrived on a mock port.

**Never performer intent:**

- anything on a non-authoritative path (a duplicate)
- an adapter-marked automated recall
- a message matching one Roobotto sent, within the echo window, to a device that reports through
  the same input source. Echoes are scoped to the device that can produce them, so an unrelated
  output can never swallow a footswitch press.
- a snapshot within the initialization window after that source's preset change. Any preset
  change starts the window, whatever its provenance: automated, echoed or arriving on a
  duplicate path.
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
tracker evidence only in ASSISTED or AUTO, with tracker authority enabled, from M4 (decorative)
or M5 (standard), and at or above the cue's posterior threshold. Only a fresh packet in
`following` mode about the selected song counts as evidence.

**Tracker authority** starts off. Only the operator turns it on. A new tracker session, a stale
stream (no fresh packet for `tracker_stale_ms`), a packet whose mode is not `following`, or STOP
turns it off and drops the engine to MANUAL. A reconnect never turns it back on (I-8).

## Identity and the fired ledger

An occurrence is (performance, song instance, section visit, cue id). The scheduler prepares an
occurrence once. Cancels, reschedules and relocalization re-stamp or drop pending work; they never
create a second occurrence (I-6, I-7).

Before any send, the supervisor appends an `attempting` record to the ledger and fsyncs it. Any
record blocks a second automatic send, including one whose outcome is unknown (I-5).

Every record is one line ending in a newline, so a crash mid-write leaves a final line without
one. On load, that tail is repaired on disk and fsynced before any new record is accepted. A
complete record gets its newline back, and a torn fragment is cut off. A malformed line that does
end in a newline is corruption, and the run refuses to start.

Explicit operator resync may resend retryable state, but never a one-shot and never a critical
cue. Nothing is resent automatically after a restart or reconnect.

A restart starts unarmed with nothing pending. Every song instance is recorded in the ledger as it
starts (ADR-016), and a restart continues after the highest one, so no occurrence id is reused
and no past occurrence becomes new work.

## Footswitches and GO timing

The control map (Section 5.3, ADR-010) is built from raw press and release edges in
`src/roobotto/control/gestures.py`:

- **GO, STOP, EXTEND** act on the press edge. Each has a switch to itself.
- **BACK** is a tap on the shared BACK/MANUAL switch: released before `tap_max_ms` (500 ms). It is
  recognized on release, and the release authorizes it.
- **MANUAL** is a hold on that switch: recognized the moment the hold reaches `hold_ms`
  (500 ms). The release that follows does nothing.

BACK and MANUAL never issue a GO (I-17): their code paths never reach the GO handler. Rig
validation keeps GO and EXTEND off any switch that has a tap or hold.

**BACK.**

- If nothing from the last GO has been committed, BACK cancels that GO with no output. It
  restores the previous visit and puts back its still-pending cues.
- If anything was committed, BACK re-enters the previous section as a new visit. That visit's
  occurrences are authorized by the BACK release, and its entry cues are sent now, unquantized.
- BACK never moves forward. It is refused while STOP is active, and after RESUME until the next
  GO.

**MANUAL** has no effect before M3, and is logged. From M3 a hold turns tracker authority off,
and fires or cancels nothing. A second hold turns it back on only if the performer's own hold
turned it off. Authority the system suspended (stale stream, new tracker session, a lost
tracker, STOP) comes back only through the operator. This reconciles Section 5.3 ("toggle off
or on") with ADR-006 ("only the operator can re-enable it") by taking the stricter reading.

**GO timing** (Section 8) is a pure function in `src/roobotto/control/go_timing.py`. Every GO
writes a `go` record with its press time and class. The send times are in the output attempts it
authorized.

- **F, no lock.** There is no fresh, following tracker state for the song, or no downbeat within
  a bar. The press is the downbeat and the cues go now.
- **L, late.** The GO comes within the late window (a quarter bar) after a downbeat. The section
  counts from that downbeat and the cues go now.
- **Q.** The GO comes before an entry cue's send deadline, which is the next downbeat minus
  device latency minus the margin. The cue is sent at the deadline.
- **D.** The GO comes after the send deadline but before the downbeat. The cue's `on_miss`
  applies, and it is never deferred to the next bar.

Tracker relocalization re-times only the bar-counted cues after the entry. It never moves the
entry cues a GO or BACK placed.

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
- holds an explicit "awaiting GO" state until the next authorized GO: the tracker cannot move
  the engine, resync is refused and nothing fires (I-18)

## Where it is tested

| Invariant | Tests |
|---|---|
| I-1 to I-19 | `tests/property/test_invariants.py`: a Hypothesis state machine, one named check per invariant, run after every step; plus a property test over random control maps for I-17 |
| I-1, I-2, I-13, I-15 | `tests/unit/test_supervisor.py` (routing matrix, including a rig that skipped validation) |
| I-9 | `tests/replay/test_trace_replay.py` (future-read guard) |
| I-10 | `tests/unit/test_midi_wire.py` (single conversion function, checked by scanning the source) |
| I-14 | `tests/unit/test_gate.py` (every mode × milestone × posterior); `tests/scenarios/test_audit_scenarios.py` |
| I-19 | `tests/replay/test_trace_replay.py` (loss, reordering, simultaneous input, staleness, tracker restart, clock jump) |
| Audit scenarios F-6, F-7, F-8, F-9 | `tests/scenarios/test_audit_scenarios.py` |
| F-1 gestures, F-10 GO timing (T-05) | `tests/scenarios/test_manual_control.py`; tables in `tests/unit/test_go_timing.py` and `tests/unit/test_gestures.py` |

## Known gaps

- **Automated recalls (E-21).** An unmarked mid-song Showcase or Command Center recall of a mapped
  snapshot is indistinguishable from a press and would count as a GO. H-2 must resolve this
  before snapshot-mapped GO is used with automation.
- **Unverified input reports.** The template's Helix reports are `unknown` until H-1 and H-2, so
  on real hardware Helix snapshots are tracker evidence only, never a GO (E-25).
- **Rate limits across restarts.** The supervisor's rate-limit memory does not survive a restart.
- **Spacing and skip windows.** A cue with `on_miss: skip` cannot space two commands to one device
  (ADR-017), so its second intent is refused and logged.
- **Gesture and timing thresholds (E-26).** The 500 ms tap and hold thresholds and the quarter-bar
  late window are plan starting values. Bench test P-1 measures real footswitch timing.
- **Not yet built.** EXTEND and tracker-detected repeats come with M4.
- **Not tested on hardware.** No command is bench-verified, so nothing has reached hardware.
- **Not measured on the Mac.** Real-time deadlines and scheduler jitter have not been measured.
