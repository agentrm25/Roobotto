# ADR-012: Cue occurrence identity

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 5.5 (audit finding F-7)

## Context

Cue identity has to survive scheduler generation changes, relocalization, reconnects, and
restarts, or one-shots can fire twice.

## Decision

A cue occurrence is identified by (performance, song instance, section visit, cue id). The
scheduler generation only invalidates pending work. A persisted fired ledger makes each
one-shot fire at most once across cancels, relocalization, reconnects, and restarts.

## Consequences

Invariants I-5, I-6, and I-7 cover this. Restart builds no past work. Fired ledgers are
written under the ignored `logs/` directory.
