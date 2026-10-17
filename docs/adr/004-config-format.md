# ADR-004: Config format

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Section 3

## Context

Songs, rigs, and setlists are written and edited by people. Components need a shared,
machine-checkable contract.

## Decision

YAML written by people, validated by pydantic models in `src/roobotto/config`, with JSON
Schema exported to `schemas/` by `uv run roobotto schemas export`.

## Consequences

The schemas are the contract between components. Changing a schema needs the owner's
approval (AGENTS.md).
