"""Synthetic renderer (T-08): a song YAML becomes separate audio feeds plus exact annotations.

Simple deterministic pulses and tones, no acoustic realism: drum pulses, bass roots, chord tones
and a tone standing in for vocals in sections marked ``vocal: true``. Every take of a song is
rendered causally, sample by sample from the same recipe, so a take that deviates from the
nominal path (a repeat, a skip, an extension) is a **causal twin** of the nominal take: identical
up to the instant the band's choice becomes audible, different after it. Dropouts silence one
feed for a stated interval and leave the others untouched. Nothing here is left to chance:
the same song, variation and sample rate give the same bytes.
"""

import hashlib
import itertools
import json
import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import numpy as np
import numpy.typing as npt
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs]

from roobotto.config.loader import load_config
from roobotto.config.song import Section, Song
from roobotto.eval.annotations import (
    Annotations,
    Bar,
    Beat,
    CueInstant,
    Deviation,
    Dropout,
    SyncBurst,
    TakeManifest,
    TempoKnot,
    Visit,
)

Array = npt.NDArray[np.float64]
StreamName = Literal["drums", "bass", "chords", "vocal"]
Variation = Literal["nominal", "drift", "repeat", "skip", "extend", "dropout"]

STREAMS: tuple[StreamName, ...] = ("drums", "bass", "chords", "vocal")
VARIATIONS: tuple[Variation, ...] = ("nominal", "drift", "repeat", "skip", "extend", "dropout")
DEFAULT_SAMPLE_RATE = 48_000
BIT_DEPTH = 24

FIRST_BURST_S = 0.5
MUSIC_START_S = 1.0
END_TAIL_S = 0.5
"""Silence between the last beat's end and the closing sync burst."""
BURST_S = 0.05
BURST_HZ = 1000.0
BURST_LEVEL = 0.9

_A1_HZ = 55.0
_MAJOR = (0, 2, 4, 5, 7, 9, 11)
_DEGREES = (1, 4, 5, 6, 2, 3)
"""Scale degrees a section's progression is drawn from."""


class SynthError(ValueError):
    pass


# --- Planning ---


@dataclass(frozen=True)
class PlannedVisit:
    section: str
    visit: int
    bars: int
    deviation: Literal["repeat", "skip", "extend"] | None = None


@dataclass(frozen=True)
class PlannedDropout:
    stream: StreamName
    bar_index: int
    """Global bar index into the take, from 0."""
    bars: int


@dataclass(frozen=True)
class PlannedDeviation:
    kind: Literal["repeat", "skip", "extend"]
    visit_index: int
    """Index into ``visits`` of the first visit that differs from the nominal plan."""
    bar: int
    """Bar within that visit where the audio first differs (1-based)."""
    section: str
    modeled: bool
    detail: str


@dataclass(frozen=True)
class TakePlan:
    song: Song
    variation: Variation
    visits: tuple[PlannedVisit, ...]
    tempo: tuple[tuple[int, float], ...]
    """Tempo knots as (beat index, bpm); linear between knots, flat outside."""
    dropouts: tuple[PlannedDropout, ...] = ()
    deviation: PlannedDeviation | None = None

    @property
    def take(self) -> str:
        return f"{self.song.id}/{self.variation}"

    @property
    def total_bars(self) -> int:
        return sum(v.bars for v in self.visits)


def _nominal_visits(song: Song) -> list[PlannedVisit]:
    visits: list[PlannedVisit] = []
    for section in song.sections:
        visits.append(PlannedVisit(section.id, 1, section.bars))
        if section.ends:
            break
    return visits


def _renumber(visits: Sequence[PlannedVisit]) -> tuple[PlannedVisit, ...]:
    counts: dict[str, int] = {}
    out: list[PlannedVisit] = []
    for v in visits:
        counts[v.section] = counts.get(v.section, 0) + 1
        out.append(PlannedVisit(v.section, counts[v.section], v.bars, v.deviation))
    return tuple(out)


def _pick(visits: Sequence[PlannedVisit], song: Song, want: str) -> tuple[int, bool]:
    """Index of the visit a deviation applies to, and whether the song models that move."""
    by_id = {s.id: s for s in song.sections}
    candidates = range(1, len(visits) - 1) if len(visits) > 2 else range(len(visits))

    def has(section: Section) -> bool:
        if want == "repeat":
            return section.repeat is not None
        if want == "extend":
            return section.extendable is not None
        return section.model_as is not None

    for i in candidates:
        if has(by_id[visits[i].section]):
            return i, True
    if want == "skip":
        return (1 if len(visits) > 2 else 0), False
    return (len(visits) - 2 if len(visits) > 1 else 0), False


