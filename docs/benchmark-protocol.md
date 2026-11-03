# Benchmark protocol

> Stub. To be filled from `implementation-plan.md` Section 9 (the decisive experiment): capture specification, ground truth, methods, metrics, adversarial tests, and gates.

Gates are proposed targets, not current capability. Synthetic results never count as
evidence that following is feasible.

## Synthetic corpus (T-08)

`roobotto synth render <song.yaml> --out recordings/synthetic` renders each song to four feeds
(drums, bass, chords, vocal) as 48 kHz 24-bit FLAC, plus `annotations.json` (exact ground truth:
section visits, bar lines, beats with tempo, intended cue instants, deviation points, dropouts,
sync bursts) and `manifest.json` (files and SHA-256 hashes). Audio is never committed; the corpus
is regenerated from the song files, deterministically on one machine (E-31).

Take variations per song: `nominal`, `drift` (a tempo ramp within the song's range), `repeat`,
`skip`, `extend` (a section played longer), and `dropout` (one feed silent at a time). Every
deviation take is a **causal twin** of the nominal take: sample-identical up to
`diverges_at_s`, the first instant the band's choice is audible, and different after it. A
follower scored on these must abstain before that instant; any success earlier than that is
evidence of future leakage. The example songs `synthetic_anthem` (4/4), `synthetic_waltz` (3/4)
and `synthetic_seven` (7/8) are the three P0 songs.

Synthetic results never count as evidence that following is feasible (Section 9 of the plan).

## Causal replay and features (T-10)

`roobotto replay features <take> [--block 512] [--feeds a,b] [--beats none|oracle] [--drop feed:s:e]`
replays a take block by block on the simulated clock. A block becomes available when its last
sample was captured; the future-read guard moves to that instant before any extractor sees it,
and every read goes through a guarded series that raises on a read past "now" (I-9).

Features per block and feed, from the trailing window only: the onset envelope (spectral flux,
E-32) and 12-bin chroma (E-33). With `--beats oracle`, beat-synchronous chroma is averaged over
each beat interval and emitted only once the beat has passed, labeled `source: oracle` because
the beat times come from the annotations, not from a follower. Input health per feed is `ok`,
`degraded` (silence over 1.5 s, or clipping) or `lost` (blocks stopped arriving); a lost feed gets
no made-up frames, and a feed absent from the take is an error, never silence.

Leakage controls checked by tests: the first N blocks give the same records whether or not the
rest of the take exists; causal twins give identical records before `diverges_at_s`; replay and a
simulated live run over the same blocks give the same output hash (E-34).
