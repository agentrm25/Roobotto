# ADR-003: Environments

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Sections 2 (items 6–7) and 3, Section 11

## Context

Non-commercial models and old version pins must stay out of the product. Matchmaker needs
numpy older than 2. BeatNet pins numba 0.54.1, which supports Python 3.7–3.9 and lists Apple
Silicon support as unofficial. madmom's models are CC BY-NC-SA.

## Decision

- `core`: runtime and evaluation.
- `offline` extra: Beat This! and PyTorch, for reference beat grids.
- `research-a`: Matchmaker, numpy older than 2.
- `research-b`: madmom from git plus BeatNet, Python 3.9.

The research environments are separate tools that exchange plain CSV or JSON files with the
core. Each gets at most half a day of setup.

## Consequences

`research-b` may need an Intel Python under Rosetta or a Linux machine. If a comparison is
deferred, name the validation lost and carry its check to the milestone that needs it: the
beat-tracker comparison to M1, the DTW cross-check to the full study. A deferred check is
never reported as satisfied.
