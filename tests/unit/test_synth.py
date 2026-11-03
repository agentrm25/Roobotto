"""T-08 acceptance: at least three songs render in every take variation to separate feeds with
exact annotations, deterministically, with causal twins and per-stream dropouts."""

import hashlib
import json
import re
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs]

from roobotto.cli import main
from roobotto.config import schemas
from roobotto.config.loader import load_config
from roobotto.config.song import Song
from roobotto.eval import synth
from roobotto.eval.annotations import Annotations, TakeManifest
from roobotto.eval.synth import STREAMS, VARIATIONS, Take, plan_take, render_take, write_take

ROOT = Path(__file__).parents[2]
SONGS = sorted((ROOT / "examples" / "songs").glob("*.yaml"))
RATE = 8000  # fast; the content is the same at 48 kHz


def _song(path: Path) -> Song:
    model, issues = load_config(path)
    assert not issues and isinstance(model, Song), (path, issues)
    return model


@pytest.fixture(scope="module")
def takes() -> dict[tuple[str, str], Take]:
    out: dict[tuple[str, str], Take] = {}
    for path in SONGS:
        song = _song(path)
        for variation in VARIATIONS:
            out[(song.id, variation)] = render_take(plan_take(song, variation), sample_rate=RATE)
    return out


def test_at_least_three_songs_render_every_variation(takes: dict[tuple[str, str], Take]) -> None:
    songs = {song for song, _ in takes}
    assert len(songs) >= 3
    assert set(VARIATIONS) >= {"nominal", "drift", "repeat", "skip", "extend", "dropout"}
    for take in takes.values():
        assert set(take.streams) == set(STREAMS) == {"drums", "bass", "chords", "vocal"}
        n = {len(a) for a in take.streams.values()}
        assert len(n) == 1  # separate feeds, same length
        assert all(a.dtype == np.float64 for a in take.streams.values())
        assert all(np.max(np.abs(a)) <= 1.0 for a in take.streams.values())


def test_annotations_are_exact_and_consistent(takes: dict[tuple[str, str], Take]) -> None:
    for (song_id, variation), take in takes.items():
        ann = take.annotations
        assert ann.song == song_id and ann.variation == variation and ann.synthetic is True
        assert ann.sample_rate == RATE
        beats = [b.at_s for b in ann.beats]
        assert beats == sorted(beats) and len(set(beats)) == len(beats)
        bpb = take.song.beats_per_bar
        assert len(ann.beats) == sum(v.bars for v in ann.visits) * bpb
        assert [b for b in ann.beats if b.beat == 1] == [
            b for b in ann.beats if b.at_s in {x.start_s for x in ann.bars}
        ]
        for visit in ann.visits:
            inside = [b for b in ann.beats if b.section == visit.section and b.visit == visit.visit]
            assert inside[0].at_s == visit.start_s and len(inside) == visit.bars * bpb
            assert visit.end_s > visit.start_s
        for bar in ann.bars:
            assert take.samples_at(bar.start_s) == bar.start_sample
        assert ann.sync_bursts[0].at_s < ann.beats[0].at_s
        assert ann.sync_bursts[-1].at_s > ann.beats[-1].at_s
        assert ann.duration_s * RATE == pytest.approx(len(take.streams["drums"]), abs=1)
        # Every song cue whose section is visited has an intended instant per visit.
        for cue in take.song.cues:
            visits = [v for v in ann.visits if v.section == cue.at.section]
            instants = [c for c in ann.cues if c.id == cue.id]
            assert len(instants) == len(visits), (song_id, variation, cue.id)
            for inst in instants:
                assert inst.at_s >= ann.beats[0].at_s - 2 * 60 / take.song.tempo.range[0]
        # Tempo stays within the song's range at every beat.
        lo, hi = take.song.tempo.range
        assert all(lo <= b.bpm <= hi for b in ann.beats)


