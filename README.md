# Roobotto

Roobotto is a planned open-source stage manager for live bands. It aims to follow
live audio and trigger preprogrammed MIDI cues while the band plays naturally,
without requiring a click track or backing track.

## Planned capabilities

- Follow song position and section changes while allowing for tempo variation.
- Coordinate Helix snapshots, lighting scenes, and video cues through MIDI.
- Support a single next-cue footswitch and manual override.
- Run audio following and cue control locally.

## Status

This repository contains the project foundation only. Audio following,
MIDI control, and hardware integrations are not implemented yet. Physical outputs
are off by default and nothing here talks to real hardware.

The stack is Python 3.12 with uv (ADR-002). The code is licensed Apache-2.0 (ADR-001).

## Planning

The [LLM Council report](Conceiving_the_Roobotto_Project-2026-10-16.html) captures
the initial research and design proposals. The [implementation plan](docs/implementation-plan.md)
turns them into tasks, and accepted decisions are in [docs/adr/](docs/adr/).

## Development

```
uv sync
uv run ruff check
uv run ruff format --check
uv run pyright
uv run pytest
```

See [CONTRIBUTING.md](CONTRIBUTING.md) and [AGENTS.md](AGENTS.md).

## Local files

Keep credentials in ignored environment files. Store rehearsal recordings in
`recordings/` and private show data in `local/`; these folders are ignored by Git.
Only add audio or other media that you have permission to share.
