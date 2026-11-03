"""Baselines B0 and B1 (T-11, plan Section 9).

B0 is the fixed timeline: from the annotated song start, at the song's nominal tempo, count
beats along the nominal path. It is what a click track would give you. B1 counts bars the same
way but takes its beats from the onset PLL on the drum feed, limited to the song's tempo range,
with no content observation at all. Both are expected to fail in known ways: B0 under tempo
drift, B1 on skips and repeats. They exist to be beaten.
"""

from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from roobotto.config.song import Song
from roobotto.engine.hashing import decision_hash
from roobotto.eval.annotations import Annotations
from roobotto.eval.replay import DEFAULT_BLOCK, replay_take
from roobotto.eval.score import Score, score_estimates
from roobotto.eval.synth import nominal_visits
from roobotto.tracker.pll import OnsetPLL

NS = 1_000_000_000
METHODS: tuple[str, ...] = ("B0", "B1")


@dataclass(frozen=True)
class Estimate:
    t_ns: int
    """When the estimate was made: never earlier than the evidence it used."""
    valid_from_ns: int
    """The beat instant it describes (at or before ``t_ns``)."""
    section: str
    visit: int
    bar: int
    beat: int
    tempo_bpm: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class NominalCounter:
    """Maps a running beat count onto the nominal path: section visits in order, each with its
    written bars. After the last beat it stays on the last position."""

    def __init__(self, song: Song) -> None:
        self._bpb = song.beats_per_bar
        self._positions: list[tuple[str, int, int, int]] = []
        for section, visit, bars in nominal_visits(song):
            for bar in range(1, bars + 1):
                for beat in range(1, self._bpb + 1):
                    self._positions.append((section, visit, bar, beat))

    @property
    def total_beats(self) -> int:
        return len(self._positions)

    def position(self, beat_index: int) -> tuple[str, int, int, int]:
        return self._positions[min(max(beat_index, 0), len(self._positions) - 1)]


@dataclass(frozen=True)
class BaselineResult:
    method: str
    take: str
    estimates: list[Estimate]
    score: Score
    annotations: Annotations

    @property
    def output_hash(self) -> str:
        return decision_hash((e.as_dict() for e in self.estimates), origin_ns=0)


def _b0(song: Song, start_s: float, until_s: float, hop_s: float) -> list[Estimate]:
    counter = NominalCounter(song)
    period = 60.0 / song.tempo.nominal
    out: list[Estimate] = []
    k = 0
    beat_s = start_s
    while beat_s <= until_s and k < counter.total_beats:
        section, visit, bar, beat = counter.position(k)
        t_ns = round(beat_s * NS)
        out.append(Estimate(t_ns, t_ns, section, visit, bar, beat, song.tempo.nominal))
        k += 1
        beat_s += period
    _ = hop_s
    return out


def _b1(
    song: Song, start_s: float, onsets: Sequence[tuple[float, float]], hop_s: float
) -> list[Estimate]:
    counter = NominalCounter(song)
    lo, hi = song.tempo.range
    pll = OnsetPLL(
        nominal_bpm=song.tempo.nominal, tempo_range=(lo, hi), start_s=start_s, hop_s=hop_s
    )
    out: list[Estimate] = []
    k = 0
    for t_s, onset in onsets:
        for beat_s in pll.push(t_s, onset):
            section, visit, bar, beat = counter.position(k)
            out.append(
                Estimate(
                    round(t_s * NS),
                    round(beat_s * NS),
                    section,
                    visit,
                    bar,
                    beat,
                    round(pll.bpm, 3),
                )
            )
            k += 1
    return out


def run_baseline(
    method: str,
    folder: Path,
    *,
    block: int = DEFAULT_BLOCK,
    feed: str = "drums",
    drop: Mapping[str, Sequence[tuple[float, float]]] | None = None,
) -> BaselineResult:
    """Run one baseline on a take. Both are anchored at the annotated song start (the first
    annotated beat), as the plan allows for B0 and B1. ``drop`` withholds a feed's blocks in the
    given intervals (fault injection)."""
    if method not in METHODS:
        raise ValueError(f"unknown method '{method}'; choose from {', '.join(METHODS)}")
    run = replay_take(folder, block=block, feeds=[feed], drop=drop)
    song = run.song
    annotations = run.annotations
    start_s = annotations.beats[0].at_s
    hop_s = block / run.rate
    if method == "B0":
        estimates = _b0(song, start_s, annotations.duration_s, hop_s)
    else:
        onsets = [
            (r["t_ns"] / NS, float(r["onset"])) for r in run.records if r["type"] == "features"
        ]
        estimates = _b1(song, start_s, onsets, hop_s)
    score = score_estimates(
        song,
        estimates,
        annotations.beats,
        annotations.visits,
        annotations.diverges_at_s,
        method=method,
    )
    return BaselineResult(method, annotations.take, estimates, score, annotations)
