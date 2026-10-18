# ADR-008: Data policy

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3

## Context

Recordings and lyrics may be copyrighted or private. The README and `.gitignore` already
separate shareable from private data.

## Decision

- Audio never goes in git.
- Rehearsal audio goes in `recordings/` and bench captures in `captures/`, both ignored.
- `corpus/` holds take manifests and annotations and is committed.
- Private show data goes in `local/`, which is ignored. A separate private repo cloned there
  is recommended. Lyric anchors live only in `local/`.
- Demo evidence is a text transcript from a synthetic or non-private replay.

## Consequences

CI runs `scripts/check_forbidden_paths.py`, which rejects audio files and anything tracked
under `local/`, `recordings/`, or `captures/`.
