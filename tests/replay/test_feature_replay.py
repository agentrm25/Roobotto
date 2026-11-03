"""T-10 (P0) acceptance: block-by-block replay on the simulated clock behind the future-read
guard; onset envelope and beat-synchronous chroma from past audio only; replay equals a
simulated live run on the same blocks; input health; a missing feed is reported, never zeroed."""

import itertools
import json
from pathlib import Path

import numpy as np
import pytest

from roobotto.cli import main
from roobotto.config.loader import load_config
from roobotto.config.song import Song
from roobotto.engine.guard import FutureReadError, FutureReadGuard, GuardedSeries
from roobotto.eval import replay as rp
from roobotto.eval.replay import (
    FeatureRun,
    MissingFeedError,
    live_run,
    replay_take,
    take_blocks,
)
from roobotto.eval.synth import plan_take, render_take, write_take
from roobotto.features.chroma import ChromaExtractor
from roobotto.features.health import HealthMonitor
from roobotto.features.onset import OnsetExtractor

ROOT = Path(__file__).parents[2]
SONG = ROOT / "examples" / "songs" / "synthetic_anthem.yaml"
RATE = 8000
BLOCK = 256


def _song() -> Song:
    model, issues = load_config(SONG)
    assert not issues and isinstance(model, Song)
    return model


@pytest.fixture(scope="module")
def takes(tmp_path_factory: pytest.TempPathFactory) -> dict[str, Path]:
    root = tmp_path_factory.mktemp("takes")
    out: dict[str, Path] = {}
    song = _song()
    for variation in ("nominal", "repeat", "dropout"):
        take = render_take(plan_take(song, variation), sample_rate=RATE)
        out[variation] = write_take(take, root / variation)
    return out


# --- The guard ---


def test_feature_extractors_cannot_read_the_future() -> None:
    guard = FutureReadGuard()
    series: GuardedSeries[np.ndarray] = GuardedSeries(guard)
    for i in range(4):
        series.append(i * 1000, np.zeros(BLOCK))
    guard.advance(2000)
    assert len(series.read_until(2000)) == 3
    with pytest.raises(FutureReadError):
        series.read_until(3000)
    # The harness hands extractors only what the guard allows; a cheating read raises.
    run = FeatureRun(rate=RATE, block=BLOCK, feeds=["drums"])
    run.guard.advance(5 * 1000)
    with pytest.raises(FutureReadError):
        run.audio["drums"].read_until(6 * 1000)


def test_features_are_causal_on_the_prefix(takes: dict[str, Path]) -> None:
    """Features computed on the first N blocks equal those blocks' features in the full run."""
    full = replay_take(takes["nominal"], block=BLOCK, feeds=["drums", "chords"])
    cut_s = 12.0
    partial = replay_take(takes["nominal"], block=BLOCK, feeds=["drums", "chords"], until_s=cut_s)
    assert partial.records == [r for r in full.records if r["t_ns"] <= partial.records[-1]["t_ns"]]
    assert partial.output_hash != full.output_hash


def test_features_match_on_causal_twins_before_divergence(takes: dict[str, Path]) -> None:
    nominal = replay_take(takes["nominal"], block=BLOCK)
    other = replay_take(takes["repeat"], block=BLOCK)
    assert other.annotations.diverges_at_s is not None
    diverge_ns = round(other.annotations.diverges_at_s * 1e9)
    before_a = [r for r in nominal.records if r["t_ns"] < diverge_ns]
    before_b = [r for r in other.records if r["t_ns"] < diverge_ns]
    assert len(before_a) > 100 and before_a == before_b
    after_a = [r for r in nominal.records if r["t_ns"] >= diverge_ns][:400]
    after_b = [r for r in other.records if r["t_ns"] >= diverge_ns][:400]
    assert after_a != after_b


# --- Replay versus a simulated live run ---


def test_replay_equals_simulated_live_run(takes: dict[str, Path]) -> None:
    replayed = replay_take(takes["nominal"], block=BLOCK)
    blocks = list(take_blocks(rp.read_take(takes["nominal"]), block=BLOCK))
    live = live_run(blocks, rate=RATE, block=BLOCK, feeds=list(replayed.feeds), jitter_blocks=3)
    assert live.output_hash == replayed.output_hash
    assert live.records == replayed.records
    assert len(replayed.records) > 1000


