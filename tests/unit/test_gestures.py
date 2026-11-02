"""Tap and hold recognition from raw press and release edges (Section 5.3)."""

from typing import Any

import pytest

from roobotto.config.rig import Rig
from roobotto.control.gestures import Gestures
from spine_support import MS, rig, rig_doc

SW = "back_sw"


def run(gestures: Gestures, script: list[tuple[str, int]]) -> list[tuple[str, str, int]]:
    """Feed ('press'|'release'|'tick', ms) steps; return (action, event id, at ms) recognized."""
    out: list[tuple[str, str, int]] = []
    for n, (what, ms) in enumerate(script):
        t = ms * MS
        for g in gestures.due(t):
            out.append((g.action, g.event_id, g.at_ns // MS))
        if what == "press":
            gestures.press(SW, t, f"e{n}")
        elif what == "release":
            g, _ = gestures.release(SW, t, f"e{n}")
            if g is not None:
                out.append((g.action, g.event_id, g.at_ns // MS))
    return out


ROWS: list[tuple[str, list[tuple[str, int]], list[tuple[str, str, int]]]] = [
    ("quick tap is BACK, on release", [("press", 0), ("release", 120)], [("back", "e1", 120)]),
    ("tap at 499 ms is BACK", [("press", 0), ("release", 499)], [("back", "e1", 499)]),
    (
        "release at 500 ms is a hold, not a tap",
        [("press", 0), ("release", 500)],
        [("manual", "e0", 500)],
    ),
    (
        "hold is MANUAL when it reaches 500 ms, before release",
        [("press", 0), ("tick", 500), ("release", 900)],
        [("manual", "e0", 500)],
    ),
    (
        "hold fires once however long it is held",
        [("press", 0), ("tick", 600), ("tick", 5000), ("release", 6000)],
        [("manual", "e0", 500)],
    ),
    ("release with no press is ignored", [("release", 100)], []),
    (
        "a second press restarts the timing",
        [("press", 0), ("press", 400), ("release", 800)],
        [("back", "e2", 800)],
    ),
]


@pytest.mark.parametrize(("name", "script", "expected"), ROWS, ids=[r[0] for r in ROWS])
def test_gestures(
    name: str, script: list[tuple[str, int]], expected: list[tuple[str, str, int]]
) -> None:
    assert run(Gestures(rig()), script) == expected


def test_gap_between_tap_and_hold_is_neither() -> None:
    doc: dict[str, Any] = rig_doc()
    doc["timing"] = {"tap_max_ms": 400, "hold_ms": 600}
    g = Gestures(Rig.model_validate(doc))
    assert run(g, [("press", 0), ("release", 500)]) == []
    assert run(g, [("press", 1000), ("tick", 1600), ("release", 1700)]) == [("manual", "e0", 1600)]


def test_only_tap_and_hold_switches_are_handled_and_wakeup_is_the_hold_time() -> None:
    g = Gestures(rig())
    assert g.handles("back_sw") and not g.handles("go_sw") and not g.handles("stop_sw")
    assert g.next_wakeup_ns() is None
    g.press(SW, 100 * MS, "e1")
    assert g.next_wakeup_ns() == 600 * MS
    g.due(600 * MS)
    assert g.next_wakeup_ns() is None
