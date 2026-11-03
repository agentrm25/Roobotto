"""T-12 acceptance: the bar-grid follower v0 recovers synthetic deviations within 2 bars,
abstains on causal twins before they diverge, emits valid MusicalState, routes proposals
through the real decision gate without firing, and uses as-captured streams only."""

import inspect
import json
from pathlib import Path

import pytest

from roobotto.cli import main
from roobotto.config.loader import load_config
from roobotto.config.song import Song
from roobotto.eval.synth import VARIATIONS, plan_take, render_take, write_take
from roobotto.messages import MusicalState
from roobotto.tracker.follower import (
    FollowerResult,
    Reference,
    build_reference,
    calibrate_temperature,
    run_follower,
)
from roobotto.tracker.propose import propose_cues

ROOT = Path(__file__).parents[2]
SONGS = sorted((ROOT / "examples" / "songs").glob("*.yaml"))
RATE = 8000
BLOCK = 256
DEVIATIONS = ("repeat", "skip", "extend")


def _song(path: Path) -> Song:
    model, issues = load_config(path)
    assert not issues and isinstance(model, Song)
    return model


@pytest.fixture(scope="module")
def takes(tmp_path_factory: pytest.TempPathFactory) -> dict[tuple[str, str], Path]:
    root = tmp_path_factory.mktemp("takes")
    out: dict[tuple[str, str], Path] = {}
    for path in SONGS:
        song = _song(path)
        for variation in VARIATIONS:
            take = render_take(plan_take(song, variation), sample_rate=RATE)
            out[(song.id, variation)] = write_take(take, root / song.id / variation)
    return out


@pytest.fixture(scope="module")
def references(takes: dict[tuple[str, str], Path]) -> dict[str, Reference]:
    return {
        song: build_reference([folder], block=BLOCK)
        for (song, variation), folder in takes.items()
        if variation == "nominal"
    }


@pytest.fixture(scope="module")
def results(
    takes: dict[tuple[str, str], Path], references: dict[str, Reference]
) -> dict[tuple[str, str], FollowerResult]:
    return {
        key: run_follower(folder, references[key[0]], block=BLOCK) for key, folder in takes.items()
    }


def test_reference_covers_every_bar_and_beat(references: dict[str, Reference]) -> None:
    for path in SONGS:
        song = _song(path)
        ref = references[song.id]
        assert set(ref.feeds) == {"chords", "bass"}
        last = song.sections[-1]
        for section in song.sections:
            for bar in range(1, section.bars + 1):
                for beat in range(1, song.beats_per_bar + 1):
                    final = section is last and bar == last.bars and beat == song.beats_per_bar
                    for feed in ref.feeds:
                        chroma = ref.chroma(feed, section.id, bar, beat)
                        assert len(chroma) == 12
                        # The very last beat has no following beat to close its interval.
                        assert any(chroma) or final, (section.id, bar, beat)


def test_follows_the_nominal_takes(results: dict[tuple[str, str], FollowerResult]) -> None:
    for path in SONGS:
        s = results[(_song(path).id, "nominal")].score
        assert s.section_visit_accuracy >= 0.95, (path.name, s)
        assert s.bar_accuracy >= 0.9, (path.name, s)
        assert s.tempo_error_ratio < 0.03


@pytest.mark.parametrize("variation", DEVIATIONS)
def test_recovers_synthetic_deviations_within_two_bars(
    results: dict[tuple[str, str], FollowerResult], variation: str
) -> None:
    for path in SONGS:
        s = results[(_song(path).id, variation)].score
        assert s.recovery_bars is not None and s.recovery_bars <= 2, (path.name, variation, s)
        assert s.accuracy_after_deviation is not None and s.accuracy_after_deviation >= 0.8
        assert s.accuracy_before_deviation is not None and s.accuracy_before_deviation >= 0.95


@pytest.mark.parametrize("variation", DEVIATIONS)
def test_abstains_on_causal_twins_before_they_diverge(
    results: dict[tuple[str, str], FollowerResult], variation: str
) -> None:
    for path in SONGS:
        song = _song(path)
        nominal, other = results[(song.id, "nominal")], results[(song.id, variation)]
        diverge = other.annotations.diverges_at_s
        assert diverge is not None
        diverge_ns = round(diverge * 1e9)
        before_a = [s for s in nominal.states if s.t_ns < diverge_ns]
        before_b = [s for s in other.states if s.t_ns < diverge_ns]
        assert len(before_a) > 20
        assert [s.model_dump() for s in before_a] == [s.model_dump() for s in before_b]
        dev = other.annotations.deviations[0]
        first_after = next(v for v in other.annotations.visits if v.start_s >= diverge)
        written = song.section(dev.section)
        assert written is not None
        for state in before_b:
            for h in state.hypotheses:
                # Nothing from after the divergence is believed before it: not the repeated
                # visit, not the extension bars, not the section the skip lands on.
                if variation == "repeat":
                    assert not (h.section == dev.section and h.visit == dev.visit) or h.p < 0.5
                elif variation == "extend":
                    assert not (h.section == dev.section and h.bar > written.bars) or h.p < 0.5
                else:
                    landed = (first_after.section, first_after.visit)
                    assert (h.section, h.visit) != landed or h.p < 0.5
        if variation in ("repeat", "extend"):
            # The modeled choice stays open until the music decides it: the forecast at the
            # diverging boundary carries the song's prior, never a commitment.
            last_bar = [s for s in before_b if s.boundary is not None][-1]
            assert last_bar.boundary is not None
            assert max(last_bar.boundary.successors.values()) < 0.9