def test_block_availability_is_capture_time(takes: dict[str, Path]) -> None:
    loaded = rp.read_take(takes["nominal"])
    blocks = list(take_blocks(loaded, block=BLOCK, feeds=["drums"]))
    first, second = blocks[0], blocks[1]
    assert first.feed == "drums" and first.index == 0
    assert first.available_ns == round(BLOCK / RATE * 1e9)  # when its last sample exists
    assert second.available_ns - first.available_ns == round(BLOCK / RATE * 1e9)
    assert len(first.samples) == BLOCK
    # Blocks from several feeds come out in availability order, feeds in a fixed order.
    mixed = list(take_blocks(loaded, block=BLOCK, feeds=["vocal", "drums"]))
    assert [b.feed for b in mixed[:2]] == ["drums", "vocal"]
    assert all(a.available_ns <= b.available_ns for a, b in itertools.pairwise(mixed))


# --- The features themselves ---


def test_onset_envelope_peaks_on_beats(takes: dict[str, Path]) -> None:
    run = replay_take(takes["nominal"], block=BLOCK, feeds=["drums"])
    onsets = [(r["t_ns"] / 1e9, r["onset"]) for r in run.records if r["type"] == "features"]
    beats = [b.at_s for b in run.annotations.beats]
    times = np.array([t for t, _ in onsets])
    values = np.array([v for _, v in onsets])
    near = np.zeros(len(times), dtype=bool)
    hop = BLOCK / RATE
    for b in beats:
        near |= (times >= b) & (times < b + 3 * hop)
    assert values[near].mean() > 3 * values[~near].mean()
    assert values.min() >= 0.0


def test_chroma_tracks_the_chord_tones(takes: dict[str, Path]) -> None:
    run = replay_take(takes["nominal"], block=BLOCK, feeds=["chords"])
    frames = [r for r in run.records if r["type"] == "features"]
    chroma = np.array([r["chroma"] for r in frames])
    assert chroma.shape[1] == 12
    assert np.all(chroma >= 0) and np.allclose(chroma.sum(axis=1)[chroma.sum(axis=1) > 0], 1.0)
    # Each frame's energy sits in at most a few pitch classes: a triad, not noise.
    loud = chroma[chroma.max(axis=1) > 0.2]
    assert len(loud) > len(frames) // 2
    assert (np.sort(loud, axis=1)[:, -3:].sum(axis=1) > 0.8).mean() > 0.9


def test_beat_synchronous_chroma_uses_only_past_beats(takes: dict[str, Path]) -> None:
    run = replay_take(takes["nominal"], block=BLOCK, feeds=["chords"], beats="oracle")
    beat_frames = [r for r in run.records if r["type"] == "beat_chroma"]
    assert beat_frames and all(r["source"] == "oracle" for r in beat_frames)
    beats = run.annotations.beats
    assert len(beat_frames) == len(beats) - 1  # one per completed beat interval
    for frame, beat in zip(beat_frames, beats[1:], strict=True):
        assert frame["t_ns"] == round(beat.at_s * 1e9)
        assert frame["emitted_ns"] >= frame["t_ns"]  # never before the beat has passed
        assert len(frame["chroma"]) == 12
    without = replay_take(takes["nominal"], block=BLOCK, feeds=["chords"])
    assert not [r for r in without.records if r["type"] == "beat_chroma"]


# --- Health and missing feeds ---


def test_health_reports_silence_and_recovery(takes: dict[str, Path]) -> None:
    run = replay_take(takes["dropout"], block=BLOCK, feeds=["drums", "bass", "chords", "vocal"])
    health = [r for r in run.records if r["type"] == "health"]
    drops = run.annotations.dropouts
    drums = next(d for d in drops if d.stream == "drums")
    changes = [(r["t_ns"] / 1e9, r["status"]) for r in health if r["feed"] == "drums"]
    assert changes[0][1] == "ok"
    degraded = [t for t, s in changes if s == "degraded"]
    assert degraded and drums.start_s <= degraded[0] <= drums.start_s + 2.0
    recovered = [t for t, s in changes if s == "ok" and t > degraded[0]]
    assert recovered and drums.end_s <= recovered[0] <= drums.end_s + 0.3
    # Silence is degraded, not lost: the blocks still arrived.
    assert not [r for r in health if r["feed"] == "drums" and r["status"] == "lost"]


