# ADR-007: MIDI channel numbering

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3

## Context

MIDI channels are 1–16 to people and 0–15 on the wire. Mixing the two is a classic
off-by-one error.

## Decision

Channels are 1–16 in config and logs, converted to 0–15 in exactly one adapter function in
`adapters/midi_wire.py` (invariant I-10).

## Consequences

No other code converts channels. Tests cover the conversion's boundaries.
