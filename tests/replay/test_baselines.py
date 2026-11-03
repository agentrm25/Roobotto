"""T-11 (P0) acceptance: B0 (fixed timeline) and B1 (bar counting on the onset PLL) run on the
synthetic corpus; B0 fails under tempo drift and B1 fails on skips."""

import json
from pathlib import Path

import numpy as np
import pytest

from roobotto.cli import main
from roobotto.config.loader import load_config
from roobotto.config.song import Song
from roobotto.eval.score import Score, score_estimates
from roobotto.eval.synth import plan_take, render_take, write_take
from roobotto.tracker.baselines import METHODS, run_baseline
from roobotto.tracker.pll import OnsetPLL

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
    song = _song()
    return {
        v: write_take(render_take(plan_take(song, v), sample_rate=RATE), root / v)
        for v in ("nominal", "drift", "skip", "repeat")
    }


@pytest.fixture(scope="module")
def scores(takes: dict[str, Path]) -> dict[tuple[str, str], Score]:
    out: dict[tuple[str, str], Score] = {}
    for method in METHODS:
        for variation, folder in takes.items():
            result = run_baseline(method, folder, block=BLOCK)
            out[(method, variation)] = result.score
    return out


def test_both_baselines_follow_the_nominal_take(scores: dict[tuple[str, str], Score]) -> None:
    for method in METHODS:
        s = scores[(method, "nominal")]
        assert s.beats_scored > 200
        assert s.section_visit_accuracy >= 0.97, (method, s)
        assert s.production_equivalent_accuracy >= s.section_visit_accuracy
        assert abs(s.tempo_error_ratio) < 0.03, (method, s)


def test_b0_fails_under_tempo_drift(scores: dict[tuple[str, str], Score]) -> None:
    b0, b1 = scores[("B0", "drift")], scores[("B1", "drift")]
    # B0 drifts about two bars by the end: bars are mostly wrong, boundaries land seconds late.
    assert b0.bar_accuracy < 0.5, b0
    assert b0.boundary_error_p95_s > 1.0, b0
    assert b0.tempo_error_ratio > 0.03  # it never learns the tempo
    assert b1.bar_accuracy >= 0.95, b1
    assert b1.boundary_error_p95_s < 0.5, b1
    assert b1.tempo_error_ratio < 0.03
    assert b1.section_visit_accuracy - b0.section_visit_accuracy > 0.05


def test_b1_fails_on_skips(scores: dict[tuple[str, str], Score]) -> None:
    b1 = scores[("B1", "skip")]
    assert b1.accuracy_before_deviation is not None and b1.accuracy_after_deviation is not None
    assert b1.accuracy_before_deviation >= 0.95, b1
    assert b1.accuracy_after_deviation < 0.3, b1
    assert b1.section_visit_accuracy < 0.75
    # Pure counting has no way to notice the skip: the recovery never happens.
    assert b1.recovery_bars is None


def test_b1_stays_right_before_a_repeat_and_wrong_after(
    scores: dict[tuple[str, str], Score],
) -> None:
    b1 = scores[("B1", "repeat")]
    assert b1.accuracy_before_deviation is not None and b1.accuracy_after_deviation is not None
    assert b1.accuracy_before_deviation >= 0.95
    # After the repeat it is a section early; it only "agrees" again once both sit on the
    # song's final section, which the scorer counts honestly.
    assert b1.accuracy_after_deviation < 0.4


def test_boundary_timing_is_measured_signed(scores: dict[tuple[str, str], Score]) -> None:
    s = scores[("B1", "nominal")]
    assert s.boundaries_scored >= 6
    assert abs(s.boundary_error_median_s) < 0.25
    assert s.boundary_error_p95_s < 0.5
    assert s.unexpected_boundaries == 0
    assert scores[("B1", "skip")].unexpected_boundaries == 1


def test_estimates_are_causal_and_in_order(takes: dict[str, Path]) -> None:
    result = run_baseline("B1", takes["nominal"], block=BLOCK)
    times = [e.t_ns for e in result.estimates]
    assert times == sorted(times)
    assert all(e.t_ns >= e.valid_from_ns for e in result.estimates)
    assert result.estimates[0].section == "intro" and result.estimates[0].visit == 1
    assert result.output_hash == run_baseline("B1", takes["nominal"], block=BLOCK).output_hash


def test_pll_locks_to_a_pulse_within_the_tempo_range() -> None:
    hop = BLOCK / RATE
    period = 60 / 132.0
    pll = OnsetPLL(nominal_bpm=128.0, tempo_range=(118.0, 140.0), start_s=1.0, hop_s=hop)
    beats: list[float] = []
    n = int(40 / hop)
    for i in range(n):
        t = i * hop
        phase = (t - 1.0) / period
        onset = 1.0 if t >= 1.0 and abs(phase - round(phase)) * period < hop / 2 else 0.02
        beats += pll.push(t, onset)
    assert len(beats) > 40
    intervals = np.diff(beats[-20:])
    assert abs(60 / intervals.mean() - 132.0) < 1.5
    assert 118.0 <= pll.bpm <= 140.0
    # Beats are reported at or before "now", never in the future.
    assert max(beats) <= n * hop


def test_pll_never_leaves_the_song_range() -> None:
    hop = BLOCK / RATE
    pll = OnsetPLL(nominal_bpm=128.0, tempo_range=(118.0, 140.0), start_s=0.5, hop_s=hop)
    period = 60 / 200.0  # a pulse far too fast for the song
    for i in range(int(30 / hop)):
        t = i * hop
        phase = (t - 0.5) / period
        pll.push(t, 1.0 if t >= 0.5 and abs(phase - round(phase)) * period < hop / 2 else 0.0)
    assert 118.0 <= pll.bpm <= 140.0


def test_score_handles_positions_off_the_grid() -> None:
    song = _song()
    from roobotto.eval.annotations import Beat
    from roobotto.tracker.baselines import Estimate

    beats = [
        Beat(section="outro", visit=1, bar=1, beat=b, at_s=10.0 + b, bpm=128.0) for b in range(1, 5)
    ]
    estimates = [
        Estimate(
            t_ns=round(9.5e9),
            valid_from_ns=round(9.5e9),
            section="outro",
            visit=3,
            bar=1,
            beat=1,
            tempo_bpm=128.0,
        )
    ]
    s = score_estimates(song, estimates, beats, [], None)
    assert s.beats_scored == 4 and s.section_visit_accuracy == 0.0
    assert s.production_equivalent_accuracy == 0.0


def test_cli_baseline(
    takes: dict[str, Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    out = tmp_path / "b1.json"
    assert (
        main(
            [
                "replay",
                "baseline",
                str(takes["drift"]),
                "--method",
                "B1",
                "--block",
                str(BLOCK),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "section_visit_accuracy" in text and "B1" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["method"] == "B1" and data["score"]["beats_scored"] > 200
    assert len(data["estimates"]) > 100
    assert main(["replay", "baseline", str(takes["drift"]), "--method", "B9"]) == 2
    assert main(["replay", "baseline", str(tmp_path / "nope"), "--method", "B0"]) == 1
