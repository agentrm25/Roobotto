from typing import Any

import pytest
from pydantic import ValidationError

from roobotto.messages import (
    CueOccurrence,
    Decision,
    Event,
    FiredLedgerRecord,
    MusicalState,
    OccurrenceId,
    OutputAttempt,
)

OCC: dict[str, Any] = {
    "performance": "show_1",
    "song_instance": 0,
    "section_visit": 1,
    "cue_id": "chorus1_scene",
}
HASH = "a" * 64


def event(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": "ev1",
        "available_ns": 10,
        "source": "helix",
        "path": "usb",
        "kind": "control_change",
        "channel": 1,
        "data1": 69,
        "data2": 2,
        "provenance": "performer_intent",
    }
    return base | over


def test_event_accepts_a_mapped_performer_action() -> None:
    assert Event.model_validate(event(mapped_action="go")).mapped_action == "go"


@pytest.mark.parametrize("provenance", ["state_observation", "output_echo", "unknown"])
def test_only_performer_intent_can_map_to_an_action(provenance: str) -> None:
    with pytest.raises(ValidationError, match="requires provenance 'performer_intent'"):
        Event.model_validate(event(provenance=provenance, mapped_action="go"))


def test_event_provenance_is_required() -> None:
    data = event()
    del data["provenance"]
    with pytest.raises(ValidationError, match="provenance"):
        Event.model_validate(data)


MUSICAL_STATE: dict[str, Any] = {
    "tracker_session": "7f3c2a9e",
    "seq": 18231,
    "t_ns": 912345678901234,
    "song": {"id": "song_07", "p": 0.999},
    "mode": "following",
    "hypotheses": [
        {"section": "chorus1", "visit": 1, "bar": 7, "p": 0.93},
        {"section": "chorus2", "visit": 1, "bar": 7, "p": 0.05},
    ],
    "beat": {"phase": 0.62, "tempo_bpm": 127.4, "tempo_sd": 0.9},
    "next_downbeat": {"t_ns": 912346090000000, "sd_ms": 12},
    "boundary": {
        "t_ns": 912347965000000,
        "sd_ms": 35,
        "successors": {"verse2": 0.81, "bridge": 0.19},
    },
    "health": {"drums": "ok", "room": "ok", "vocal": "ok", "midi": "ok"},
}


def test_plan_musical_state_example_validates() -> None:
    state = MusicalState.model_validate(MUSICAL_STATE)
    assert (state.tracker_session, state.seq) == ("7f3c2a9e", 18231)


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"hypotheses": []}, "needs at least one hypothesis"),
        ({"beat": None}, "needs at least one hypothesis and a beat"),
        ({"next_downbeat": {"t_ns": 1, "sd_ms": 1}}, "next_downbeat is earlier"),
        ({"boundary": {"t_ns": 1, "sd_ms": 1}}, "boundary is earlier"),
        ({"tracker_session": "XYZ"}, "String should match pattern"),
        ({"seq": -1}, "greater than or equal to 0"),
    ],
)
def test_musical_state_rejections(change: dict[str, Any], message: str) -> None:
    with pytest.raises(ValidationError, match=message):
        MusicalState.model_validate(MUSICAL_STATE | change)


def occurrence(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "id": OCC,
        "generation": 3,
        "class": "standard",
        "intents": {"mvp.scene": "chorus"},
        "due_ns": 1000,
        "send_deadline_ns": 990,
        "lifecycle": "scheduled",
    }
    return base | over


def test_occurrence_identity_ignores_the_generation() -> None:
    first = CueOccurrence.model_validate(occurrence(generation=1))
    second = CueOccurrence.model_validate(occurrence(generation=9))
    assert first.id == second.id
    assert hash(first.id) == hash(second.id)
    assert first.id.key == "show_1/0/1/chorus1_scene"
    assert "generation" not in OccurrenceId.model_fields
    assert "generation" not in FiredLedgerRecord.model_fields


def test_a_new_section_visit_is_a_different_occurrence() -> None:
    assert OccurrenceId.model_validate(OCC) != OccurrenceId.model_validate(
        OCC | {"section_visit": 2}
    )


@pytest.mark.parametrize("lifecycle", ["committed", "sent"])
def test_critical_occurrence_needs_authorization_to_commit(lifecycle: str) -> None:
    data = occurrence(**{"class": "critical", "lifecycle": lifecycle})
    with pytest.raises(ValidationError, match="authorized it"):
        CueOccurrence.model_validate(data)
    data["authorized_by"] = {"event_id": "ev1", "action": "go"}
    assert CueOccurrence.model_validate(data).authorized_by is not None


def test_critical_occurrence_may_be_planned_without_authorization() -> None:
    CueOccurrence.model_validate(occurrence(**{"class": "critical", "lifecycle": "planned"}))


def test_send_deadline_cannot_follow_due_time() -> None:
    with pytest.raises(ValidationError, match="send_deadline_ns must not be after due_ns"):
        CueOccurrence.model_validate(occurrence(send_deadline_ns=2000))


def test_fire_decision_names_its_occurrence() -> None:
    with pytest.raises(ValidationError, match="must name the occurrence"):
        Decision.model_validate({"kind": "fire", "reason": "authorized GO"})
    Decision.model_validate({"kind": "fire", "reason": "authorized GO", "occurrence": OCC})
    Decision.model_validate({"kind": "abstain", "reason": "tracker-only critical request"})


def attempt(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "occurrence": OCC,
        "generation": 3,
        "adapter": "mock",
        "device": "mvp",
        "command": "mvp.scene",
        "value": "chorus",
        "send_time_ns": 995,
        "config_hash": HASH,
        "armed": False,
        "reason": "authorized GO",
        "result": "attempted",
    }
    return base | over


def test_mock_attempt_needs_no_arming() -> None:
    OutputAttempt.model_validate(attempt())


def test_physical_attempt_needs_arming() -> None:
    with pytest.raises(ValidationError, match="not armed"):
        OutputAttempt.model_validate(attempt(adapter="midi"))
    OutputAttempt.model_validate(attempt(adapter="midi", armed=True))


def test_attempt_requires_a_reason_and_config_hash() -> None:
    with pytest.raises(ValidationError):
        OutputAttempt.model_validate(attempt(reason=""))
    with pytest.raises(ValidationError):
        OutputAttempt.model_validate(attempt(config_hash="abc"))


def test_ledger_record() -> None:
    record = FiredLedgerRecord.model_validate(
        {"occurrence": OCC, "first_attempt_ns": 995, "result": "attempting"}
    )
    assert record.result == "attempting"
