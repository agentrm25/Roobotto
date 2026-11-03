"""Block-by-block replay of a take on the simulated clock (T-10, ADR-014, I-9).

Each feed is split into blocks that become available when their last sample was captured. The
harness advances the simulated clock and the future-read guard to each block's availability
time, hands the block to that feed's causal extractors through a guarded series, and records
one feature frame per block plus every input-health change. A simulated live run pushes the same
blocks through a callback-and-queue path with reordering and must produce the same records and
the same output hash. A feed that is absent raises; a feed whose blocks stop is reported as
``lost`` and gets no made-up frames.
"""

import heapq
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt

from roobotto.engine.clock import SimClock
from roobotto.engine.guard import FutureReadGuard, GuardedSeries
from roobotto.engine.hashing import decision_hash
from roobotto.eval.annotations import Annotations
from roobotto.eval.synth import LoadedTake, read_take
from roobotto.features.chroma import BeatSyncChroma, ChromaExtractor
from roobotto.features.health import HealthMonitor
from roobotto.features.onset import OnsetExtractor
from roobotto.messages import Health

__all__ = [
    "Block",
    "FeatureRun",
    "MissingFeedError",
    "live_run",
    "read_take",
    "replay_take",
    "take_blocks",
]

Array = npt.NDArray[np.float64]
NS = 1_000_000_000
DEFAULT_BLOCK = 512
LOST_AFTER_BLOCKS = 2
"""A feed is lost when "now" passes its next expected block by this many block durations."""


class MissingFeedError(ValueError):
    """A requested feed is not in the take. It is never replaced by silence."""


@dataclass(frozen=True)
class Block:
    feed: str
    index: int
    available_ns: int
    """When the block's last sample existed: the earliest a live system could have had it."""
    samples: Array


