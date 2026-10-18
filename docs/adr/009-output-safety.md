# ADR-009: Output safety

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 10.2 (audit finding F-11)

## Context

Council principle 3: reliability over cleverness. A wrong message to live gear can change
the sound or the show.

## Decision

Physical outputs are off by default. Arming is explicit for each run and never carries over a
restart (I-2). The runtime has no override for unverified commands (I-1). Randy verifies each
command for the first time outside Roobotto (Section 10.2), and the result is logged in
`docs/hardware-verification.md` with a bench log in `captures/`.

## Consequences

Code, tests, and examples use the mock adapter. Every hardware command references a
capability record marked `bench_verified`.
