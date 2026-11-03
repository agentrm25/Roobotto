"""Bar-grid follower v0 (T-12): exact forward filtering over (section visit, bar, beat, tempo
bin) with the onset PLL's phase.

The state space is every beat position of the song's bar grid (T-03) times a few tempo bins.
Transitions per beat follow the grid: within a bar to the next beat, at a bar's end to the
successors the song allows (next section, extension, repeat, graph jumps), plus a small
probability of jumping anywhere, forward more than back, which is how unmodeled deviations are
recovered. The grid's bar counts and the song's extend and repeat probabilities are the duration
prior. Observations are the beat-synchronous chroma of each listening feed, compared with the
reference take's chroma at that position; the likelihood temperature is calibrated on
development takes. The output is a ``MusicalState`` per beat with a session id. Everything is
causal: a beat is processed once the PLL reports it, from blocks already available, and results
come from the take's as-captured streams only.
"""

import hashlib
import math
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from roobotto.arrangement import BarGrid, BeatPosition, expand
from roobotto.config.song import Song
from roobotto.engine.hashing import decision_hash
from roobotto.eval.annotations import Annotations
from roobotto.eval.replay import DEFAULT_BLOCK, replay_take
from roobotto.eval.score import Score, score_estimates
from roobotto.features.chroma import BeatSyncChroma
from roobotto.messages import MusicalState
from roobotto.tracker.baselines import Estimate
from roobotto.tracker.pll import OnsetPLL

Array = npt.NDArray[np.float64]
NS = 1_000_000_000
DEFAULT_FEEDS: tuple[str, ...] = ("chords", "bass")
BEAT_FEED = "drums"


class FollowerError(ValueError):
    pass


# --- Reference ---


@dataclass(frozen=True)
class Reference:
    """Mean beat-synchronous chroma per feed at each (section, bar, beat) of the reference
    takes. Visits of one section share an entry; bars past a section's written length wrap."""

    feeds: tuple[str, ...]
    table: Mapping[str, Mapping[tuple[str, int, int], tuple[float, ...]]]

    def chroma(self, feed: str, section: str, bar: int, beat: int) -> tuple[float, ...]:
        rows = self.table[feed]
        key = (section, bar, beat)
        if key in rows:
            return rows[key]
        bars = [b for s, b, _ in rows if s == section]
        if not bars:
            return (0.0,) * 12
        return rows.get((section, (bar - 1) % max(bars) + 1, beat), (0.0,) * 12)


def build_reference(
    take_dirs: Sequence[Path], *, feeds: Sequence[str] = DEFAULT_FEEDS, block: int = DEFAULT_BLOCK
) -> Reference:
    """Beat-synchronous chroma of the reference takes, by position, using their annotated beats
    (the reference is annotated by definition)."""
    sums: dict[str, dict[tuple[str, int, int], Array]] = {f: {} for f in feeds}
    counts: dict[str, dict[tuple[str, int, int], int]] = {f: {} for f in feeds}
    for folder in take_dirs:
        run = replay_take(folder, block=block, feeds=list(feeds), beats="oracle")
        beats = run.annotations.beats
        # A beat_chroma record stamped at beat k averages the interval [k-1, k): it describes
        # beat k-1, so it is keyed by the beat that started it.
        previous = {round(b.at_s * NS): beats[i - 1] for i, b in enumerate(beats) if i > 0}
        for rec in run.records:
            if rec["type"] != "beat_chroma":
                continue
            beat = previous.get(int(rec["t_ns"]))
            if beat is None:
                continue
            key = (beat.section, beat.bar, beat.beat)
            feed = str(rec["feed"])
            sums[feed][key] = sums[feed].get(key, np.zeros(12)) + np.asarray(rec["chroma"])
            counts[feed][key] = counts[feed].get(key, 0) + 1
    table = {
        f: {k: tuple(round(float(x), 6) for x in sums[f][k] / counts[f][k]) for k in sums[f]}
        for f in feeds
    }
    if not any(table[f] for f in feeds):
        raise FollowerError("the reference takes produced no beat chroma; check feeds and beats")
    return Reference(tuple(feeds), table)


# --- Configuration ---


@dataclass(frozen=True)
class FollowerConfig:
    feeds: tuple[str, ...] = DEFAULT_FEEDS
    tempo_bins: int = 5
    temperature: float = 0.2
    """Likelihood = exp(-distance / temperature); calibrated on development takes."""
    jump_prob: float = 0.08
    """Mass moved at every beat to a jump anywhere (the unmodeled-deviation escape)."""
    forward_share: float = 0.85
    """Share of the jump mass that goes forward; the rest goes back."""
    jump_horizon_bars: int = 16
    lost_below: float = 0.5
    """Top-hypothesis probability under which the follower reports ``lost``."""
    top_hypotheses: int = 3
    max_extensions: int = 2

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- The filter ---