def test_render_is_deterministic(tmp_path: Path) -> None:
    song = _song(SONGS[0])
    a = render_take(plan_take(song, "drift"), sample_rate=RATE)
    b = render_take(plan_take(song, "drift"), sample_rate=RATE)
    for name in STREAMS:
        assert np.array_equal(a.streams[name], b.streams[name])
    assert a.annotations == b.annotations
    da, db = tmp_path / "a", tmp_path / "b"
    write_take(a, da)
    write_take(b, db)
    for f in sorted(p.name for p in da.iterdir()):
        assert (da / f).read_bytes() == (db / f).read_bytes(), f


def test_drift_take_has_a_tempo_curve(takes: dict[tuple[str, str], Take]) -> None:
    song_id = _song(SONGS[0]).id
    nominal, drift = takes[(song_id, "nominal")], takes[(song_id, "drift")]
    assert len({b.bpm for b in nominal.annotations.beats}) == 1
    bpms = [b.bpm for b in drift.annotations.beats]
    assert bpms[-1] > bpms[0] and bpms == sorted(bpms)
    assert len(drift.annotations.tempo) >= 2
    intervals = np.diff([b.at_s for b in drift.annotations.beats])
    assert intervals[-1] < intervals[0]


@pytest.mark.parametrize("variation", ["repeat", "skip", "extend"])
def test_deviation_takes_are_causal_twins_of_nominal(
    takes: dict[tuple[str, str], Take], variation: str
) -> None:
    for path in SONGS:
        song = _song(path)
        nominal, other = takes[(song.id, "nominal")], takes[(song.id, variation)]
        ann = other.annotations
        assert ann.twin_of == f"{song.id}/nominal"
        assert ann.diverges_at_s is not None and ann.deviations
        dev = ann.deviations[0]
        assert dev.kind == variation and dev.at_s == ann.diverges_at_s
        split = other.samples_at(ann.diverges_at_s)
        assert split > RATE  # after the sync burst and some music
        for name in STREAMS:
            a, b = nominal.streams[name], other.streams[name]
            assert np.array_equal(a[:split], b[:split]), (song.id, variation, name)
        differs = any(
            not np.array_equal(
                nominal.streams[n][split : split + 4 * RATE],
                other.streams[n][split : split + 4 * RATE],
            )
            for n in ("drums", "bass", "chords")
        )
        assert differs, (song.id, variation)
        # The visits before the divergence are identical; after it they differ as named.
        before = [v for v in ann.visits if v.end_s <= ann.diverges_at_s]
        assert before == nominal.annotations.visits[: len(before)]
        nominal_bars = sum(v.bars for v in nominal.annotations.visits)
        other_bars = sum(v.bars for v in ann.visits)
        if variation == "skip":
            assert other_bars < nominal_bars
            assert len(ann.visits) == len(nominal.annotations.visits) - 1
        elif variation == "repeat":
            assert len(ann.visits) == len(nominal.annotations.visits) + 1
            assert dev.section == ann.visits[len(before)].section
        else:
            assert other_bars > nominal_bars and len(ann.visits) == len(nominal.annotations.visits)


def test_dropout_take_silences_one_stream_at_a_time(takes: dict[tuple[str, str], Take]) -> None:
    for path in SONGS:
        song = _song(path)
        nominal, drop = takes[(song.id, "nominal")], takes[(song.id, "dropout")]
        ann = drop.annotations
        assert ann.dropouts and {d.stream for d in ann.dropouts} >= {"drums"}
        for d in ann.dropouts:
            s, e = drop.samples_at(d.start_s), drop.samples_at(d.end_s)
            assert not np.any(drop.streams[d.stream][s:e])
            assert np.any(nominal.streams[d.stream][s:e])
            assert np.any(drop.streams[d.stream][e : e + RATE])
            others = [n for n in STREAMS if n != d.stream]
            for n in others:
                assert np.array_equal(drop.streams[n][s:e], nominal.streams[n][s:e])


