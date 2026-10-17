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
  share a snapshot.
- **Cues** have a unique `id`, a position (`section`, `bar`, `beat`, optional `offset_beats`,
  negative to anticipate), a `class`, and `state`: intents such as `mvp.scene: chorus`, never
  bytes. `on_miss` is `skip`, `send_now` (default) or `{fire_late_within_beats: N}`.
- **Classes:** `decorative`, `standard`, `critical`. There is no `excluded` class: hazardous
  effects are never automated.
- **Critical cues** must list `footswitch` or `mapped_snapshot` in `requires.corroborate_by`.
  Tracker evidence alone never fires a critical cue (ADR-013).
- **`lyric_anchor`** needs `vocal: true` and may appear only in files under `local/` (ADR-008).

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
