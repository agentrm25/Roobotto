"""Production-equivalent classes (T-03).

Two beat positions are production-equivalent when the band cannot tell them apart by what the
rig needs to do. For each way through the song (each path of the bar grid) that passes through a
position, take

- the **active state**: the value every device intent was last set to by a cue due before this
  beat (nothing if none), and
- the **upcoming cues**: the cues due from this beat up to ``horizon_beats`` later, with their
  distance in beats.

A position's signature is the set of those pairs over all paths through it. Equal signatures
mean one class. Notes on the definition:

- A cue is identified by what it asks for (class, state, ``on_miss``, ``requires``,
  ``on_uncertain``), not by its id or the section it is written under. Verse 1 and verse 2
  cues that ask for the same thing are interchangeable. Any difference in what is asked keeps
  two positions apart: the safe direction, since it only ever demands more certainty.
- Every visit of a section runs that section's cues, each at its offset from the start of the
  visit. A negative ``offset_beats`` can reach back into earlier sections, so such a cue shows
  up in the futures that enter the section and in no others.
- A cue due before the start of the performance never fires and is ignored.
- Cues due at the same instant keep the cue sheet's order; a later cue overrides an earlier
  one on the same intent.
- Path probabilities are ignored. Only what can happen matters here, not how likely it is.
"""

import json
from bisect import bisect_left
from dataclasses import dataclass
from fractions import Fraction
from functools import cached_property

from roobotto.arrangement import DEFAULT_MAX_PATHS, BarGrid, BeatPosition, SongPath
from roobotto.config.song import Cue

CueKey = str
RigState = frozenset[tuple[str, str]]
Upcoming = tuple[tuple[Fraction, CueKey], ...]
Signature = frozenset[tuple[RigState, Upcoming]]


@dataclass(frozen=True)
class EquivalenceClasses:
    """A partition of the grid's beat positions, in play order."""

    horizon_beats: int
    classes: tuple[tuple[BeatPosition, ...], ...]

    @cached_property
    def _index(self) -> dict[BeatPosition, int]:
        return {member: i for i, members in enumerate(self.classes) for member in members}

    def class_of(self, position: BeatPosition) -> int:
        """The index in ``classes`` of the class holding ``position``."""
        return self._index[position]

    def equivalent(self, a: BeatPosition, b: BeatPosition) -> bool:
        return self.class_of(a) == self.class_of(b)


@dataclass(frozen=True)
class _SheetCue:
    order: int
    section: str
    key: CueKey
    state: tuple[tuple[str, str], ...]
    anchor: Fraction
    """Beats from the start of the section visit to the cue; negative if it anticipates."""


def production_classes(
    grid: BarGrid, *, horizon_beats: int, max_paths: int = DEFAULT_MAX_PATHS
) -> EquivalenceClasses:
    """Group the grid's beat positions by active state and upcoming cues within the horizon."""
    if horizon_beats < 1:
        raise ValueError("horizon_beats must be at least 1")
    beats = grid.beats_per_bar
    sheet: dict[str, list[_SheetCue]] = {}
    for order, cue in enumerate(grid.song.cues):
        sheet.setdefault(cue.at.section, []).append(_sheet_cue(order, cue, beats))

    signatures: dict[BeatPosition, set[tuple[RigState, Upcoming]]] = {}
    for path in grid.paths(max_paths=max_paths):
        for where, pair in _walk(path, sheet, beats, horizon_beats):
            signatures.setdefault(where, set()).add(pair)

    groups: dict[Signature, list[BeatPosition]] = {}
    for where in grid.beat_positions():
        groups.setdefault(frozenset(signatures[where]), []).append(where)
    return EquivalenceClasses(horizon_beats, tuple(tuple(g) for g in groups.values()))


def _sheet_cue(order: int, cue: Cue, beats: int) -> _SheetCue:
    key = json.dumps(
        cue.model_dump(mode="json", by_alias=True, exclude={"id", "at"}), sort_keys=True
    )
    # Reading the float through its shortest decimal form keeps the beat arithmetic exact.
    offset = Fraction(str(cue.at.offset_beats))
    return _SheetCue(
        order=order,
        section=cue.at.section,
        key=key,
        state=tuple(cue.state.items()),
        anchor=(cue.at.bar - 1) * beats + (cue.at.beat - 1) + offset,
    )


def _walk(
    path: SongPath, sheet: dict[str, list[_SheetCue]], beats: int, horizon: int
) -> list[tuple[BeatPosition, tuple[RigState, Upcoming]]]:
    """Every beat of one path, with the rig state and upcoming cues seen at that beat."""
    due: list[tuple[Fraction, _SheetCue]] = []
    for index, position in enumerate(path.positions):
        if position.bar == 1:
            due.extend((index * beats + cue.anchor, cue) for cue in sheet.get(position.section, ()))
    due = sorted((item for item in due if item[0] >= 0), key=lambda item: (item[0], item[1].order))
    times = [when for when, _ in due]

    seen: list[tuple[BeatPosition, tuple[RigState, Upcoming]]] = []
    rig: dict[str, str] = {}
    applied = 0
    for beat_index in range(len(path.positions) * beats):
        while applied < len(due) and due[applied][0] < beat_index:
            rig.update(due[applied][1].state)
            applied += 1
        end = bisect_left(times, beat_index + horizon)
        upcoming = tuple((when - beat_index, cue.key) for when, cue in due[applied:end])
        where = BeatPosition(path.positions[beat_index // beats], beat_index % beats + 1)
        seen.append((where, (frozenset(rig.items()), upcoming)))
    return seen
