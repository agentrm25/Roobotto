# AGENTS.md — Roobotto

Roobotto is a local, open-source stage manager for live bands. It estimates where the band is
in a song from audio, MIDI and footswitch evidence and drives production cues through a
deterministic, safety-gated cue engine. Read docs/implementation-plan.md (Sections 3–5 hold
the contracts) before any task. Work only on the task card you were given.

## Non-negotiables
1. Physical outputs are off by default. Never enable, arm or send to real hardware in code,
   tests or examples. Use the mock adapter. There is no override for unverified commands;
   first-time hardware verification is done by the owner outside Roobotto.
2. Never invent device mappings. Every hardware command must reference a capability record
   marked bench_verified, with a bench log cited in docs/hardware-verification.md.
3. ML produces evidence; deterministic code decides. Anything that fires goes through the
   decision gate (a pure function) and the output supervisor.
4. The engine core is a step function with an injected clock (ADR-014): no wall-clock reads,
   no unseeded randomness. Replay and live share one code path; nothing reads audio beyond
   "now" (keep the future-read guard on in tests).
5. Only performer-intent events authorize GO, BACK or critical cues (ADR-011). Echoes, state
   observations, preset-load recalls, duplicates and unknown provenance never do: fail closed.
6. Cue identity is the occurrence (performance, song instance, section visit, cue id), never
   the scheduler generation. One-shots fire at most once via the fired ledger (ADR-012).
7. Tracker evidence alone never fires a critical cue, in any mode or milestone (ADR-013).
8. The audio callback only timestamps and enqueues buffers: no inference, parsing, disk,
   network, or locks shared with workers.
9. Essential guitar and vocal audio never passes through Roobotto.
10. Never commit audio. Lyrics and private show data stay in local/. Demo evidence is a text
    transcript from a synthetic or non-private replay.
11. Every research or hardware claim you rely on gets an entry in docs/evidence-register.md
    (claim, source, status, local test, result). Mark anything unconfirmed [verify].

## Invariants (tests in tests/property/test_invariants.py; changing one needs an ADR)
I-1 unverified mapping → mock only, no override · I-2 no physical output unarmed; arming never
persists · I-3 manual action cancels pending automation · I-4 expired cues discarded · I-5 no
resend of a ledgered one-shot after restart/reconnect; restart builds no past work · I-6
identity = occurrence, never generation · I-7 at most one output per occurrence · I-8 lost or
stale input never raises authority; reconnects never restore it silently · I-9 no future reads
· I-10 MIDI channel conversion in one function · I-11 every output attempt logged · I-12
performer's manual changes win · I-13 every recipient of a message must be known-safe · I-14
no critical cue on tracker evidence alone, any mode, any milestone · I-15 rate limits; state vs
one-shot events · I-16 only performer intent authorizes · I-17 no switch mixes forward with
backward/safety actions; BACK/MANUAL never issue GO; STOP on press edge · I-18 STOP cancels
everything; RESUME is deliberate, releases nothing stale, one owner per device · I-19 same
consumed input trace + config + seed → same normalized decision hash.

## Conventions
- Python 3.12, uv, src layout (src/roobotto). ruff (lint + format), pyright, pytest + hypothesis.
- Units: arrangement durations in bars/beats; physical intervals in explicit time units (ms in
  config, ns at runtime); all runtime time comes from the injected clock.
- MIDI channels are 1–16 in config and logs; convert only in adapters/midi_wire.py.
- Config: YAML → pydantic models in src/roobotto/config; regenerate schemas/ via
  `uv run roobotto schemas export`.
- Logs: JSONL with run id, config hash and clock time. Normalized decision hashes exclude run
  ids, absolute clock origins and physical delivery measurements.
- Ask before adding any dependency; record its license in THIRD_PARTY.md. No GPL/AGPL code in
  core; non-commercial models only under research/.

## How to work
- One task card per branch/PR. Restate the acceptance tests, write them first, keep PRs small.
- If part of a card is deferred, list the acceptance checks that remain unsatisfied; never mark
  them done.
- Stop and ask the owner before: adding a dependency, changing a schema, contract or
  invariant, touching a real-device adapter, or anything that changes what reaches hardware.
- Done = acceptance tests pass; `uv run ruff check`, `uv run pyright`, `uv run pytest` green;
  docs and evidence register updated; PR lists what was verified vs assumed.
- Never claim something was tested on hardware unless a bench log in captures/ shows it.
- Any work Randy must do outside the coding environment (equipment setup, wiring, bench
  tests, captures, recordings, annotation, device settings) needs its own step-by-step Tech
  Order in docs/tech-orders/, written from the template there before you ask him. Link it
  from the card/PR; checks that depend on it stay open until its sign-off is filled in.