def test_missing_feed_is_reported_not_zeroed(takes: dict[str, Path]) -> None:
    with pytest.raises(MissingFeedError, match="room"):
        replay_take(takes["nominal"], block=BLOCK, feeds=["drums", "room"])
    # A feed that stops mid-take: health goes lost, no feature frames are made up, then ok again.
    run = replay_take(
        takes["nominal"], block=BLOCK, feeds=["drums", "bass"], drop={"bass": [(10.0, 14.0)]}
    )
    bass = [r for r in run.records if r["feed"] == "bass"]
    lost = [r for r in bass if r["type"] == "health" and r["status"] == "lost"]
    assert len(lost) == 1 and 10.0 <= lost[0]["t_ns"] / 1e9 <= 10.5
    frames_in_gap = [r for r in bass if r["type"] == "features" and 10.0 < r["t_ns"] / 1e9 < 14.0]
    assert frames_in_gap == []
    back = [
        r for r in bass if r["type"] == "health" and r["status"] == "ok" and r["t_ns"] / 1e9 > 14.0
    ]
    assert back
    assert run.health_summary["bass"]["lost_s"] == pytest.approx(4.0, abs=0.2)
    assert run.health_summary["drums"]["lost_s"] == 0.0
    # The drums feed was unaffected: its records equal the clean run's.
    clean = replay_take(takes["nominal"], block=BLOCK, feeds=["drums", "bass"])
    assert [r for r in run.records if r["feed"] == "drums"] == [
        r for r in clean.records if r["feed"] == "drums"
    ]


def test_extractors_are_streaming_and_deterministic() -> None:
    rate = RATE
    t = np.arange(rate * 2) / rate
    signal = 0.5 * np.sin(2 * np.pi * 220.0 * t) * (t > 1.0)
    a = OnsetExtractor(rate, BLOCK)
    b = OnsetExtractor(rate, BLOCK)
    out_a = [a.push(signal[i : i + BLOCK]) for i in range(0, len(signal), BLOCK)]
    out_b = [b.push(signal[i : i + BLOCK]) for i in range(0, len(signal), BLOCK)]
    assert out_a == out_b
    onset_index = int(np.argmax(out_a))
    assert abs(onset_index * BLOCK / rate - 1.0) < 2 * BLOCK / rate
    c = ChromaExtractor(rate, BLOCK)
    chroma = [c.push(signal[i : i + BLOCK]) for i in range(0, len(signal), BLOCK)]
    assert int(np.argmax(chroma[-1])) == 9  # A
    h = HealthMonitor(rate, BLOCK)
    assert h.push(np.zeros(BLOCK)) == "degraded" or h.push(np.zeros(BLOCK)) in ("ok", "degraded")


def test_no_whole_recording_normalization_or_centered_windows() -> None:
    for path in (ROOT / "src" / "roobotto" / "features").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "center=True" not in text and "centre" not in text, path.name
        assert "random" not in text, path.name


def test_cli_replay_features(
    takes: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "features.jsonl"
    assert (
        main(
            [
                "replay",
                "features",
                str(takes["nominal"]),
                "--block",
                str(BLOCK),
                "--feeds",
                "drums,chords",
                "--beats",
                "oracle",
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "output hash" in text and "drums" in text
    lines = out.read_text(encoding="utf-8").splitlines()
    first = json.loads(lines[0])
    assert first["type"] in ("health", "features")
    assert any(json.loads(line)["type"] == "beat_chroma" for line in lines)
    assert main(["replay", "features", str(takes["nominal"]), "--feeds", "drums,room"]) == 1
    assert "room" in capsys.readouterr().err
    assert main(["replay", "features", str(tmp_path / "nope")]) == 1