def test_holds_through_drift_and_dropouts(results: dict[tuple[str, str], FollowerResult]) -> None:
    for path in SONGS:
        song = _song(path)
        assert results[(song.id, "drift")].score.section_visit_accuracy >= 0.95
        assert results[(song.id, "drift")].score.tempo_error_ratio < 0.03
        assert results[(song.id, "dropout")].score.section_visit_accuracy >= 0.85


def test_musical_state_is_valid_and_ordered(results: dict[tuple[str, str], FollowerResult]) -> None:
    r = results[(_song(SONGS[0]).id, "nominal")]
    states = r.states
    assert states and all(isinstance(s, MusicalState) for s in states)
    assert len({s.tracker_session for s in states}) == 1
    assert [s.seq for s in states] == list(range(len(states)))
    assert [s.t_ns for s in states] == sorted(s.t_ns for s in states)
    for s in states:
        MusicalState.model_validate(s.model_dump())
        assert s.song.id == r.annotations.song
        assert s.mode in ("following", "lost")
        if s.mode == "following":
            assert s.beat is not None and s.next_downbeat is not None
            assert s.next_downbeat.t_ns >= s.t_ns
            assert s.health == {"drums": "ok", "chords": "ok", "bass": "ok"} or set(s.health) == {
                "drums",
                "chords",
                "bass",
            }
    assert (
        r.output_hash
        == run_follower(r.folder, build_reference([r.folder], block=BLOCK), block=BLOCK).output_hash
    )


def test_proposals_go_through_the_real_gate_without_firing(
    results: dict[tuple[str, str], FollowerResult],
) -> None:
    """The waltz has a bar-counted decorative cue (chorus1 bar 5) and a bar-counted critical cue
    (chorus2 bar 3). Entry cues at bar 1 are planned a beat late by a tracker, which the real
    scheduler expires (I-4); the bar-counted ones meet the gate."""
    r = results[("synthetic_waltz", "nominal")]
    proposal = propose_cues(r, [ROOT / "examples"])
    kinds = {rec["type"] for rec in proposal.records}
    assert {"visit", "planned", "decision", "output_attempt"} <= kinds
    assert proposal.entered_sections[:3] == ["intro", "verse1", "chorus1"]

    def cue_of(rec: dict[str, object]) -> object:
        occ = rec.get("occurrence")
        if isinstance(occ, dict):
            mapping: dict[object, object] = dict(occ)  # pyright: ignore[reportUnknownArgumentType]
            return mapping.get("cue_id")
        return None

    attempts = [rec for rec in proposal.records if rec["type"] == "output_attempt"]
    assert attempts and all(rec["adapter"] == "mock" for rec in attempts)
    assert {cue_of(rec) for rec in attempts} == {"chorus_pad_swell"}  # decorative, M4, to mock
    assert proposal.fired_physical == 0
    decisions = [rec for rec in proposal.records if rec["type"] == "decision"]
    critical = [rec for rec in decisions if cue_of(rec) == "chorus2_lead"]
    assert critical and all(rec["kind"] != "fire" for rec in critical)
    assert any("critical" in str(rec["reason"]) for rec in critical)  # I-14, whatever the posterior
    assert not [rec for rec in attempts if cue_of(rec) == "chorus2_lead"]
    assert proposal.decisions.get("fire", 0) >= 1
    assert proposal.decision_hash == propose_cues(r, [ROOT / "examples"]).decision_hash


def test_temperature_is_calibrated_on_development_takes(
    takes: dict[tuple[str, str], Path], references: dict[str, Reference]
) -> None:
    dev = [takes[("synthetic_anthem", "nominal")], takes[("synthetic_anthem", "drift")]]
    chosen, table = calibrate_temperature(
        references["synthetic_anthem"], dev, candidates=(0.05, 0.2, 0.8), block=BLOCK
    )
    assert chosen in (0.05, 0.2, 0.8)
    assert set(table) == {0.05, 0.2, 0.8} and all(0.0 <= v <= 1.0 for v in table.values())
    assert table[chosen] == max(table.values())


def test_results_use_as_captured_streams_only() -> None:
    assert "corrected" not in inspect.signature(run_follower).parameters
    assert "corrected" not in inspect.signature(build_reference).parameters
    src = (ROOT / "src" / "roobotto" / "tracker" / "follower.py").read_text(encoding="utf-8")
    assert "corrected" not in src.lower() or "as-captured" in src.lower()
    assert "random" not in src


def test_cli_follow(
    takes: dict[tuple[str, str], Path], tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    nominal, skip = takes[("synthetic_anthem", "nominal")], takes[("synthetic_anthem", "skip")]
    out = tmp_path / "follow.json"
    assert (
        main(
            [
                "replay",
                "follow",
                str(skip),
                "--reference",
                str(nominal),
                "--block",
                str(BLOCK),
                "--propose",
                str(ROOT / "examples"),
                "--out",
                str(out),
            ]
        )
        == 0
    )
    text = capsys.readouterr().out
    assert "recovery_bars" in text and "proposals" in text and "mock" in text
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["score"]["recovery_bars"] is not None and data["states"]
    assert data["proposals"]["fired_physical"] == 0
    assert main(["replay", "follow", str(skip), "--reference", str(tmp_path / "nope")]) == 1
    assert "error" in capsys.readouterr().err
