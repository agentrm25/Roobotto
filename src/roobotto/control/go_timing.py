"""The GO timing policy (plan Section 8, audit F-10). Pure functions, tested from a table.

Each GO answers one question first: which downbeat does it belong to?

- **F, no lock.** There is no fresh, following tracker state for the song, or no downbeat is
  predicted within one bar. The press is the downbeat and entry cues go now.
- **L, late window.** The GO comes within the late window (default a quarter bar) after a
  predicted downbeat. That downbeat starts the section, and entry cues go now.
- Otherwise the GO belongs to the **next predicted downbeat**. For each entry cue,
  send deadline = downbeat - device latency - margin:
  - **Q**: the GO came before the deadline. The cue is sent at the deadline, so its effect lands
    on the downbeat.
  - **D**: the GO came after the deadline but before the downbeat. The cue's ``on_miss`` policy
    applies. It is never deferred to the next bar.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal

from roobotto.config.song import FireLateWithin, OnMiss, Song
from roobotto.messages import MusicalState

NS_PER_MINUTE = 60_000_000_000
GoClass = Literal["Q", "D", "L", "F"]


@dataclass(frozen=True)
class GoAnchor:
    kind: Literal["F", "L", "next"]
    go_ns: int
    """When the GO was authorized."""
    anchor_ns: int
    """The downbeat of the section's first bar."""
    beat_ns: int


def beat_ns_for(song: Song, state: MusicalState | None) -> int:
    """Beat length from the tracker's tempo, held inside the song's tempo range."""
    tempo = song.tempo.nominal
    if state is not None and state.beat is not None:
        low, high = song.tempo.range
        tempo = min(max(state.beat.tempo_bpm, low), high)
    return round(NS_PER_MINUTE / tempo)


def go_anchor(
    go_ns: int, state: MusicalState | None, song: Song, late_window_bars: float
) -> GoAnchor:
    """Where a GO authorized at ``go_ns`` puts the section's first downbeat.

    ``state`` must already be usable (fresh, following, this song), or ``None``.
    """
    beat_ns = beat_ns_for(song, state)
    if (
        state is None
        or state.mode != "following"
        or state.beat is None
        or state.next_downbeat is None
    ):
        return GoAnchor("F", go_ns, go_ns, beat_ns)
    bar_ns = beat_ns * song.beats_per_bar
    downbeat = state.next_downbeat.t_ns
    if downbeat <= go_ns:
        # An older packet: carry its prediction forward bar by bar until it is ahead of the GO.
        downbeat += ((go_ns - downbeat) // bar_ns + 1) * bar_ns
    if downbeat - go_ns > bar_ns:
        return GoAnchor("F", go_ns, go_ns, beat_ns)
    previous = downbeat - bar_ns
    if go_ns - previous <= round(late_window_bars * bar_ns):
        return GoAnchor("L", go_ns, previous, beat_ns)
    return GoAnchor("next", go_ns, downbeat, beat_ns)


@dataclass(frozen=True)
class EntryTiming:
    timing_class: GoClass
    due_ns: int
    """When the effect should land."""
    send_at_ns: int
    """The send deadline."""


def entry_timing(anchor: GoAnchor, lead_ns: int) -> EntryTiming:
    """Timing of a cue at the very start of the section (bar 1, beat 1, no offset).

    ``lead_ns`` is the device latency plus the send margin.
    """
    if anchor.kind in ("F", "L"):
        return EntryTiming(anchor.kind, anchor.go_ns + lead_ns, anchor.go_ns)
    send_at = anchor.anchor_ns - lead_ns
    timing_class: GoClass = "Q" if anchor.go_ns <= send_at else "D"
    return EntryTiming(timing_class, anchor.anchor_ns, send_at)


def miss_window_end(
    on_miss: OnMiss,
    *,
    due_ns: int,
    send_at_ns: int,
    beat_ns: int,
    beats_per_bar: int,
    late_window_bars: float,
    margin_ns: int,
) -> int:
    """The last moment ``on_miss`` allows a send (I-4)."""
    if isinstance(on_miss, FireLateWithin):
        return due_ns + round(on_miss.fire_late_within_beats * beat_ns)
    if on_miss == "send_now":
        return due_ns + round(late_window_bars * beats_per_bar * beat_ns)
    # skip: the latest send that still lands on time; the margin absorbs jitter.
    return send_at_ns + margin_ns


def go_class(entry_classes: Sequence[str], anchor: GoAnchor) -> GoClass:
    """One class for the GO record: F or L, else Q if every entry cue met its deadline, else D."""
    if anchor.kind in ("F", "L"):
        return anchor.kind
    return "D" if "D" in entry_classes else "Q"
