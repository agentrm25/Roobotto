# ADR-011: Input authority and provenance

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 5.4 (audit finding F-8)

## Context

A snapshot observation from the Helix is not automatically a GO or an authorization. The
Helix also sends CC when automation, preset loads, or echoes change a snapshot.

## Decision

Only performer-intent events authorize GO, BACK, or critical cues, with one authoritative
path per source. Mapped Helix snapshot transitions count as GO. These never do:

- the snapshot a preset recalls on load
- recalls made by automation
- echoes of Roobotto's own output
- duplicates
- anything of unknown origin

Authorization is tied to one specific cue occurrence. Fail closed.

## Consequences

Invariants I-8, I-13, and I-16 cover this. A source with two authoritative paths is rejected
by config validation.
