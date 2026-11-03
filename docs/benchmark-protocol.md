# Benchmark protocol

> Section 9 of `implementation-plan.md` (the decisive experiment) is the authority for capture,
> ground truth, methods, metrics, adversarial tests and gates. This file says what the tooling
> built so far does and how to run it.

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

## The benchmark command (T-13)

`roobotto benchmark <protocol.yaml> [--out folder]` runs every method in the protocol over the
corpus and writes, into the protocol's `out` folder: `results.csv` (one row per method, feed
subset, take and fault injection, with every score field, `streams: as-captured` and
`idealized: false`), `report.md`, three SVG plots (accuracy by method, boundary timing,
production-equivalent coverage), `summary.json` (commit, config hash, manifest hashes, reduced
scope) and `timing.csv` (wall-clock per run, kept apart from the results because it measures the
machine, not the method).

A protocol (`experiments/protocols/*.yaml`, schema `protocol`) names the corpus root (take
folders `<song>/<take>/`, optionally rendered from song files when missing), which take is the
reference per song, the block size, the methods, the feed subsets per method and the fault
injections (blocks of a feed withheld for an interval). Audio never goes in git: the corpus root
lives under `recordings/`.

The report has: provenance (commit, config hash over the protocol and song files, manifest
hashes); the method x feed-subset matrix and a per-take table; fault-injection deltas;
assistance accounting (every method here is told one boundary, the song start, by annotation;
B5 and B6 are not built); failure examples; a reduced-scope section listing every deferred
method, feature and ablation; and the streams rule. A corpus with any synthetic take is labeled
SYNTHETIC on the first line: synthetic results never count as evidence that following is
feasible. The pilot protocol is written when WP5 delivers the takes, with `label: pilot`.

Plots are SVG written by the tool itself; no plotting library is a dependency yet. Reports are
byte-for-byte reproducible on one machine (`timing.csv` aside).

## Dry-run demo (T-14)

`roobotto run --dry-run --replay <take> [--reference <take>] [--milestone N] [--feeds a,b]
[--log decisions.jsonl] <config paths>`: the bar-grid follower listens to the take and its
`MusicalState` stream drives the real engine (mock profile, unarmed, MIDI adapter disabled).
Every decision is printed and, with `--log`, written as JSONL with the run id, config hash and
clock time. `experiments/reports/dry-run-demo/` holds the transcript and log from
`synthetic_anthem/nominal` at milestone 5, where standard cues may fire on tracker evidence:
the MVP scene change lands within a beat of the annotated chorus downbeat and goes to the mock
adapter; the critical cue is refused (I-14). A run on a pilot take is reviewed locally and never
committed.

## Capture (T-07)

`roobotto record session <session.yaml> --out <take> [--seconds N]` captures from the devices
in the session file to one FLAC per mapped channel (48 kHz, 24-bit by default), writes
`blocks.jsonl` (per block: device, block and sample index, ADC time, callback time, overflow and
underflow flags, receive time), `midi.jsonl` (every message on every input port, listen only),
`capture.json` (the operator-entered clock configuration, sync-burst marks, frame counts) and
`manifest.json` (song, take type, channel map, devices, sample rate, SHA-256 hashes, consent).
A rehearsal take needs `consent: given`. The audio callback only copies, stamps and enqueues.

`roobotto record validate <take> [--reference stream] [--tolerance-ms 1]` finds the shared
marker clicks in every stream, measures each stream's offset against the reference at every
marker, fits a linear drift, reports offset, drift (ppm), the largest residual after that model
and the largest raw offset, and marks invalid intervals: dropped blocks (the driver's ADC clock
moved further than the samples it delivered), flagged blocks, and markers whose residual breaks
tolerance. Intervals the validator flags are excluded from analysis and reported.

`roobotto record correct <take>` writes drift-corrected copies under `corrected/` with their own
manifest marked idealized. Follower results are reported on as-captured streams only; a result
on corrected streams is labeled idealized and needs a separate check against live capture.
TO-T-07 is the 10-minute bench run that decides whether the rehearsal topology is good enough.
