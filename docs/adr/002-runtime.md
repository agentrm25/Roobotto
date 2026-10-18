# ADR-002: Runtime

- **Status:** Accepted
- **Date:** 2026-10-17
- **Deciders:** Randy Mitchell
- **Plan reference:** Sections 2 (item 6) and 3

## Context

Current numpy, scipy, and librosa require Python 3.12 or newer. Matchmaker 0.3.0 requires
Python older than 3.13. All of this comes from PyPI metadata, 16 October 2026.

## Decision

Python 3.12, managed with uv. CI runs on macOS and Linux. Hot paths are ported to Rust or
C++ only if measurements demand it.

## Consequences

`pyproject.toml` pins `requires-python = ">=3.12,<3.13"`. CI uses `macos-latest` and
`ubuntu-latest`.
