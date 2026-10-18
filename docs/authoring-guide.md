# Authoring guide

How to write the three config files Roobotto reads. Each is YAML with a `kind` and
`schema_version: 0`. Check any of them with:

```
uv run roobotto validate-config path/to/files-or-dirs
```

Give it a song, a rig and a setlist together to also check cues against the rig and setlist
songs against the song files. Examples live in `examples/` (synthetic, no private data).
The JSON Schemas in `schemas/` are generated from the models by
`uv run roobotto schemas export`.

## Song (`kind: song`)

See [examples/songs/synthetic_anthem.yaml](../examples/songs/synthetic_anthem.yaml).

- **Sections** have an `id` and a bar count. The last section must set `ends: true`.
- **`model_as`** shares reference features with another section (verse 2 as verse 1). It does
  not claim the sections sound different.
- **`extendable` / `repeat`** describe loops. A self-loop edge in `graph` needs one of them.
  `graph` edges are the non-sequential transitions; the leftover probability goes to the
  next section.
- **`advance_on: {snapshot: N}`** means the Helix snapshot change (1–8) that advances the band
  *into* this section. Only these mapped changes count as performer intent (ADR-011). The
  first section cannot have one, and two sections that can follow the same section cannot
  share a snapshot. The rig must have exactly one device with a `snapshot` report that lists
  snapshot N, and on real hardware that report must be `bench_verified` before a snapshot can
  count as a GO (ADR-015). A snapshot GO never sends that device's own cues: the player has
  already changed it.
- **Cues** have a unique `id`, a position (`section`, `bar`, `beat`, optional `offset_beats`,
  negative to anticipate), a `class`, and `state`: intents such as `mvp.scene: chorus`, never
  bytes. `on_miss` is `skip`, `send_now` (default) or `{fire_late_within_beats: N}`.
- **Classes:** `decorative`, `standard`, `critical`. There is no `excluded` class: hazardous
  effects are never automated.
- **Critical cues** must list `footswitch` or `mapped_snapshot` in `requires.corroborate_by`.
  Tracker evidence alone never fires a critical cue (ADR-013).
- **`lyric_anchor`** needs `vocal: true` and may appear only in files under `local/` (ADR-008).

### How a song is expanded (bar grid)

The tracker and cue engine do not read the YAML directly. They use the bar grid built by
`roobotto.arrangement.expand`, one cell per `(section, visit, bar)`. The rules a song author
needs to know:

- **Visits are counted per section.** A repeated `chorus3` has visits 1, 2 and 3; `verse2` is
  its own section, so its first pass is visit 1. A new visit creates new cue occurrences.
- **At the end of a section** the band picks one outcome: extend (`extendable`, same visit,
  `unit` more bars), repeat (`repeat`, new visit, at most `max` extra times), jump along a
  `graph` edge, or go to the next section (or end the song, for an `ends` section). The next
  section gets whatever probability the others leave.
- **A self-loop edge and `extendable`/`repeat` are the same event written twice**, so their
  `prob` must match. A section that is both `extendable` and `repeat` cannot have a self-loop
  edge; leave the edge out and give each its own `prob`.
- **Graph edges must point forward** (to a later section). A section that was already played
  cannot be jumped back to in v0, because that would make the visit number depend on how the
  band got there. Use `repeat` for a repeated section.
- **Extension has no declared limit**, so expansion allows 2 extra blocks by default
  (`max_extensions`). A cell where that cap removed an outcome is listed in
  `BarGrid.truncated`.
- These rules are checked when a song is expanded, not by `validate-config` (yet), and an
  expansion with more than 10,000 possible paths is refused.

### Production-equivalent positions

Two beat positions are *production-equivalent* when the rig would be in the same state and the
same cues would be coming up (within a horizon you choose, in beats). Verse 1 and verse 2 are
equivalent once the same look is on and the next cues match. Until then, telling them apart does
not change what the rig has to do. This is computed by
`roobotto.cues.equivalence.production_classes` from the cue sheet:

- A cue counts by **what it asks for** (class, `state`, `on_miss`, `requires`,
  `on_uncertain`), not by its id or the section it sits under.
- **Active state** is the last value each intent was set to by a cue already due. A position
  reached by different routes can have several possible states; all of them have to match.
- A cue with a negative `offset_beats` is part of the future of every route that goes on to
  enter its section, and of no other route. A cue due before the performance starts never fires.
- A longer horizon can only split classes, never merge them. Adding a cue for a new device can
  only split classes too.

## Rig (`kind: rig`)

See [examples/rig.template.yaml](../examples/rig.template.yaml).

- **`profile: mock`** may only use `mock` ports. **`profile: physical`** may use real ports,
  but every command on one must be `bench_verified` with `evidence.bench_log` under
  `captures/` (ADR-009). There is no override, and arming is never in the file.
- **Command status:** `unknown` (wire messages may be `null`), `documented` (cite an
  evidence-register id in `evidence.source`), `bench_verified` (cite the bench log).
- **`receives`** lists where a device listens, each with a required `behavior`: `intended`,
  `ignores`, `acts` or `unknown`. A command's port and channel must reach only its own device;
  any other listener must be `ignores`, or be filtered onto another port (I-13).
- **`owner`** is `roobotto` or `performer`. Cues can only target devices Roobotto owns.
- **`inputs`** need exactly one `authoritative` path per source (ADR-011).
- **`controls`** follow ADR-010: GO, EXTEND and STOP are `press`, BACK is `tap`, MANUAL is
  `hold`. GO and STOP each need their own switch, and no switch may combine a forward action
  with a backward or safety action (I-17). A STOP and a GO control are required.
- **`timing`** is in milliseconds; defaults are the plan's starting values.
- Channels are 1–16 everywhere in config.

## Setlist (`kind: setlist`)

See [examples/setlist.yaml](../examples/setlist.yaml). Entries name a song and the Helix
`program_change` that selects it (omit it to use the HUD's "next song"). Two entries cannot
share a program change.

## Decision for T-04

A cue's `state` may name several devices, but a cue is one occurrence and the fired ledger is
keyed by occurrence alone (ADR-012). T-04 must decide whether the ledger also needs the
device-intent as part of its key.

Private show data (songs, lyric anchors, rig) stays in `local/`, which is ignored by git.
Examples committed under `examples/` use synthetic songs and a rig template with no mappings
enabled.