def _cosine_distance(a: Array, b: Array) -> float:
    na, nb = float(np.linalg.norm(a)), float(np.linalg.norm(b))
    if na < 1e-9 or nb < 1e-9:
        return 1.0 if (na >= 1e-9) != (nb >= 1e-9) else 0.0
    return 1.0 - float(np.dot(a, b) / (na * nb))


@dataclass
class BarGridFollower:
    song: Song
    reference: Reference
    config: FollowerConfig = field(default_factory=FollowerConfig)
    grid: BarGrid = field(init=False)
    positions: tuple[BeatPosition, ...] = field(init=False)
    belief: Array = field(init=False)
    """Shape (states, tempo bins); sums to 1."""
    bins_bpm: Array = field(init=False)
    _index: dict[BeatPosition, int] = field(init=False)
    _within: Array = field(init=False)
    _edges: list[tuple[int, int, float]] = field(init=False)
    _ref: dict[str, Array] = field(init=False)
    _has_ref: npt.NDArray[np.bool_] = field(init=False)
    _session: str = field(init=False)
    _seq: int = 0
    _last_beat_ns: int | None = None

    def __post_init__(self) -> None:
        cfg = self.config
        self.grid = expand(self.song, max_extensions=cfg.max_extensions)
        self.positions = self.grid.beat_positions()
        self._index = {p: i for i, p in enumerate(self.positions)}
        n = len(self.positions)
        bpb = self.song.beats_per_bar
        lo, hi = self.song.tempo.range
        self.bins_bpm = np.exp(np.linspace(math.log(lo), math.log(hi), cfg.tempo_bins))
        # Transitions: within a bar, beat -> beat + 1; at the last beat, the grid's successors.
        edges: list[tuple[int, int, float]] = []
        bar_starts = [i for i, p in enumerate(self.positions) if p.beat == 1]
        for i, p in enumerate(self.positions):
            if p.beat < bpb:
                edges.append(
                    (i, self._index[BeatPosition(p.position, p.beat + 1)], 1.0 - cfg.jump_prob)
                )
            else:
                for t in self.grid.successors(p.position):
                    if t.to is None:
                        edges.append((i, i, t.prob * (1.0 - cfg.jump_prob)))  # the end holds
                    else:
                        j = self._index[BeatPosition(t.to, 1)]
                        edges.append((i, j, t.prob * (1.0 - cfg.jump_prob)))
            # Jump anywhere: forward within the horizon decays with distance; back likewise.
            here_bar = bar_starts.index(i - (p.beat - 1)) if (i - (p.beat - 1)) in bar_starts else 0
            forward = [
                (k, j)
                for k, j in enumerate(bar_starts)
                if 0 < k - here_bar <= cfg.jump_horizon_bars
            ]
            backward = [
                (k, j)
                for k, j in enumerate(bar_starts)
                if 0 < here_bar - k <= cfg.jump_horizon_bars
            ]
            for group, share in ((forward, cfg.forward_share), (backward, 1.0 - cfg.forward_share)):
                if not group:
                    continue
                weights = np.asarray([1.0 / abs(k - here_bar) for k, _ in group])
                weights = weights / weights.sum() * cfg.jump_prob * share
                for (_, j), w in zip(group, weights, strict=True):
                    edges.append((i, j, float(w)))
        self._edges = edges
        self._within = np.zeros(n)
        # Reference chroma per state and feed. The observation that arrives with beat k is the
        # chroma of the interval that just ended, so a state at beat k is matched against the
        # reference of the beat before it. At a section's first beat the predecessor is unknown
        # (it depends on the path), so that state carries no evidence of its own.
        self._ref = {}
        self._has_ref = np.ones(n, dtype=bool)
        for feed in cfg.feeds:
            rows = np.zeros((n, 12))
            for i, p in enumerate(self.positions):
                bar, beat = p.position.bar, p.beat
                if beat > 1:
                    prev = (bar, beat - 1)
                elif bar > 1:
                    prev = (bar - 1, bpb)
                else:
                    self._has_ref[i] = False
                    continue
                rows[i] = np.asarray(
                    self.reference.chroma(feed, p.position.section, prev[0], prev[1])
                )
                if not rows[i].any():
                    self._has_ref[i] = False  # the reference has nothing for this beat
            self._ref[feed] = rows
        # Start: the first beat of the song, tempo at the nominal bin.
        self.belief = np.zeros((n, cfg.tempo_bins))
        nominal_bin = int(
            np.argmin(np.abs(np.log(self.bins_bpm) - math.log(self.song.tempo.nominal)))
        )
        self.belief[0, nominal_bin] = 1.0
        digest = hashlib.sha256(f"{self.song.id}:{cfg.as_dict()}".encode()).hexdigest()
        self._session = digest[:16]

    @property
    def session(self) -> str:
        return self._session

    def _predict(self) -> None:
        k = self.belief.shape[1]
        new = np.zeros_like(self.belief)
        for i, j, w in self._edges:
            new[j] += w * self.belief[i]
        # Tempo bins drift to their neighbours a little.
        tempo = np.zeros_like(new)
        tempo += 0.8 * new
        if k > 1:
            tempo[:, 1:] += 0.1 * new[:, :-1]
            tempo[:, :-1] += 0.1 * new[:, 1:]
            tempo[:, 0] += 0.1 * new[:, 0]
            tempo[:, -1] += 0.1 * new[:, -1]
        self.belief = tempo / max(float(tempo.sum()), 1e-300)

    def _update(self, chroma: Mapping[str, Sequence[float]], bpm: float) -> None:
        like = np.ones(len(self.positions))
        for feed in self.config.feeds:
            observed = np.asarray(chroma.get(feed, [0.0] * 12), dtype=np.float64)
            if float(observed.sum()) <= 0.0:
                continue
            distances = np.asarray([_cosine_distance(observed, row) for row in self._ref[feed]])
            feed_like = np.exp(-distances / self.config.temperature)
            feed_like[~self._has_ref] = float(np.mean(feed_like[self._has_ref]))
            like *= feed_like
        tempo_like = np.exp(-0.5 * ((np.log(self.bins_bpm) - math.log(bpm)) / 0.03) ** 2)
        self.belief = self.belief * like[:, None] * tempo_like[None, :]
        total = float(self.belief.sum())
        if total <= 1e-300:
            self.belief = np.full_like(self.belief, 1.0 / self.belief.size)
        else:
            self.belief /= total

    def observe(
        self,
        beat_ns: int,
        now_ns: int,
        chroma: Mapping[str, Sequence[float]],
        bpm: float,
        health: Mapping[str, str],
    ) -> MusicalState:
        """Advance one beat (the PLL reported a beat at ``beat_ns``, known at ``now_ns``)."""
        if self._last_beat_ns is not None:
            self._predict()
        self._last_beat_ns = beat_ns
        self._update(chroma, bpm)
        return self._state(beat_ns, now_ns, bpm, health)

    def tick(self, now_ns: int, bpm: float, health: Mapping[str, str]) -> MusicalState | None:
        """The current belief again, with the phase moved on, so the stream stays fresh between
        beats (the engine suspends authority after ``tracker_stale_ms``). None before the first
        beat."""
        if self._last_beat_ns is None:
            return None
        return self._state(self._last_beat_ns, now_ns, bpm, health)

    def _state(
        self, beat_ns: int, now_ns: int, bpm: float, health: Mapping[str, str]
    ) -> MusicalState:
        cfg = self.config
        by_state = self.belief.sum(axis=1)
        by_cell: dict[tuple[str, int, int], float] = {}
        for i, p in enumerate(self.positions):
            key = (p.position.section, p.position.visit, p.position.bar)
            by_cell[key] = by_cell.get(key, 0.0) + float(by_state[i])
        ranked = sorted(by_cell.items(), key=lambda kv: (-kv[1], kv[0]))[: cfg.top_hypotheses]
        top_i = int(np.argmax(by_state))
        top = self.positions[top_i]
        period_ns = round(60.0 / bpm * NS)
        beats_left = self.song.beats_per_bar - top.beat
        next_downbeat = (
            beat_ns + (beats_left + (0 if beats_left else self.song.beats_per_bar)) * period_ns
        )
        if beats_left == 0:
            next_downbeat = beat_ns + period_ns
        successors: dict[str, float] = {}
        boundary: dict[str, Any] | None = None
        exits = [t for t in self.grid.successors(top.position) if t.kind != "bar"]
        if exits and top.beat == self.song.beats_per_bar:
            for t in exits:
                name = t.to.section if t.to is not None else "end"
                successors[name] = successors.get(name, 0.0) + t.prob
            boundary = {
                "t_ns": max(beat_ns + period_ns, now_ns),
                "sd_ms": 60_000.0 / bpm / 4,
                "successors": {
                    k: round(min(v, 1.0), 6) for k, v in successors.items() if k != "end"
                },
            }
        top_p = ranked[0][1] if ranked else 0.0
        mode = "following" if top_p >= cfg.lost_below else "lost"
        data: dict[str, Any] = {
            "tracker_session": self._session,
            "seq": self._seq,
            "t_ns": now_ns,
            "song": {"id": self.song.id, "p": 1.0},
            "mode": mode,
            "hypotheses": [
                {"section": s, "visit": v, "bar": b, "p": round(min(p, 1.0), 6)}
                for (s, v, b), p in ranked
            ],
            "beat": {
                "phase": round(min(max((now_ns - beat_ns) / period_ns, 0.0), 0.999999), 6),
                "tempo_bpm": round(bpm, 3),
                "tempo_sd": round(60_000.0 / bpm * 0.02, 3),
            },
            "next_downbeat": {
                "t_ns": max(next_downbeat, now_ns),
                "sd_ms": round(60_000.0 / bpm / 8, 3),
            },
            "health": dict(health),
        }
        if boundary is not None:
            data["boundary"] = boundary
        self._seq += 1
        return MusicalState.model_validate(data)


