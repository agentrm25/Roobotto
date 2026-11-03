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

## Baselines B0 and B1 (T-11)

`roobotto replay baseline <take> --method B0|B1 [--feed drums] [--out score.json]` runs a
baseline on a take and scores it against the annotations.

- **B0, fixed timeline:** from the annotated song start, at the song's nominal tempo, count
  beats along the nominal path. What a click track would give you.
- **B1, bar counting:** the same count, but beats come from the onset PLL on the drum feed
  (E-36), limited to the song's tempo range. No content observation.

Both are anchored at the annotated song start, as the plan allows. Scores (`roobotto.eval.score`):
section and visit accuracy, bar accuracy and production-equivalent accuracy per annotated beat
(the estimate in force at mid-beat); mean tempo error ratio and octave errors; signed boundary
timing error (median and p95 of the first entry into each annotated visit), with deviations
counted as unexpected boundaries; accuracy before and after the first deviation; and recovery
in bars, when it happens. On the synthetic corpus B0 fails under drift and B1 fails on skips and
repeats (E-35); both still have to run on the pilot takes before the T-11 P0 check is complete.
B2 (online DTW) is P1 and not built.

## Bar-grid follower v0 (T-12)

`roobotto replay follow <take> --reference <nominal take>... [--feeds chords,bass] [--temperature t]
[--propose <config paths>] [--out follow.json]`.

The follower (`roobotto.tracker.follower`) is an exact forward filter over every beat position of
the song's bar grid times five tempo bins, with phase from the onset PLL on the drum feed. Per
beat it predicts along the grid (next beat; at a bar's end the song's successors: next section,
extension, repeat, graph jumps) plus a small jump-anywhere mass, forward more than back, which is
how unmodeled deviations are recovered; the grid's bar counts and the song's extend and repeat
probabilities are the duration prior. It then updates on the beat-synchronous chroma of each
listening feed against the reference take's chroma at that position (the chroma that arrives
with beat k describes beat k-1, so a state is matched against its predecessor's reference), with
a temperature calibrated on development takes (`calibrate_temperature`). It emits a
`MusicalState` per block with a session id: top hypotheses, beat phase and tempo, the next
downbeat, a boundary forecast with successor probabilities when the top position ends a section,
and feed health. Results use the take's as-captured streams only.

`--propose` routes the states through the real engine (mock profile, unarmed, milestone 4,
ASSISTED, tracker authority on, one GO at the annotated song start): proposals meet the real
scheduler, decision gate and output supervisor. Nothing can reach hardware; the summary reports
`fired_physical` so a reader can check it is 0. See E-37 for the synthetic results and E-38 for
why section-entry cues expire on tracker evidence while bar-counted cues meet the gate.
