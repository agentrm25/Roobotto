# Third-party software

Every dependency is recorded here with its license before it is added. Ask the owner before
adding one (AGENTS.md). No GPL or AGPL code in core. Non-commercial models only under
`research/`. Versions are pinned by `uv.lock`.

Versions and licenses below come from PyPI metadata; confirm against `uv.lock` when added.

## Core runtime

None yet. Runtime dependencies (numpy, scipy, librosa, soundfile, sounddevice, mido,
python-rtmidi, python-osc, pydantic, PyYAML, rich, textual, matplotlib, mir_eval) are added
by the task cards that first need them, per `docs/implementation-plan.md` Section 11.

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
