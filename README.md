# OpenStage

OpenStage is a planned open-source stage manager for live bands. It aims to follow
live audio and trigger preprogrammed MIDI cues while the band plays naturally,
without requiring a click track or backing track.

## Planned capabilities

- Follow song position and section changes while allowing for tempo variation.
- Coordinate Helix snapshots, lighting scenes, and video cues through MIDI.
- Support a single next-cue footswitch and manual override.
- Run audio following and cue control locally.

## Status

This repository contains the initial project scaffold only. Audio following,
MIDI control, and hardware integrations are not implemented yet. The application
stack and open-source license have not been selected.

## Local files

Keep credentials in ignored environment files. Store rehearsal recordings in
`recordings/` and private show data in `local/`; these folders are ignored by Git.
Only add audio or other media that you have permission to share.
