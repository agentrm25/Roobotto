# ADR-013: Critical-cue authority

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 5.1 (audit finding F-9)

## Context

The safety invariant must not appear to expire at a later milestone (M5), or be loosened by
configuration.

## Decision

Tracker evidence alone never fires a critical cue, in any mode or at any milestone.
Relaxing that for a specific cue takes its own ADR, citing held-out and gig evidence. A
milestone, a config flag, or a fresh tracker packet cannot grant it.

## Consequences

Invariant I-14. Property tests check it in every mode.