def take_blocks(
    take: LoadedTake, *, block: int = DEFAULT_BLOCK, feeds: Sequence[str] | None = None
) -> Iterator[Block]:
    """Every full block of every feed, in availability order; feeds in the take's channel order."""
    channels = list(take.manifest.channels)
    wanted = list(feeds) if feeds is not None else channels
    missing = [f for f in wanted if f not in channels]
    if missing:
        raise MissingFeedError(
            f"take {take.manifest.take} has no feed {', '.join(missing)}; "
            f"it has {', '.join(channels)}"
        )
    chosen = [f for f in channels if f in wanted]
    rate = take.manifest.sample_rate
    streams = {f: take.stream(f) for f in chosen}
    count = min(len(s) // block for s in streams.values()) if streams else 0
    for index in range(count):
        available = round((index + 1) * block * NS / rate)
        for feed in chosen:
            yield Block(feed, index, available, streams[feed][index * block : (index + 1) * block])


@dataclass
class _FeedState:
    audio: GuardedSeries[Array]
    onset: OnsetExtractor
    chroma: ChromaExtractor
    beat_chroma: BeatSyncChroma
    health: HealthMonitor
    last_index: int = -1
    last_available_ns: int = -1
    status: Health | None = None
    since_ns: int = 0
    totals: dict[str, int] = field(default_factory=lambda: {"ok": 0, "degraded": 0, "lost": 0})


class FeatureRun:
    """One replay or live run over the chosen feeds. Ingest blocks, then read ``records``."""

    def __init__(
        self,
        *,
        rate: int,
        block: int,
        feeds: Sequence[str],
        beats_ns: Sequence[int] = (),
        beat_source: str = "none",
    ) -> None:
        self.rate = rate
        self.block = block
        self.feeds = tuple(feeds)
        self.clock = SimClock()
        self.guard = FutureReadGuard()
        self.records: list[dict[str, Any]] = []
        self._beats = list(beats_ns)
        self._beat_source = beat_source
        self._next_beat = 0
        self._feeds: dict[str, _FeedState] = {
            f: _FeedState(
                GuardedSeries(self.guard),
                OnsetExtractor(rate, block),
                ChromaExtractor(rate, block),
                BeatSyncChroma(),
                HealthMonitor(rate, block),
            )
            for f in self.feeds
        }
        self._block_ns = round(block * NS / rate)

    @property
    def audio(self) -> Mapping[str, GuardedSeries[Array]]:
        return {f: s.audio for f, s in self._feeds.items()}

    @property
    def now_ns(self) -> int:
        return self.clock.now_ns()

    def _set_status(self, feed: str, status: Health, t_ns: int) -> None:
        state = self._feeds[feed]
        if state.status == status:
            return
        if state.status is not None:
            state.totals[state.status] += t_ns - state.since_ns
        state.status, state.since_ns = status, t_ns
        self.records.append({"type": "health", "feed": feed, "t_ns": t_ns, "status": status})

    def _advance(self, t_ns: int) -> None:
        if t_ns > self.clock.now_ns():
            self.clock.advance(t_ns - self.clock.now_ns())
        self.guard.advance(t_ns)
        for feed, state in self._feeds.items():
            if state.last_available_ns < 0:
                continue
            if (
                state.status != "lost"
                and t_ns - state.last_available_ns > LOST_AFTER_BLOCKS * self._block_ns
            ):
                state.health.lost()
                self._set_status(
                    feed, "lost", state.last_available_ns + LOST_AFTER_BLOCKS * self._block_ns
                )

    def ingest(self, block: Block) -> None:
        if block.feed not in self._feeds:
            raise MissingFeedError(f"feed '{block.feed}' is not part of this run")
        state = self._feeds[block.feed]
        if block.index <= state.last_index:
            return  # a duplicate or an out-of-order block from the past: already consumed
        self._advance(block.available_ns)
        state.audio.append(block.available_ns, block.samples)
        state.last_index, state.last_available_ns = block.index, block.available_ns
        samples = state.audio.read_until(self.now_ns)[-1]  # through the guard, never past now
        status = state.health.push(samples)
        self._set_status(block.feed, status, block.available_ns)
        onset = state.onset.push(samples)
        chroma = state.chroma.push(samples)
        state.beat_chroma.push(block.available_ns, chroma)
        self.records.append(
            {
                "type": "features",
                "feed": block.feed,
                "t_ns": block.available_ns,
                "block": block.index,
                "onset": onset,
                "chroma": chroma,
                "health": status,
            }
        )
        self._emit_beats()

    def _emit_beats(self) -> None:
        while self._next_beat < len(self._beats) and self._beats[self._next_beat] <= self.now_ns:
            beat_ns = self._beats[self._next_beat]
            self._next_beat += 1
            for feed in self.feeds:
                mean = self._feeds[feed].beat_chroma.beat_passed(beat_ns)
                if mean is not None:
                    self.records.append(
                        {
                            "type": "beat_chroma",
                            "feed": feed,
                            "t_ns": beat_ns,
                            "emitted_ns": self.now_ns,
                            "chroma": mean,
                            "source": self._beat_source,
                        }
                    )

    def finish(self) -> None:
        for feed, state in self._feeds.items():
            if state.status is not None:
                state.totals[state.status] += self.now_ns - state.since_ns
                state.since_ns = self.now_ns
            _ = feed

    @property
    def output_hash(self) -> str:
        return decision_hash(self.records, origin_ns=0)

    @property
    def health_summary(self) -> dict[str, dict[str, float]]:
        return {
            f: {f"{k}_s": round(v / NS, 3) for k, v in s.totals.items()}
            for f, s in self._feeds.items()
        }


@dataclass(frozen=True)
class RunResult:
    take: str
    feeds: tuple[str, ...]
    rate: int
    block: int
    records: list[dict[str, Any]]
    output_hash: str
    health_summary: dict[str, dict[str, float]]
    annotations: Annotations


def _oracle_beats(annotations: Annotations) -> list[int]:
    return [round(b.at_s * NS) for b in annotations.beats]


def replay_take(
    folder: Path,
    *,
    block: int = DEFAULT_BLOCK,
    feeds: Sequence[str] | None = None,
    beats: str = "none",
    until_s: float | None = None,
    drop: Mapping[str, Sequence[tuple[float, float]]] | None = None,
) -> RunResult:
    """Replay a take from its files. ``drop`` removes a feed's blocks inside the given intervals,
    which is how a feed that stops arriving is simulated."""
    take = read_take(folder)
    wanted = list(feeds) if feeds is not None else list(take.manifest.channels)
    blocks = take_blocks(take, block=block, feeds=wanted)  # raises on a missing feed
    chosen = [f for f in take.manifest.channels if f in wanted]
    beats_ns = _oracle_beats(take.annotations) if beats == "oracle" else []
    if beats not in ("none", "oracle"):
        raise ValueError("beats must be 'none' or 'oracle'")
    run = FeatureRun(
        rate=take.manifest.sample_rate,
        block=block,
        feeds=chosen,
        beats_ns=beats_ns,
        beat_source=beats,
    )
    limit = round(until_s * NS) if until_s is not None else None
    for b in blocks:
        if limit is not None and b.available_ns > limit:
            break
        if drop and any(s * NS <= b.available_ns < e * NS for s, e in drop.get(b.feed, ())):
            continue
        run.ingest(b)
    run.finish()
    return RunResult(
        take.manifest.take, tuple(chosen), run.rate, block, run.records, run.output_hash,
        run.health_summary, take.annotations,
    )  # fmt: skip


@dataclass(frozen=True)
class LiveResult:
    records: list[dict[str, Any]]
    output_hash: str
    health_summary: dict[str, dict[str, float]]


def live_run(
    blocks: Sequence[Block],
    *,
    rate: int,
    block: int,
    feeds: Sequence[str],
    jitter_blocks: int = 0,
    beats_ns: Sequence[int] = (),
) -> LiveResult:
    """The same blocks through a callback-shaped path: the callback only stamps and enqueues
    (AGENTS.md rule 8); a worker reorders by availability within a small window and feeds the
    same ``FeatureRun``. Delivery order is perturbed deterministically by ``jitter_blocks``."""
    queue: list[tuple[int, int, int, Block]] = []
    counter = 0

    def callback(b: Block) -> None:
        nonlocal counter
        heapq.heappush(queue, (b.available_ns, feeds.index(b.feed), counter, b))
        counter += 1

    run = FeatureRun(rate=rate, block=block, feeds=feeds, beats_ns=beats_ns)
    delivery = list(blocks)
    if jitter_blocks > 1:
        for start in range(0, len(delivery), jitter_blocks):
            delivery[start : start + jitter_blocks] = delivery[start : start + jitter_blocks][::-1]
    window = max(jitter_blocks, 1)
    for b in delivery:
        callback(b)
        while len(queue) > window:
            run.ingest(heapq.heappop(queue)[3])
    while queue:
        run.ingest(heapq.heappop(queue)[3])
    run.finish()
    return LiveResult(run.records, run.output_hash, run.health_summary)
