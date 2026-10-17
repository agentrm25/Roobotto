# Third-party software

Every dependency is recorded here with its license before it is added. Ask the owner before
adding one (AGENTS.md). No GPL or AGPL code in core. Non-commercial models only under
`research/`. Versions are pinned by `uv.lock`.

Versions and licenses below come from PyPI metadata; confirm against `uv.lock` when added.

## Core runtime

| Package | Version | License | Used for |
|---|---|---|---|
| pydantic (with annotated-types, pydantic-core, typing-inspection) | 2.14.1 | MIT | Config and message schemas, JSON Schema export (ADR-004) |
| PyYAML | 6.0.3 | MIT | Reading song, rig and setlist files (ADR-004) |

The remaining runtime dependencies (numpy, scipy, librosa, soundfile, sounddevice, mido,
python-rtmidi, python-osc, rich, textual, matplotlib, mir_eval) are added by the task cards
that first need them, per `docs/implementation-plan.md` Section 11.

## Development tools

| Package | License | Used for |
|---|---|---|
| pytest | MIT | Test runner |
| hypothesis | MPL-2.0 | Property tests (tests only, never shipped) |
| ruff | MIT | Lint and format |
| pyright | MIT | Type checking |

## Bench tools (not dependencies)

| Tool | License | Note |
|---|---|---|
| MIDI Monitor | Free app | First-time verification only, used outside Roobotto |
| SendMIDI | GPL-3.0 | First-time verification only, used outside Roobotto |
