"""The decision gate (Sections 5.1, 5.4; ADR-013)."""

from itertools import product
from typing import Any

import pytest

from roobotto.cues.gate import DeviceView, GateInput, decide
from roobotto.messages import Authorization, OccurrenceId
from spine_support import song

SONG = song()
CUES = {c.id: c for c in SONG.cues}
OCC = OccurrenceId(performance="p", song_instance=0, section_visit=1, cue_id="x")
GO = Authorization(event_id="e1", action="go")


def gate(cue_id: str, **over: Any) -> GateInput:
    cue = CUES[cue_id]
    base: dict[str, Any] = {
        "posterior": 0.99,
        "cue": cue,
        "devices": {k.split(".")[0]: DeviceView("roobotto") for k in cue.state},
        "now_ns": 100,
        "occurrence": OCC,
        "window_end_ns": 200,
        "authorization": None,
        "mode": "assisted",
        "milestone": 8,
        "tracker_authority": True,
        "tracker_fresh": True,
        "stopped": False,
    }
    return GateInput(**(base | over))


@pytest.mark.parametrize(
    ("mode", "milestone", "posterior", "authority"),
    list(product(["manual", "assisted", "auto"], range(1, 9), [0.0, 0.99, 1.0], [True, False])),
)
def test_tracker_only_critical_is_refused_everywhere(
    mode: str, milestone: int, posterior: float, authority: bool
) -> None:
    """I-14 / ADR-013: no mode, milestone, posterior or fresh packet fires a critical cue."""
    decision = decide(
        gate(
            "solo_lead",
            mode=mode,
            milestone=milestone,
            posterior=posterior,
            tracker_authority=authority,
        )
    )
    assert decision.kind != "fire"
    assert decision.kind == "ask"  # the cue's on_uncertain
    assert "ADR-013" in decision.reason


def test_authorized_critical_fires() -> None:
    assert decide(gate("solo_lead", authorization=GO)).kind == "fire"


@pytest.mark.parametrize(
    ("over", "reason"),
    [
        ({"stopped": True}, "STOP"),
        ({"now_ns": 201}, "expired"),
        ({"devices": {"guitar": DeviceView("performer")}}, "owned by the performer"),
        ({"devices": {"guitar": DeviceView("missing")}}, "not in the rig"),
        ({"devices": {"guitar": DeviceView("roobotto", overridden=True)}}, "I-12"),
    ],
)
def test_vetoes_beat_authorization(over: dict[str, Any], reason: str) -> None:
    decision = decide(gate("solo_lead", authorization=GO, **over))
    assert decision.kind == "abstain" and reason in decision.reason


@pytest.mark.parametrize(
    ("cue_id", "over", "kind"),
    [
        ("verse_look", {"mode": "manual"}, "abstain"),
        ("verse_look", {"tracker_authority": False}, "abstain"),
        ("verse_look", {"tracker_fresh": False}, "abstain"),
        ("verse_look", {"milestone": 3}, "abstain"),
        ("verse_look", {"milestone": 4}, "fire"),
        ("verse_look", {"milestone": 4, "posterior": 0.2}, "hold"),
        ("chorus_scene", {"milestone": 4}, "abstain"),
        ("chorus_scene", {"milestone": 5}, "fire"),
        ("chorus_scene", {"milestone": 5, "posterior": 0.79}, "hold"),
        ("chorus_scene", {"milestone": 5, "posterior": None}, "hold"),
        ("chorus_scene", {"mode": "auto", "milestone": 5}, "fire"),
    ],
)
def test_tracker_only_noncritical(cue_id: str, over: dict[str, Any], kind: str) -> None:
    assert decide(gate(cue_id, **over)).kind == kind


def test_gate_is_pure() -> None:
    g = gate("chorus_scene", milestone=5)
    assert decide(g) == decide(g)
