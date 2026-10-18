# ADR-014: Determinism and replay

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 4.1 (audit finding F-2)

## Context

Deterministic replay needs a defined input, clock, and recording contract. Live and replay
must share one code path.

## Decision

The engine core is a step function with an injected clock. It reads no wall clock and uses no
unseeded randomness. The engine records the input trace it actually consumed. Decisions are
hashed in a normalized form that excludes run ids, absolute clock origins, and physical
delivery measurements. Nothing reads audio beyond "now"; the future-read guard stays on in
tests.

## Consequences

Invariants I-9 and I-19: the same consumed input trace, config, and seed produce the same
normalized decision hash.
