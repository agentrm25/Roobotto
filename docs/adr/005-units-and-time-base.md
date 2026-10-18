# ADR-005: Units and time base

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 4.1

## Context

Song structure is naturally measured in bars and beats, while device behavior is measured in
physical time. Mixing the two, or reading several clocks, causes timing bugs.

## Decision

- Song lengths (sections, bars) are in bars and beats.
- Physical intervals (device latency, debounce, gesture thresholds, timeouts) are in explicit
  time units: milliseconds in config, nanoseconds at runtime.
- All runtime time comes from the clock interface in ADR-014: `time.monotonic_ns()` live, a
  simulated clock in replay.
- Audio blocks are mapped from PortAudio's input time to that clock.

## Consequences

One clock, and every quantity in the right kind of unit. No other wall-clock reads in the
engine core.
