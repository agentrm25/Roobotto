# ADR-001: License

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3

## Context

The council recommended Apache-2.0, which includes a patent grant. Several useful tools are
GPL or AGPL (BTrack, aubio, Chordino, Essentia).

## Decision

Roobotto's own code is licensed Apache-2.0. GPL tools are used only as separate processes or
as optional extras. Essentia (AGPL) is avoided. No GPL/AGPL code in core. Non-commercial
models live only under `research/`.

## Consequences

`LICENSE` and `NOTICE` carry the terms. Every dependency's license is recorded in
`THIRD_PARTY.md` before it is added.
