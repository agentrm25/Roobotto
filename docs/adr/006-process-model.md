# ADR-006: Process model

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3, Section 5.2

## Context

A tracker crash must not take down manual control. Only the latest tracker value matters, so
a lossy transport is acceptable.

## Decision

The tracker and the cue engine run as separate processes. The tracker publishes
`MusicalState` as localhost UDP datagrams (JSON or msgpack), each carrying a tracker session
id and a sequence number. The engine rejects packets from an old session or out of order. A
stale stream or a new tracker session suspends tracker authority and drops the engine to
MANUAL. Only the operator can re-enable it.

## Consequences

Reconnecting never silently restores authority (I-8). The same schema later becomes the
public OSC/WebSocket state protocol.
