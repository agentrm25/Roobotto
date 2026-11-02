# ADR-018: Local web workbench instead of a terminal HUD

- **Status:** Accepted
- **Date:** 2026-11-02
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 5.3 (HUD row), Section 7, Section 11, Appendix B (T-16, which supersedes T-15)

## Context

The plan gave the operator one screen, a terminal HUD (T-15), and left every owner-run step
(the bench worksheet in Section 10.2, recording, annotation, replay, the benchmark) on the
command line. The owner is not strong on the CLI and asked for something that guides him
through those steps. ADR-006 already says the `MusicalState` schema later becomes a public
OSC/WebSocket state protocol, so a local page that subscribes to engine state was foreseen.

## Decision

- Roobotto's operator surface is a **local web workbench**: `roobotto ui` starts an HTTP server
  bound to `127.0.0.1` only, serves one static page, and opens the browser. It replaces the
  terminal HUD. T-16 (Appendix B) is the task card; T-15 is folded into it.
- Phase 0 uses the standard library for the server (`http.server`, server-sent events for live
  updates). **No UI dependency is added in Phase 0.** rich and textual leave the planned
  dependency list. Adopting a web framework or a JavaScript build step later needs a new ADR.
- The workbench holds no logic of its own. Every button calls the same function the matching
  CLI command calls, and every screen has a CLI equivalent listed in the docs. Nothing in the
  UI talks to an adapter directly.
- Operator actions from the workbench (RESUME, arm, disarm, re-enable tracker authority) enter
  the engine through the same input-authority path as the footswitches, with operator
  provenance (ADR-011). Arming from the workbench lasts for the server's lifetime at most and
  is never written anywhere (I-2).
- The bench-worksheet screen guides and records; it never sends MIDI. First-time verification
  stays outside Roobotto with external tools (ADR-009, audit F-11). The screen may set a
  capability record to `bench_verified` only when a bench log path under `captures/` is given.
- The workbench ships in slices with the cards whose functions it wraps: the bench worksheet
  with WP3, doctor and devices with T-06, the recorder with T-07, run and HUD with T-14.

## Consequences

- `src/roobotto/ui/` becomes the workbench package. `roobotto ui` joins the CLI list.
- The M7 iPad HUD over OSC/WebSocket (Section 6) reads the same state feed the workbench reads.
- A replay with the workbench attached produces the same normalized decision hash as a replay
  without it (I-19); the UI is an observer plus an operator-input source, never a decider.
- The HUD row of the control map (Section 5.3) keeps its meaning. "Keyboard" becomes a
  workbench button with a confirmation step.
- If this decision changes, T-16's screens must move to the new surface without changing the
  functions they call.
