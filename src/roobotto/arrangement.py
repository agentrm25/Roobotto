"""Bar-grid expansion of a song (T-03).

A song is a list of sections plus a few optional deviations. The bar grid unrolls it into the
cells the tracker reasons about: ``Position(section, visit, bar)``. Visits are numbered per
section, as in ``MusicalState.hypotheses``; a new visit is a new set of cue occurrences (I-6).

At the end of a section's planned bars (and at the end of each extension block) the band picks
one outcome:

- **extend**: the section runs ``unit`` more bars in the same visit (``extendable``);
- **repeat**: the section starts again as a new visit, up to ``repeat.max`` extra times;
- **jump**: a ``graph`` edge to a later section;
- **next**: the following section, or the end of the song for an ``ends`` section. This takes
  whatever probability the other outcomes leave.

Self-loop edges and ``extendable``/``repeat`` describe the same event, so their probabilities
must agree. Extension has no declared limit, so ``max_extensions`` caps it; positions where that
cap removed an outcome are listed in ``BarGrid.truncated``. Graph edges may only point forward,
which keeps the grid free of cycles and makes the visit number a function of the position.
Everything else the schema allows but this module does not understand raises
``ArrangementError``.
"""

from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Literal

from roobotto.config.song import Section, Song

_EPSILON = 1e-9
DEFAULT_MAX_EXTENSIONS = 2
DEFAULT_MAX_PATHS = 10_000

Kind = Literal["bar", "extend", "repeat", "jump", "next", "end"]


class ArrangementError(ValueError):
    """The song is valid, but cannot be expanded into a bar grid."""


@dataclass(frozen=True)
class Position:
    """One cell of the bar grid. ``visit`` and ``bar`` start at 1."""

    section: str
    visit: int
    bar: int


@dataclass(frozen=True)
class BeatPosition:
    """A cell and a beat within its bar (1 to the meter's beats per bar)."""

    position: Position
    beat: int


@dataclass(frozen=True)
class Transition:
    """A possible move to the next bar. ``to`` is ``None`` when the song ends."""

    to: Position | None
    prob: float
    kind: Kind


@dataclass(frozen=True)
class SongPath:
    """One complete way through the song and its probability."""

    positions: tuple[Position, ...]
    prob: float


@dataclass(frozen=True)
class BarGrid:
    song: Song
    positions: tuple[Position, ...]
    """Every reachable cell in play order; each transition goes to a later cell."""
    transitions: Mapping[Position, tuple[Transition, ...]]
    truncated: frozenset[Position]
    """Cells whose extension outcome was removed by ``max_extensions``, not by the song."""

    @property
    def start(self) -> Position:
        return self.positions[0]

    @property
    def beats_per_bar(self) -> int:
        return self.song.beats_per_bar

    def successors(self, position: Position) -> tuple[Transition, ...]:
        """The possible next cells. Probabilities add up to 1."""
        return self.transitions[position]

    def beat_positions(self) -> tuple[BeatPosition, ...]:
        return tuple(
            BeatPosition(position, beat)
            for position in self.positions
            for beat in range(1, self.beats_per_bar + 1)
        )

    def path_count(self) -> int:
        counts: dict[Position, int] = {}
        for position in reversed(self.positions):
            counts[position] = sum(
                1 if t.to is None else counts[t.to] for t in self.transitions[position]
            )
        return counts[self.start]

    def paths(self, *, max_paths: int = DEFAULT_MAX_PATHS) -> Iterator[SongPath]:
        """Every complete path, depth first. Fails before starting if there are too many."""
        total = self.path_count()
        if total > max_paths:
            raise ArrangementError(
                f"the song has {total} possible paths, more than the limit of {max_paths}; "
                "lower max_extensions or simplify the repeats and edges"
            )
        stack: list[tuple[tuple[Position, ...], float, Position | None]] = [((), 1.0, self.start)]
        while stack:
            prefix, prob, position = stack.pop()
            if position is None:
                yield SongPath(prefix, prob)
                continue
            walk = (*prefix, position)
            for t in reversed(self.transitions[position]):
                stack.append((walk, prob * t.prob, t.to))


@dataclass(frozen=True)
class _Exits:
    """What can happen at the end of a section's bars, before any cap is applied."""

    extend_prob: float
    repeat_prob: float
    jumps: tuple[tuple[str, float], ...]
    next_section: str | None
    """The section after this one, or ``None`` when this one ends the song."""
    remainder: float


