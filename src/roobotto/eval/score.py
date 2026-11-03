"""Scoring a stream of position estimates against a take's annotations (plan Section 9).

Per annotated beat: the estimate in force halfway through that beat (so a tracker that reports
a beat one block late is not penalized by a whole beat) is compared on section and visit, and on
production equivalence (two positions are equivalent when the rig would do the same things;
positions off the bar grid are never equivalent). Boundary timing error is signed, per annotated
visit start, and split into predicted and unexpected boundaries (the latter are deviations).
"""

from collections.abc import Sequence
from dataclasses import asdict, dataclass
from typing import Any, Protocol

import numpy as np

from roobotto.arrangement import ArrangementError, BeatPosition, Position, expand
from roobotto.config.song import Song
from roobotto.cues.equivalence import EquivalenceClasses, production_classes
from roobotto.eval.annotations import Beat, Visit

NS = 1_000_000_000


class Estimated(Protocol):
    @property
    def t_ns(self) -> int: ...
    @property
    def section(self) -> str: ...
    @property
    def visit(self) -> int: ...
    @property
    def bar(self) -> int: ...
    @property
    def beat(self) -> int: ...
    @property
    def tempo_bpm(self) -> float: ...


@dataclass(frozen=True)
class Score:
    method: str
    beats_scored: int
    section_visit_accuracy: float
    bar_accuracy: float
    production_equivalent_accuracy: float
    tempo_error_ratio: float
    """Mean of |estimated - true| / true over scored beats."""
    tempo_octave_errors: int
    """Beats where the tempo was read at half or double."""
    boundaries_scored: int
    boundary_error_median_s: float
    boundary_error_p95_s: float
    unexpected_boundaries: int
    accuracy_before_deviation: float | None
    accuracy_after_deviation: float | None
    recovery_bars: float | None
    """Bars after the first deviation until the estimate is right again for a whole bar, if ever."""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _classes(song: Song) -> EquivalenceClasses | None:
    try:
        return production_classes(expand(song), horizon_beats=song.beats_per_bar)
    except ArrangementError:
        return None


def _equivalent(classes: EquivalenceClasses | None, truth: Beat, est: Estimated) -> bool:
    if (truth.section, truth.visit, truth.bar) == (est.section, est.visit, est.bar):
        return True
    if classes is None:
        return False
    a = BeatPosition(Position(truth.section, truth.visit, truth.bar), truth.beat)
    b = BeatPosition(Position(est.section, est.visit, est.bar), est.beat)
    try:
        return classes.equivalent(a, b)
    except KeyError:
        return False


def score_estimates(
    song: Song,
    estimates: Sequence[Estimated],
    beats: Sequence[Beat],
    visits: Sequence[Visit],
    diverges_at_s: float | None,
    *,
    method: str = "",
) -> Score:
    classes = _classes(song)
    times = [e.t_ns for e in estimates]
    right_sv: list[bool] = []
    right_bar: list[bool] = []
    right_pe: list[bool] = []
    tempo_err: list[float] = []
    octave = 0
    at: list[float] = []
    for truth in beats:
        t_ns = round((truth.at_s + 30.0 / truth.bpm) * NS)  # mid-beat
        k = int(np.searchsorted(np.asarray(times), t_ns, side="right")) - 1
        at.append(truth.at_s)
        if k < 0:
            right_sv.append(False)
            right_bar.append(False)
            right_pe.append(False)
            tempo_err.append(1.0)
            continue
        est = estimates[k]
        right_sv.append((est.section, est.visit) == (truth.section, truth.visit))
        right_bar.append(right_sv[-1] and est.bar == truth.bar)
        right_pe.append(_equivalent(classes, truth, est))
        ratio = est.tempo_bpm / truth.bpm
        tempo_err.append(abs(ratio - 1.0))
        if abs(ratio - 0.5) < 0.1 or abs(ratio - 2.0) < 0.2:
            octave += 1

    # Boundaries: when did the estimate first enter each annotated visit?
    first_entry: dict[tuple[str, int], float] = {}
    for e in estimates:
        first_entry.setdefault((e.section, e.visit), e.t_ns / NS)
    errors: list[float] = []
    unexpected = 0
    for v in visits[1:]:
        if v.deviation is not None:
            unexpected += 1
        entered = first_entry.get((v.section, v.visit))
        if entered is not None:
            errors.append(entered - v.start_s)

    before: list[bool] = []
    after: list[bool] = []
    recovery: float | None = None
    if diverges_at_s is not None:
        for ok, t in zip(right_sv, at, strict=True):
            (before if t < diverges_at_s else after).append(ok)
        bpb = song.beats_per_bar
        after_beats = [ok for ok, t in zip(right_sv, at, strict=True) if t >= diverges_at_s]
        for i in range(len(after_beats) - bpb + 1):
            if all(after_beats[i : i + bpb]):
                recovery = round(i / bpb, 2)
                break

    def mean(xs: Sequence[bool | float]) -> float:
        return float(np.mean(np.asarray(xs, dtype=np.float64))) if xs else 0.0

    arr = np.asarray(errors) if errors else np.asarray([0.0])
    return Score(
        method=method,
        beats_scored=len(beats),
        section_visit_accuracy=round(mean(right_sv), 4),
        bar_accuracy=round(mean(right_bar), 4),
        production_equivalent_accuracy=round(mean(right_pe), 4),
        tempo_error_ratio=round(mean(tempo_err), 4),
        tempo_octave_errors=octave,
        boundaries_scored=len(errors),
        boundary_error_median_s=round(float(np.median(arr)), 4) if errors else 0.0,
        boundary_error_p95_s=round(float(np.percentile(np.abs(arr), 95)), 4) if errors else 0.0,
        unexpected_boundaries=unexpected,
        accuracy_before_deviation=round(mean(before), 4) if diverges_at_s is not None else None,
        accuracy_after_deviation=round(mean(after), 4) if diverges_at_s is not None else None,
        recovery_bars=recovery,
    )