def plan_take(song: Song, variation: str) -> TakePlan:
    if variation not in VARIATIONS:
        raise SynthError(f"unknown variation '{variation}'; choose from {', '.join(VARIATIONS)}")
    nominal = _nominal_visits(song)
    bpb = song.beats_per_bar
    total_beats = sum(v.bars for v in nominal) * bpb
    flat: tuple[tuple[int, float], ...] = ((0, song.tempo.nominal),)
    by_id = {s.id: s for s in song.sections}

    if variation == "nominal":
        return TakePlan(song, "nominal", _renumber(nominal), flat)

    if variation == "drift":
        lo, hi = song.tempo.range
        end = song.tempo.nominal + 0.8 * (hi - song.tempo.nominal)
        if end <= song.tempo.nominal:
            end = max(lo, song.tempo.nominal * 0.92)
        return TakePlan(
            song, "drift", _renumber(nominal), ((0, song.tempo.nominal), (total_beats - 1, end))
        )

    if variation == "dropout":
        drops: list[PlannedDropout] = []
        bars_before = [sum(v.bars for v in nominal[:i]) for i in range(len(nominal))]
        second = min(1, len(nominal) - 1)
        drops.append(PlannedDropout("drums", bars_before[second] + 1, 2))
        vocal = next((i for i, v in enumerate(nominal) if by_id[v.section].vocal), None)
        if vocal is not None:
            drops.append(PlannedDropout("vocal", bars_before[vocal] + nominal[vocal].bars // 2, 1))
        third = min(2, len(nominal) - 1)
        drops.append(PlannedDropout("bass", bars_before[third] + nominal[third].bars // 2, 1))
        spans = sorted((d.bar_index, d.bar_index + d.bars) for d in drops)
        for (_, end), (start, _) in itertools.pairwise(spans):
            if start < end:
                raise SynthError(f"dropouts overlap in {song.id}; one feed at a time")
        return TakePlan(song, "dropout", _renumber(nominal), flat, tuple(drops))

    if variation == "repeat":
        i, modeled = _pick(nominal, song, "repeat")
        again = PlannedVisit(nominal[i].section, 0, nominal[i].bars, "repeat")
        visits = _renumber([*nominal[: i + 1], again, *nominal[i + 1 :]])
        dev = PlannedDeviation(
            "repeat", i + 1, 1, nominal[i].section, modeled,
            f"'{nominal[i].section}' played again as visit {visits[i + 1].visit}",
        )  # fmt: skip
        return TakePlan(song, "repeat", visits, flat, (), dev)

    if variation == "skip":
        i, modeled = _pick(nominal, song, "skip")
        skipped = nominal[i].section
        rest = [*nominal[:i], *nominal[i + 1 :]]
        if i < len(rest):
            rest[i] = PlannedVisit(rest[i].section, 0, rest[i].bars, "skip")
        visits = _renumber(rest)
        dev = PlannedDeviation(
            "skip", i, 1, skipped, modeled,
            f"'{skipped}' skipped; went straight to '{visits[i].section}'"
            if i < len(visits)
            else f"'{skipped}' skipped; the song ended",
        )  # fmt: skip
        return TakePlan(song, "skip", visits, flat, (), dev)

    i, modeled = _pick(nominal, song, "extend")
    section = by_id[nominal[i].section]
    extra = section.extendable.unit if section.extendable is not None else 2
    longer = PlannedVisit(section.id, 0, nominal[i].bars + extra, "extend")
    visits = _renumber([*nominal[:i], longer, *nominal[i + 1 :]])
    dev = PlannedDeviation(
        "extend", i, nominal[i].bars + 1, section.id, modeled,
        f"'{section.id}' extended by {extra} bar(s)",
    )  # fmt: skip
    return TakePlan(song, "extend", visits, flat, (), dev)


# --- Timing ---


def _bpm_at(knots: Sequence[tuple[int, float]], beat: int) -> float:
    if beat <= knots[0][0]:
        return knots[0][1]
    for (b0, t0), (b1, t1) in itertools.pairwise(knots):
        if b0 <= beat <= b1:
            return t0 + (t1 - t0) * (beat - b0) / (b1 - b0) if b1 > b0 else t1
    return knots[-1][1]


@dataclass(frozen=True)
class _Grid:
    """Every beat of the take with its start time and duration."""

    starts: list[float]
    durations: list[float]
    bpms: list[float]
    positions: list[tuple[int, int, int]]
    """(visit index, bar within visit from 1, beat within bar from 1)."""


def _grid(plan: TakePlan) -> _Grid:
    bpb = plan.song.beats_per_bar
    t = MUSIC_START_S
    starts: list[float] = []
    durations: list[float] = []
    bpms: list[float] = []
    positions: list[tuple[int, int, int]] = []
    k = 0
    for vi, visit in enumerate(plan.visits):
        for bar in range(1, visit.bars + 1):
            for beat in range(1, bpb + 1):
                bpm = _bpm_at(plan.tempo, k)
                starts.append(t)
                durations.append(60.0 / bpm)
                bpms.append(bpm)
                positions.append((vi, bar, beat))
                t += 60.0 / bpm
                k += 1
    return _Grid(starts, durations, bpms, positions)


# --- Musical content, derived from the song alone ---


def _progression(song: Song, section_id: str) -> tuple[int, ...]:
    """Four scale degrees per section, shared by sections that model_as each other."""
    by_id = {s.id: s for s in song.sections}
    root = section_id
    while by_id[root].model_as:
        root = by_id[root].model_as or root
    digest = hashlib.sha256(f"{song.id}:{root}".encode()).digest()
    return tuple(_DEGREES[b % len(_DEGREES)] for b in digest[:4])


def _hz(degree: int, octave: int) -> float:
    semis = _MAJOR[(degree - 1) % 7] + 12 * octave
    return _A1_HZ * 2 ** (semis / 12)


def _tone(rate: int, hz: float, seconds: float, *, decay: float, level: float) -> Array:
    n = max(1, round(seconds * rate))
    t = np.arange(n, dtype=np.float64) / rate
    env = np.exp(-t * decay)
    return (level * env * np.sin(2 * math.pi * hz * t)).astype(np.float64)


def _add(target: Array, start: int, chunk: Array) -> None:
    end = min(len(target), start + len(chunk))
    if end > start:
        target[start:end] += chunk[: end - start]


# --- Rendering ---


@dataclass(frozen=True)
class Take:
    plan: TakePlan
    sample_rate: int
    streams: Mapping[str, Array]
    annotations: Annotations

    @property
    def song(self) -> Song:
        return self.plan.song

    def samples_at(self, seconds: float) -> int:
        return round(seconds * self.sample_rate)


def render_take(plan: TakePlan, *, sample_rate: int = DEFAULT_SAMPLE_RATE) -> Take:
    song = plan.song
    rate = sample_rate
    bpb = song.beats_per_bar
    grid = _grid(plan)
    last_end = grid.starts[-1] + grid.durations[-1]
    end_burst = last_end + END_TAIL_S
    duration = end_burst + BURST_S + END_TAIL_S
    total = round(duration * rate)
    streams: dict[str, Array] = {name: np.zeros(total) for name in STREAMS}
    by_id = {s.id: s for s in song.sections}

    def at(seconds: float) -> int:
        return round(seconds * rate)

    progressions = {v.section: _progression(song, v.section) for v in plan.visits}
    for k, (start, dur, (vi, bar, beat)) in enumerate(
        zip(grid.starts, grid.durations, grid.positions, strict=True)
    ):
        s = at(start)
        section = by_id[plan.visits[vi].section]
        degree = progressions[section.id][(bar - 1) % 4]
        # Drums: a low pulse on beat 1, a snap on the backbeats, a tick on every beat.
        _add(streams["drums"], s, _tone(rate, 3000.0, 0.02, decay=200.0, level=0.3))
        if beat == 1:
            _add(streams["drums"], s, _tone(rate, 60.0, 0.15, decay=20.0, level=0.6))
        elif (bpb >= 4 and beat % 2 == 1) or (bpb < 4 and beat == bpb):
            _add(streams["drums"], s, _tone(rate, 1200.0, 0.08, decay=50.0, level=0.5))
        # Bass: the root on every beat, decaying within the beat.
        _add(streams["bass"], s, _tone(rate, _hz(degree, 0), dur * 0.9, decay=3.0, level=0.8))
        # Chords: the triad, sustained for the bar from its first beat.
        if beat == 1:
            bar_len = sum(grid.durations[k : k + bpb])
            for step, level in ((0, 0.25), (2, 0.2), (4, 0.2)):
                _add(
                    streams["chords"],
                    s,
                    _tone(rate, _hz(degree + step, 2), bar_len, decay=0.4, level=level),
                )
        # Vocal: a tone cycling through chord tones, only where the song says someone sings.
        if section.vocal:
            step = (0, 4, 2, 0)[(beat - 1) % 4]
            hz = _hz(degree + step, 3)
            n = at(dur * 0.8)
            t = np.arange(n) / rate
            vib = np.sin(2 * math.pi * (hz + 3.0 * np.sin(2 * math.pi * 5.0 * t)) * t)
            _add(streams["vocal"], s, 0.6 * np.minimum(1.0, t * 40.0) * vib)

    for burst_at in (FIRST_BURST_S, end_burst):
        for name in STREAMS:
            _add(
                streams[name],
                at(burst_at),
                _tone(rate, BURST_HZ, BURST_S, decay=0.0, level=BURST_LEVEL),
            )

    # Annotations, exact.
    bar_starts: dict[tuple[int, int], float] = {}
    beats: list[Beat] = []
    bars: list[Bar] = []
    for start, bpm, (vi, bar, beat) in zip(grid.starts, grid.bpms, grid.positions, strict=True):
        v = plan.visits[vi]
        beats.append(
            Beat(section=v.section, visit=v.visit, bar=bar, beat=beat, at_s=start, bpm=bpm)
        )
        if beat == 1:
            bar_starts[(vi, bar)] = start
            bars.append(
                Bar(
                    section=v.section, visit=v.visit, bar=bar, start_s=start, start_sample=at(start)
                )
            )
    visits: list[Visit] = []
    for vi, v in enumerate(plan.visits):
        first = bar_starts[(vi, 1)]
        end = bar_starts[(vi + 1, 1)] if vi + 1 < len(plan.visits) else last_end
        visits.append(
            Visit(
                section=v.section,
                visit=v.visit,
                bars=v.bars,
                start_s=first,
                end_s=end,
                deviation=v.deviation,
            )
        )
    beat_index = {(vi, bar, beat): k for k, (vi, bar, beat) in enumerate(grid.positions)}
    cues: list[CueInstant] = []
    for cue in song.cues:
        for vi, v in enumerate(plan.visits):
            if v.section != cue.at.section or cue.at.bar > v.bars:
                continue
            k = beat_index[(vi, cue.at.bar, cue.at.beat)]
            cues.append(
                CueInstant(
                    id=cue.id,
                    section=v.section,
                    visit=v.visit,
                    at_s=grid.starts[k] + cue.at.offset_beats * grid.durations[k],
                )
            )
    deviations: list[Deviation] = []
    diverges: float | None = None
    if plan.deviation is not None:
        d = plan.deviation
        if d.visit_index < len(plan.visits):
            diverges = bar_starts[(d.visit_index, d.bar)]
            visit_no = plan.visits[d.visit_index].visit
        else:
            diverges = last_end
            visit_no = plan.visits[-1].visit
        deviations.append(
            Deviation(
                kind=d.kind,
                at_s=diverges,
                section=d.section,
                visit=visit_no,
                modeled=d.modeled,
                detail=d.detail,
            )
        )
    dropouts: list[Dropout] = []
    bar_list = [
        bar_starts[(vi, bar)] for vi, v in enumerate(plan.visits) for bar in range(1, v.bars + 1)
    ]
    bar_list.append(last_end)
    for drop in plan.dropouts:
        start_s = bar_list[drop.bar_index]
        end_s = bar_list[min(drop.bar_index + drop.bars, len(bar_list) - 1)]
        streams[drop.stream][at(start_s) : at(end_s)] = 0.0
        dropouts.append(Dropout(stream=drop.stream, start_s=start_s, end_s=end_s))
    tempo = [
        TempoKnot(at_s=grid.starts[min(b, len(grid.starts) - 1)], bpm=bpm) for b, bpm in plan.tempo
    ]
    annotations = Annotations(
        kind="annotations",
        schema_version=0,
        take=plan.take,
        song=song.id,
        variation=plan.variation,
        synthetic=True,
        sample_rate=rate,
        duration_s=total / rate,
        sync_bursts=[
            SyncBurst(at_s=FIRST_BURST_S, streams=list(STREAMS)),
            SyncBurst(at_s=end_burst, streams=list(STREAMS)),
        ],
        visits=visits,
        bars=bars,
        beats=beats,
        tempo=tempo,
        cues=cues,
        deviations=deviations,
        dropouts=dropouts,
        twin_of=f"{song.id}/nominal" if plan.deviation is not None else None,
        diverges_at_s=diverges,
    )
    return Take(plan, rate, streams, annotations)


# --- Files ---


def write_take(take: Take, folder: Path) -> Path:
    """Write one FLAC per stream, ``annotations.json`` and ``manifest.json``. Returns the folder."""
    folder.mkdir(parents=True, exist_ok=True)
    channels: dict[str, str] = {}
    hashes: dict[str, str] = {}
    for name in STREAMS:
        file = f"{name}.flac"
        sf.write(
            folder / file, take.streams[name], take.sample_rate, subtype="PCM_24", format="FLAC"
        )
        channels[name] = file
        hashes[file] = hashlib.sha256((folder / file).read_bytes()).hexdigest()
    (folder / "annotations.json").write_text(
        take.annotations.model_dump_json(indent=1) + "\n", encoding="utf-8"
    )
    manifest = TakeManifest(
        kind="take_manifest",
        schema_version=0,
        take=take.plan.take,
        song=take.song.id,
        take_type="synthetic",
        variation=take.plan.variation,
        sample_rate=take.sample_rate,
        bit_depth=BIT_DEPTH,
        channels=channels,  # pyright: ignore[reportArgumentType]
        sha256=hashes,
        consent="synthetic",
        render={
            "renderer": "roobotto.eval.synth",
            "song": take.song.id,
            "variation": take.plan.variation,
            "sample_rate": take.sample_rate,
            "visits": [[v.section, v.visit, v.bars] for v in take.plan.visits],
        },
    )
    (folder / "manifest.json").write_text(
        manifest.model_dump_json(indent=1) + "\n", encoding="utf-8"
    )
    return folder


@dataclass(frozen=True)
class LoadedTake:
    folder: Path
    manifest: TakeManifest
    annotations: Annotations

    def stream(self, name: str) -> Array:
        file = self.manifest.channels.get(name)  # pyright: ignore[reportArgumentType]
        if file is None:
            raise SynthError(f"take {self.manifest.take} has no stream '{name}'")
        data, rate = sf.read(self.folder / file, dtype="float64", always_2d=False)
        if rate != self.manifest.sample_rate:
            raise SynthError(f"{file}: sample rate {rate} differs from the manifest")
        arr: Array = np.asarray(data, dtype=np.float64)
        return arr


def read_take(folder: Path) -> LoadedTake:
    manifest = TakeManifest.model_validate_json(
        (folder / "manifest.json").read_text(encoding="utf-8")
    )
    annotations = Annotations.model_validate_json(
        (folder / "annotations.json").read_text(encoding="utf-8")
    )
    return LoadedTake(folder, manifest, annotations)


def render_song_file(
    path: Path, out: Path, variations: Sequence[str], *, sample_rate: int
) -> list[Path]:
    model, issues = load_config(path)
    if issues or not isinstance(model, Song):
        raise SynthError(f"{path} is not a valid song:\n" + "\n".join(str(i) for i in issues))
    written: list[Path] = []
    for variation in variations:
        take = render_take(plan_take(model, variation), sample_rate=sample_rate)
        written.append(write_take(take, out / model.id / variation))
    return written


def describe(take_dir: Path) -> dict[str, Any]:
    loaded = read_take(take_dir)
    a = loaded.annotations
    return {
        "take": a.take,
        "duration_s": round(a.duration_s, 3),
        "visits": [
            f"{v.section}#{v.visit}x{v.bars}" + (f" ({v.deviation})" if v.deviation else "")
            for v in a.visits
        ],
        "diverges_at_s": a.diverges_at_s,
        "dropouts": [f"{d.stream} {d.start_s:.3f}-{d.end_s:.3f}" for d in a.dropouts],
    }


def dumps(obj: Any) -> str:
    return json.dumps(obj, indent=1)