def expand(song: Song, *, max_extensions: int = DEFAULT_MAX_EXTENSIONS) -> BarGrid:
    """Unroll ``song`` into its bar grid, following only cells the band can reach."""
    if max_extensions < 0:
        raise ValueError("max_extensions must be 0 or more")
    index_of = {s.id: i for i, s in enumerate(song.sections)}
    exits = {s.id: _exits(song, index_of, s) for s in song.sections}
    by_id = {s.id: s for s in song.sections}

    transitions: dict[Position, tuple[Transition, ...]] = {}
    truncated: set[Position] = set()
    pending = [Position(song.sections[0].id, 1, 1)]
    while pending:
        position = pending.pop()
        if position in transitions:
            continue
        found, capped = _transitions(by_id[position.section], exits, position, max_extensions)
        transitions[position] = found
        if capped:
            truncated.add(position)
        pending.extend(t.to for t in found if t.to is not None and t.to not in transitions)

    ordered = sorted(transitions, key=lambda p: (index_of[p.section], p.visit, p.bar))
    return BarGrid(
        song=song,
        positions=tuple(ordered),
        transitions={p: transitions[p] for p in ordered},
        truncated=frozenset(truncated),
    )


def _exits(song: Song, index_of: Mapping[str, int], section: Section) -> _Exits:
    where = f"section '{section.id}'"
    here = index_of[section.id]
    loop = next((e for e in song.graph if e.from_ == e.to == section.id), None)
    if loop is not None:
        if section.extendable and section.repeat:
            raise ArrangementError(
                f"{where}: the self-loop edge is ambiguous because the section is both "
                "extendable and repeat; remove the edge and give each its own prob"
            )
        declared = section.extendable or section.repeat
        if declared is None:
            raise ArrangementError(f"{where}: a self-loop edge needs extendable or repeat")
        if abs(loop.prob - declared.prob) > _EPSILON:
            kind = "extendable" if section.extendable else "repeat"
            raise ArrangementError(
                f"{where}: the self-loop edge has prob {loop.prob:g} but {kind}.prob is "
                f"{declared.prob:g}; they describe the same event, so make them match"
            )

    jumps: dict[str, float] = {}
    for edge in song.graph:
        if edge.from_ != section.id or edge.to == section.id:
            continue
        if index_of[edge.to] < here:
            raise ArrangementError(
                f"graph edge {edge.from_} -> {edge.to} goes back to an earlier section; "
                "v0 expansion supports only forward edges and self-loops"
            )
        jumps[edge.to] = jumps.get(edge.to, 0.0) + edge.prob

    extend_prob = section.extendable.prob if section.extendable else 0.0
    repeat_prob = section.repeat.prob if section.repeat else 0.0
    declared_total = extend_prob + repeat_prob + sum(jumps.values())
    if declared_total > 1 + _EPSILON:
        raise ArrangementError(
            f"{where}: the outcome probabilities (extension, repeat and graph edges) add up "
            f"to {declared_total:.3g}, more than 1"
        )
    following = song.sections[here + 1].id if here + 1 < len(song.sections) else None
    return _Exits(
        extend_prob=extend_prob,
        repeat_prob=repeat_prob,
        jumps=tuple(jumps.items()),
        next_section=None if section.ends else following,
        remainder=max(0.0, 1.0 - declared_total),
    )


def _exit_block(section: Section, bar: int) -> int | None:
    """0 at the planned end, ``k`` at the end of the k-th extension block, else ``None``."""
    if bar == section.bars:
        return 0
    if section.extendable is not None and bar > section.bars:
        extra, rest = divmod(bar - section.bars, section.extendable.unit)
        return extra if rest == 0 else None
    return None


def _transitions(
    section: Section,
    exits: Mapping[str, _Exits],
    position: Position,
    max_extensions: int,
) -> tuple[tuple[Transition, ...], bool]:
    block = _exit_block(section, position.bar)
    if block is None:
        nxt = Position(position.section, position.visit, position.bar + 1)
        return (Transition(nxt, 1.0, "bar"),), False

    here = exits[section.id]
    outcomes: dict[Position | None, tuple[float, Kind]] = {}

    def add(to: Position | None, prob: float, kind: Kind) -> None:
        if prob > 0:
            outcomes[to] = (outcomes.get(to, (0.0, kind))[0] + prob, kind)

    capped = here.extend_prob > 0 and block >= max_extensions
    if not capped:
        add(
            Position(position.section, position.visit, position.bar + 1), here.extend_prob, "extend"
        )
    if section.repeat is not None and position.visit <= section.repeat.max:
        add(Position(position.section, position.visit + 1, 1), here.repeat_prob, "repeat")
    for target, prob in here.jumps:
        add(Position(target, 1, 1), prob, "next" if target == here.next_section else "jump")
    if here.next_section is None:
        add(None, here.remainder, "end")
    else:
        add(Position(here.next_section, 1, 1), here.remainder, "next")

    total = sum(prob for prob, _ in outcomes.values())
    if total <= _EPSILON:
        raise ArrangementError(
            f"section '{section.id}': no way forward from bar {position.bar} of visit "
            f"{position.visit}; its extension limit or repeat maximum leaves no outcome"
        )
    found = tuple(Transition(to, prob / total, kind) for to, (prob, kind) in outcomes.items())
    return found, capped
