# ADR-010: Control map and gesture commitment

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Sections 5.2–5.3 (audit findings F-1, F-6)

## Context

A gesture-based switch can issue a forward cue before the gesture is recognized. Stopping
Roobotto and restoring today's rig are different operations.

## Decision

- GO has its own single-purpose switch.
- BACK (tap) and MANUAL (hold) share a switch that never issues GO.
- STOP has its own switch and acts the instant it is pressed (press edge).
- RESTORE is the H4's preset A.
- RESUME is a deliberate HUD action.

## Consequences

A gesture can never issue a forward cue before it is recognized. Invariants I-17 and I-18
cover this. The config validator rejects a control map that mixes forward with backward or
safety actions on one switch.
