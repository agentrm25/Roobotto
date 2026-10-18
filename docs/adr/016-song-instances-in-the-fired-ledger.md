# ADR-016: Song instances in the fired ledger

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 4 (fired ledger record), Section 5.5; extends ADR-012

## Context

An occurrence id includes the song instance. T-04 numbered song instances after the highest one
in the fired ledger. A song selected before a restart, with nothing fired, left no record, so its
instance number was reused after the restart. At-most-once still held, because nothing with that
id had been sent. But two different occurrences could share an id in the logs.

## Decision

- The fired ledger file holds two kinds of record, told apart by a `record` field.
  - `fired`: the existing fired-occurrence record. Lines without the field are read as `fired`.
  - `song_instance`: performance, song instance, song and start time.
- When a song instance starts, the spine appends a `song_instance` record and flushes it before
  any occurrence of that instance exists.
- On start, the next song instance is one past the highest instance in either kind of record.
- Only `fired` records block sends.

## Consequences

- No occurrence id is ever reused across restarts, fired or not.
- The fired-ledger record schema gains the `record` field. A new `song_instance_record` schema
  is exported.
- Traces record the starting ledger with both kinds, so replay stays exact (I-19).
- I-6 is now checked across restarts in `tests/property/test_invariants.py`.