# --- Running on a take ---


@dataclass(frozen=True)
class FollowerResult:
    folder: Path
    config: FollowerConfig
    states: list[MusicalState]
    estimates: list[Estimate]
    score: Score
    annotations: Annotations
    song: Song
    song_start_ns: int

    @property
    def output_hash(self) -> str:
        return decision_hash((s.model_dump(mode="json") for s in self.states), origin_ns=0)


def run_follower(
    folder: Path,
    reference: Reference,
    *,
    config: FollowerConfig | None = None,
    block: int = DEFAULT_BLOCK,
    drop: Mapping[str, Sequence[tuple[float, float]]] | None = None,
) -> FollowerResult:
    """Replay the take (as-captured streams), drive the PLL from the drum feed and the follower
    from the listening feeds' beat chroma, and score the top hypothesis per beat."""
    cfg = config or FollowerConfig()
    feeds = [BEAT_FEED, *[f for f in cfg.feeds if f != BEAT_FEED]]
    run = replay_take(folder, block=block, feeds=feeds, drop=drop)
    song = run.song
    annotations = run.annotations
    start_s = annotations.beats[0].at_s
    hop_s = block / run.rate
    lo, hi = song.tempo.range
    pll = OnsetPLL(
        nominal_bpm=song.tempo.nominal, tempo_range=(lo, hi), start_s=start_s, hop_s=hop_s
    )
    follower = BarGridFollower(song, reference, cfg)
    sync: dict[str, BeatSyncChroma] = {f: BeatSyncChroma() for f in cfg.feeds}
    health: dict[str, str] = {f: "ok" for f in feeds}
    states: list[MusicalState] = []
    estimates: list[Estimate] = []
    pending_beats: list[float] = []
    for rec in run.records:
        if rec["type"] == "health":
            health[str(rec["feed"])] = str(rec["status"])
            continue
        if rec["type"] != "features":
            continue
        feed = str(rec["feed"])
        t_ns = int(rec["t_ns"])
        if feed in sync:
            sync[feed].push(t_ns, list(rec["chroma"]))
        if feed == BEAT_FEED:
            pending_beats += pll.push(t_ns / NS, float(rec["onset"]))
            if not pending_beats:
                tick = follower.tick(t_ns, pll.bpm, health)
                if tick is not None:
                    states.append(tick)
            for beat_s in pending_beats:
                beat_ns = round(beat_s * NS)
                chroma: dict[str, Sequence[float]] = {}
                for f, s in sync.items():
                    mean = s.beat_passed(beat_ns)
                    if mean is not None:
                        chroma[f] = mean
                state = follower.observe(beat_ns, t_ns, chroma, pll.bpm, health)
                states.append(state)
                top = state.hypotheses[0]
                top_beat = follower.positions[int(np.argmax(follower.belief.sum(axis=1)))].beat
                estimates.append(
                    Estimate(
                        t_ns, beat_ns, top.section, top.visit, top.bar, top_beat, round(pll.bpm, 3)
                    )
                )
            pending_beats = []
    score = score_estimates(
        song,
        estimates,
        annotations.beats,
        annotations.visits,
        annotations.diverges_at_s,
        method="B3",
    )
    return FollowerResult(
        folder, cfg, states, estimates, score, annotations, song, round(start_s * NS)
    )


def calibrate_temperature(
    reference: Reference,
    development_takes: Sequence[Path],
    *,
    candidates: Sequence[float] = (0.05, 0.1, 0.2, 0.4, 0.8),
    block: int = DEFAULT_BLOCK,
) -> tuple[float, dict[float, float]]:
    """Pick the temperature with the best mean section-and-visit accuracy on the development
    takes. Returns it and the full table, so the report can show the choice."""
    table: dict[float, float] = {}
    for temperature in candidates:
        cfg = FollowerConfig(temperature=temperature)
        scores = [
            run_follower(t, reference, config=cfg, block=block).score for t in development_takes
        ]
        table[temperature] = round(sum(s.section_visit_accuracy for s in scores) / len(scores), 4)
    best = max(table.items(), key=lambda kv: (kv[1], -kv[0]))[0]
    return best, table
