# ADR-015: Input capability records

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 4 (event adapters), Section 5.4; extends ADR-011

## Context

Section 4 says each input message is interpreted through a verified capability record. The rig
schema v0 had capability records for commands only, so T-04 read Helix snapshots as a fixed
CC69 from engine settings (E-01), on any channel, and found the Helix by its device `kind`.
That bypassed the verification status that governs every output (ADR-009).

## Decision

- A device may declare `reports`: the messages it sends to Roobotto and what they mean.
  - `snapshot`: a control change on one input source and channel, whose `values` list the data
    value for snapshot 1, 2, 3 and so on.
  - `preset`: a Program Change sent on preset change (song selection).
- Each report has a status (`unknown`, `documented`, `bench_verified`) with the same evidence
  rules as commands.
- Input from a source is interpreted only through its report records. A message that matches no
  switch and no report has unknown provenance and fails closed.
- A mapped snapshot transition is performer intent only when its report is `bench_verified`, or
  the message arrived on a mock port. Otherwise it is a state observation.
- A message may mean only one thing: no two reports share source, channel, kind and number.
- A song that uses `advance_on` needs exactly one snapshot report in the rig, and every mapped
  snapshot must be one that report lists.
- Reports of any status are allowed in a physical profile, because observing state reaches no
  hardware.

## Consequences

- Snapshot-mapped GO on real hardware needs bench test H-1 to mark the snapshot report
  `bench_verified`. Until then the Helix's snapshots are tracker evidence only.
- The engine settings `helix_source` and `snapshot_cc` are removed. The rig's input records
  replace them.
- The rig and device-capability JSON Schemas change (`roobotto schemas export`).
- Invariant I-16 is unchanged. Its rules now read device input through these records.
