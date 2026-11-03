# Dry-run demo transcript (T-14)

`transcript.txt` is the standard output and `decisions.jsonl` the JSONL decision log of the
command on its first line: the bar-grid follower listens to the synthetic take
`synthetic_anthem/nominal` (rendered at 48 kHz by `roobotto synth render`) and its
`MusicalState` stream drives the real engine in the mock profile, unarmed, at milestone 5 so
standard cues may fire on tracker evidence. Every send goes to the mock adapter. The critical
cue is refused on tracker evidence alone (I-14). Synthetic take, no private data, no audio.

Regenerate from a clean checkout:

```
uv run roobotto synth render examples/songs/synthetic_anthem.yaml --out recordings/synthetic
uv run roobotto run --dry-run --replay recordings/synthetic/synthetic_anthem/nominal --milestone 5 \
  --log experiments/reports/dry-run-demo/decisions.jsonl examples
```