def test_vocal_feed_follows_the_vocal_flag(takes: dict[tuple[str, str], Take]) -> None:
    for path in SONGS:
        song = _song(path)
        take = takes[(song.id, "nominal")]
        by_id = {s.id: s for s in song.sections}
        for visit in take.annotations.visits:
            s, e = take.samples_at(visit.start_s), take.samples_at(visit.end_s)
            energy = float(np.mean(np.abs(take.streams["vocal"][s:e])))
            if by_id[visit.section].vocal:
                assert energy > 1e-3, (song.id, visit.section)
            else:
                assert energy == 0.0, (song.id, visit.section)


def test_sync_bursts_reach_every_feed(takes: dict[tuple[str, str], Take]) -> None:
    take = takes[(_song(SONGS[0]).id, "nominal")]
    for burst in take.annotations.sync_bursts:
        s = take.samples_at(burst.at_s)
        for name in STREAMS:
            assert np.max(np.abs(take.streams[name][s : s + RATE // 20])) > 0.5, (name, burst)


def test_written_take_has_manifest_hashes_and_reads_back(tmp_path: Path) -> None:
    song = _song(SONGS[0])
    take = render_take(plan_take(song, "repeat"), sample_rate=RATE)
    out = write_take(take, tmp_path / "t")
    manifest = TakeManifest.model_validate_json((out / "manifest.json").read_text(encoding="utf-8"))
    ann = Annotations.model_validate_json((out / "annotations.json").read_text(encoding="utf-8"))
    assert manifest.take == ann.take == f"{song.id}/repeat"
    assert manifest.take_type == "synthetic" and manifest.consent == "synthetic"
    assert set(manifest.channels) == set(STREAMS)
    for name, file in manifest.channels.items():
        data, rate = sf.read(out / file, dtype="float64")
        assert rate == RATE and len(data) == len(take.streams[name])
        assert np.max(np.abs(data - take.streams[name])) < 2**-22  # 24-bit quantization
        assert manifest.sha256[file] == hashlib.sha256((out / file).read_bytes()).hexdigest()
    loaded = synth.read_take(out)
    assert loaded.annotations == ann and loaded.manifest == manifest
    assert np.array_equal(loaded.stream("drums"), sf.read(out / "drums.flac", dtype="float64")[0])


def test_schemas_cover_annotations_and_manifests() -> None:
    assert {"annotations", "take_manifest"} <= set(schemas.SCHEMAS)


def test_no_randomness_in_the_renderer() -> None:
    for path in (ROOT / "src" / "roobotto" / "eval").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "random" not in text, path.name
        assert not re.search(r"(?<![\w.])hash\(", text), path.name  # Python's salted hash()


def test_cli_renders_a_song(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["synth", "list"]) == 0
    assert "nominal" in capsys.readouterr().out
    assert (
        main(
            [
                "synth",
                "render",
                str(SONGS[0]),
                "--out",
                str(tmp_path),
                "--rate",
                str(RATE),
                "--variation",
                "nominal",
                "--variation",
                "skip",
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "skip" in out and "nominal" in out
    written = sorted(p.relative_to(tmp_path).as_posix() for p in tmp_path.rglob("*") if p.is_file())
    song_id = _song(SONGS[0]).id
    assert f"{song_id}/nominal/manifest.json" in written and f"{song_id}/skip/drums.flac" in written
    assert (
        main(["synth", "render", str(SONGS[0]), "--out", str(tmp_path), "--variation", "nope"]) == 2
    )
    assert main(["synth", "render", str(tmp_path / "missing.yaml"), "--out", str(tmp_path)]) == 1
    assert "error" in capsys.readouterr().err
    data = json.loads(
        (tmp_path / song_id / "skip" / "annotations.json").read_text(encoding="utf-8")
    )
    assert data["kind"] == "annotations" and data["variation"] == "skip"
