"""The GO timing policy (plan Section 8): a table of presses and the class each lands in."""

from typing import Any

import pytest

from roobotto.config.song import FireLateWithin
from roobotto.control.go_timing import (
    GoAnchor,
    entry_timing,
    go_anchor,
    go_class,
    miss_window_end,
)
from spine_support import MS, song, tracker_state

SONG = song()  # 120 BPM, 4/4: a beat is 500 ms and a bar 2000 ms
T0 = 10_000 * MS
LATE = 0.25  # a quarter bar: 500 ms
LEAD = 5 * MS  # no device latency, 5 ms margin


def state(downbeat_in_ms: int | None, **over: Any):
    return tracker_state(seq=1, t_ns=T0, downbeat_in_ms=downbeat_in_ms, **over)


# name, tracker state, expected entry class, expected anchor (ms from the GO)
ROWS: list[tuple[str, Any, str, int]] = [
    ("no tracker: unlocked", None, "F", 0),
    ("tracker lost: unlocked", state(1000, mode="lost"), "F", 0),
    ("no downbeat predicted", state(None), "F", 0),
    ("next downbeat more than a bar away", state(2500), "F", 0),
    ("early press", state(1000), "Q", 1000),
    ("press exactly at the send deadline", state(5), "Q", 5),
    ("press after the send deadline, before the downbeat", state(3), "D", 3),
    ("press on the downbeat", state(0), "L", 0),
    ("press inside the late window", state(1600), "L", -400),
    ("press at the end of the late window", state(1500), "L", -500),
    ("press just past the late window", state(1499), "Q", 1499),
]


@pytest.mark.parametrize(
    ("name", "tracker", "expected", "anchor_ms"), ROWS, ids=[r[0] for r in ROWS]
)
def test_press_lands_in_the_right_class(
    name: str, tracker: Any, expected: str, anchor_ms: int
) -> None:
    anchor = go_anchor(T0, tracker, SONG, LATE)
    entry = entry_timing(anchor, LEAD)
    assert entry.timing_class == expected
    assert anchor.anchor_ns == T0 + anchor_ms * MS
    assert go_class([entry.timing_class], anchor) == expected
    if expected in ("F", "L"):
        assert entry.send_at_ns == T0, "unlocked and late presses are sent now"
    else:
        assert entry.due_ns == anchor.anchor_ns and entry.send_at_ns == anchor.anchor_ns - LEAD


def test_older_packet_is_carried_forward_by_whole_bars() -> None:
    old = tracker_state(seq=1, t_ns=T0 - 100 * MS, downbeat_in_ms=50)  # downbeat 50 ms ago
    anchor = go_anchor(T0, old, SONG, LATE)
    assert (anchor.kind, anchor.anchor_ns) == ("L", T0 - 50 * MS)


def test_tracker_tempo_is_held_inside_the_song_range() -> None:
    fast = state(1000)
    fast = fast.model_copy(update={"beat": fast.beat.model_copy(update={"tempo_bpm": 200.0})})  # type: ignore[union-attr]
    assert go_anchor(T0, fast, SONG, LATE).beat_ns == round(60_000 * MS / 140)


@pytest.mark.parametrize(
    ("on_miss", "sends"),
    [("skip", False), ("send_now", True), (FireLateWithin(fire_late_within_beats=1), True)],
    ids=["skip", "send_now", "fire_late_within_1_beat"],
)
def test_deadline_missed_follows_on_miss_and_is_never_deferred_a_bar(
    on_miss: Any, sends: bool
) -> None:
    # A device with 20 ms latency: lead is 25 ms. The press comes 10 ms before the downbeat, so
    # any send now lands 10 ms late. Only on_miss decides whether it still goes.
    lead = 25 * MS
    anchor = go_anchor(T0, state(10), SONG, LATE)
    entry = entry_timing(anchor, lead)
    assert entry.timing_class == "D"
    assert entry.due_ns == T0 + 10 * MS, "the downbeat right after the press, not the next bar"
    window = miss_window_end(
        on_miss,
        due_ns=entry.due_ns,
        send_at_ns=entry.send_at_ns,
        beat_ns=anchor.beat_ns,
        beats_per_bar=4,
        late_window_bars=LATE,
        margin_ns=5 * MS,
    )
    assert (window >= T0) is sends


def test_within_the_margin_a_d_press_still_lands_on_time() -> None:
    """No latency, 3 ms before the downbeat: past the deadline, yet a send now lands on time."""
    anchor = go_anchor(T0, state(3), SONG, LATE)
    entry = entry_timing(anchor, LEAD)
    assert entry.timing_class == "D"
    window = miss_window_end(
        "skip",
        due_ns=entry.due_ns,
        send_at_ns=entry.send_at_ns,
        beat_ns=anchor.beat_ns,
        beats_per_bar=4,
        late_window_bars=LATE,
        margin_ns=5 * MS,
    )
    assert window >= T0


def test_go_class_is_d_if_any_entry_cue_missed_its_deadline() -> None:
    anchor = GoAnchor("next", T0, T0 + 10 * MS, 500 * MS)
    assert go_class(["Q", "Q"], anchor) == "Q"
    assert go_class(["Q", "D"], anchor) == "D"
