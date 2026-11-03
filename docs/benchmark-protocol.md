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
