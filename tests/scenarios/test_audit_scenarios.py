"""T-04 audit scenarios, named after the audit finding each covers (plan Section 12).

F-6 STOP vs RESTORE, F-7 cue identity, F-8 provenance, F-9 critical authority. Gesture tests
(F-1) and the GO timing table (F-10) arrive with T-05.
"""

from copy import deepcopy
from typing import Any

import pytest

from roobotto.config.song import Song
from roobotto.cues.supervisor import Commit, Intent
from roobotto.engine.inputs import MidiIn
from roobotto.engine.show import ShowConfig
from spine_support import BAR_MS, MS, SONG_DOC, World, show


def keys(world: World, command: str | None = None) -> list[str]:
    return [a.occurrence.key for a in world.sent(command)]


def visits(world: World) -> list[tuple[str, int]]:
    return [(r["section"], r["visit"]) for r in world.records("visit")]


def to_verse(world: World) -> None:
    world.select()
    world.press()
    world.wait(100)
    world.press()


def assisted(world: World, section: str, bar: int = 1, downbeat_in_ms: int = 100) -> None:
    """ASSISTED with tracker authority, following ``section``."""
    world.op("set_mode", "assisted")
    world.track(section=section, bar=bar, downbeat_in_ms=downbeat_in_ms)
    world.op("enable_tracker")
    assert world.spine.engine.tracker.authority


def repeating_song_show() -> ShowConfig:
    doc: dict[str, Any] = deepcopy(SONG_DOC)
    chorus = next(s for s in doc["sections"] if s["id"] == "chorus")
    chorus["repeat"] = {"max": 1, "prob": 0.3}
    doc["graph"] = [{"from": "chorus", "to": "chorus", "prob": 0.3}]
    base = show()
    return ShowConfig(
        base.rig, {"spine_test": Song.model_validate(doc)}, base.setlist, base.settings
    )


# ---------------------------------------------------------------------------- F-7 identity


def test_f7_cancel_reschedule_relocalize_reconnect_same_visit_no_second_one_shot() -> None:
    w = World.make(milestone=5)
    to_verse(w)
    w.wait(2100)
    assert keys(w, "mvp.scene") == ["rehearsal/0/1/verse_look"]
    sent_occurrence = w.sent("mvp.scene")[0].occurrence

    # Reschedule by relocalizing within the same visit: the tracker puts us back in bar 1, so
    # verse_look's time would be in the future again.
    assisted(w, "verse", bar=1, downbeat_in_ms=1000)
    w.track(section="verse", bar=1, downbeat_in_ms=950)
    assert w.records("relocalize"), "the tracker should have relocalized"
    # Cancel: a manual action on the device, and a new generation.
    w.snapshot(6)
    # Reconnect the output port.
    w.runner.port("mock_out_mvp", False)
    w.runner.port("mock_out_mvp", True)
    w.wait(3000)
    assert keys(w, "mvp.scene") == ["rehearsal/0/1/verse_look"]

    # Even a commit forced past the scheduler is refused by the fired ledger.
    forced = Commit(
        occurrence=sent_occurrence,
        generation=w.spine.engine.scheduler.generation,
        cue_class="decorative",
        intents=(Intent("mvp", "scene", "verse_b"),),
        reason="forced",
        window_end_ns=10**15,
        authorized_by=None,
    )
    attempts, refusals = w.spine.supervisor.submit(
        forced, now_ns=w.clock.now_ns(), generation=forced.generation, stopped=False, armed=False
    )
    assert attempts == [] and "fired ledger" in refusals[0].reason


def test_f7_a_new_visit_fires_once() -> None:
    w = World(repeating_song_show())
    to_verse(w)
    w.wait(1000)
    w.snapshot(2)  # verse -> chorus, visit 1
    w.wait(1000)
    w.snapshot(2)  # chorus -> chorus (repeat), visit 2
    w.wait(3000)
    assert visits(w)[-2:] == [("chorus", 1), ("chorus", 2)]
    assert keys(w, "mvp.scene") == [
        "rehearsal/0/1/chorus_scene",
        "rehearsal/0/2/chorus_scene",
    ]
    assert keys(w, "synth.patch") == [
        "rehearsal/0/1/chorus_synth",
        "rehearsal/0/2/chorus_synth",
    ]


def test_f7_restart_stays_unarmed_and_builds_no_past_work() -> None:
    w = World.make("physical")
    w.op("arm")
    to_verse(w)
    w.wait(1000)  # verse_look still pending at the "crash"
    w.press()  # chorus: sent
    sent_before = set(keys(w))
    assert "rehearsal/0/1/chorus_synth" in sent_before

    r = w.restart()
    assert not r.spine.engine.armed
    assert r.spine.engine.scheduler.pending() == [] and r.spine.engine.song is None
    r.wait(5000)
    assert r.attempts == []

    r.select()
    assert r.spine.engine.song_instance == 1
    r.press()
    r.wait(100)
    r.press()
    r.wait(100)
    r.press()
    r.wait(100)
    assert not set(keys(r)) & sent_before
    assert all(a.occurrence.song_instance == 1 for a in r.attempts)
    assert all(a.adapter == "mock" for a in r.attempts), "arming must not survive a restart"


# --------------------------------------------------------------------------- F-8 provenance


def test_f8_one_action_on_two_paths_advances_once() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.press()
    w.press(port="mock_in_switches_h4")
    assert visits(w) == [("intro", 1), ("verse", 1), ("chorus", 1)]
    w.wait(1000)
    w.snapshot(3)
    w.snapshot(3, port="mock_in_helix_h4")
    assert visits(w)[-1] == ("solo", 1) and len(visits(w)) == 4
    dup = [r for r in w.records("event") if r["detail"] == "duplicate_path"]
    assert len(dup) == 2 and all(r["mapped_action"] is None for r in dup)


@pytest.mark.parametrize("snapshot", [1, 4, 5, 8])
def test_f8_unmapped_snapshot_within_a_section_does_not_advance(snapshot: int) -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.snapshot(snapshot)
    w.wait(500)
    assert visits(w) == [("intro", 1), ("verse", 1)]


def _authorized_by(w: World, event_id: str) -> list[Any]:
    found: list[Any] = []
    for r in w.records("commit"):
        auth: dict[str, Any] = r["authorized_by"] or {}
        if auth.get("event_id") == event_id:
            found.append(r)
    return found


def test_f8_echo_never_authorizes() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.press()  # chorus
    w.wait(500)
    w.press()  # solo: Roobotto sends guitar.snapshot=lead (CC69 value 3)
    assert keys(w, "guitar.snapshot") == ["rehearsal/0/1/solo_lead"]
    w.wait(100)
    records = w.snapshot(4)  # the Helix echoes value 3: snapshot 4, mapped solo -> outro
    [event] = [r for r in records if r["type"] == "event"]
    assert event["provenance"] == "output_echo" and event["mapped_action"] is None
    assert visits(w)[-1] == ("solo", 1)
    assert _authorized_by(w, event["id"]) == []


def test_f8_preset_load_recall_never_authorizes() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.program(5)  # a preset change that selects no setlist entry
    w.wait(300)
    records = w.snapshot(2)  # inside the 500 ms initialization window
    [event] = [r for r in records if r["type"] == "event"]
    assert event["detail"] == "snapshot_preset_load" and event["mapped_action"] is None
    assert visits(w)[-1] == ("verse", 1)
    w.wait(300)
    w.snapshot(2)  # outside the window: a performer press
    assert visits(w)[-1] == ("chorus", 1)


def test_f8_automated_recall_never_authorizes() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    records = w.snapshot(2, automated=True)
    [event] = [r for r in records if r["type"] == "event"]
    assert event["detail"] == "automated" and event["mapped_action"] is None
    assert visits(w)[-1] == ("verse", 1) and _authorized_by(w, event["id"]) == []


def _tracker_into_solo(w: World) -> None:
    """In the chorus, ASSISTED; the tracker moves into the solo, 100 ms into its first bar."""
    to_verse(w)
    w.wait(500)
    w.press()  # chorus via GO
    w.wait(200)
    assisted(w, "chorus", bar=2, downbeat_in_ms=BAR_MS - 50)
    w.wait(50)
    w.track(section="solo", bar=1, downbeat_in_ms=BAR_MS - 100)
    assert visits(w)[-1] == ("solo", 1)


def test_f8_press_before_proposal_never_authorizes_it() -> None:
    w = World.make(milestone=5)
    _tracker_into_solo(w)
    [armed] = [r for r in w.records("proposal") if r["event"] == "armed"]
    assert armed["occurrence"]["cue_id"] == "solo_lead"
    # A press made 30 ms before the proposal was armed, delivered late.
    late = MidiIn.model_validate(
        {
            "t_ns": armed["armed_ns"] - 30 * MS,
            "port": "mock_in_switches",
            "kind": "note_on",
            "channel": 16,
            "data1": 60,
            "data2": 127,
        }
    )
    w.runner.submit(late)
    refused = [r for r in w.records("proposal") if r["event"] == "not_authorized"]
    assert refused and "before the proposal was armed" in refused[0]["reason"]
    w.wait(2000)
    assert keys(w, "guitar.snapshot") == []


def test_f8_one_press_never_authorizes_a_different_critical_action() -> None:
    w = World.make(milestone=5)
    _tracker_into_solo(w)
    w.wait(20)
    first = w.press()
    [event] = [r for r in first if r["type"] == "event"]
    w.wait(20)
    assert keys(w, "guitar.snapshot") == ["rehearsal/0/1/solo_lead"]
    assert visits(w)[-1] == ("solo", 1), "authorizing the proposal must not also advance"
    w.wait(900)  # solo_pad (beat 3, due 900 ms into the visit) is now proposed
    assert [r["occurrence"]["cue_id"] for r in w.records("proposal") if r["event"] == "armed"] == [
        "solo_lead",
        "solo_pad",
    ]
    assert "rehearsal/0/1/solo_pad" not in keys(w, "aum.layer")
    assert [r["occurrence"]["cue_id"] for r in _authorized_by(w, event["id"])] == ["solo_lead"]
    second = w.press()
    [event2] = [r for r in second if r["type"] == "event"]
    w.wait(20)
    assert keys(w, "aum.layer")[-1] == "rehearsal/0/1/solo_pad"
    assert [r["occurrence"]["cue_id"] for r in _authorized_by(w, event2["id"])] == ["solo_pad"]


# --------------------------------------------------------------------- F-9 critical authority


@pytest.mark.parametrize("milestone", range(1, 9))
@pytest.mark.parametrize("mode", ["manual", "assisted", "auto"])
def test_f9_tracker_only_critical_refused_in_every_mode_and_milestone(
    mode: str, milestone: int
) -> None:
    w = World.make(milestone=milestone)
    to_verse(w)
    w.wait(500)
    w.press()  # chorus via GO
    w.wait(200)
    w.op("set_mode", mode)
    w.track(section="chorus", bar=2, downbeat_in_ms=BAR_MS - 50)
    w.op("enable_tracker")
    w.wait(50)
    # A perfectly confident, perfectly fresh tracker packet, repeated through the solo.
    for _ in range(40):
        w.track(section="solo", bar=1, p=1.0, downbeat_in_ms=BAR_MS - 100)
        w.wait(20)
    assert keys(w, "guitar.snapshot") == []
    assert keys(w, "aum.layer") == ["rehearsal/0/1/chorus_scene"]
    critical = [
        r
        for r in w.records("decision")
        if r.get("occurrence", {}).get("cue_id") in ("solo_lead", "solo_pad")
    ]
    assert all(r["kind"] != "fire" for r in critical)
    if mode != "manual" and milestone >= 4:
        # The tracker really did drive the engine into the solo; the critical cues were only
        # proposed, never fired.
        assert ("solo", 1) in visits(w)
        assert {r["kind"] for r in critical} == {"ask"}
    else:
        assert ("solo", 1) not in visits(w)


# ------------------------------------------------------------------- F-6 STOP and RESUME


def _stage_none(w: World) -> None:
    pass


def _stage_selected(w: World) -> None:
    w.select()


def _stage_pending(w: World) -> None:
    to_verse(w)
    w.wait(500)
    assert w.spine.engine.scheduler.pending()


def _stage_just_sent(w: World) -> None:
    to_verse(w)
    w.wait(500)
    w.press()


def _stage_quantized(w: World) -> None:
    to_verse(w)
    w.wait(500)
    w.track(section="verse", bar=2, downbeat_in_ms=400)
    w.press()  # Q: scheduled for the downbeat 400 ms ahead
    assert any(i.timing_class == "Q" for i in w.spine.engine.scheduler.pending())


def _stage_proposal(w: World) -> None:
    _tracker_into_solo(w)
    assert w.spine.engine.proposal is not None


STAGES = {
    "nothing": _stage_none,
    "song-selected": _stage_selected,
    "cue-pending": _stage_pending,
    "just-sent": _stage_just_sent,
    "quantized-go-pending": _stage_quantized,
    "proposal-armed": _stage_proposal,
}


@pytest.mark.parametrize("stage", STAGES)
def test_f6_stop_at_every_stage_leaves_nothing_pending(stage: str) -> None:
    w = World.make(milestone=5)
    STAGES[stage](w)
    before = len(w.attempts)
    generation = w.spine.engine.scheduler.generation
    w.press("stop")
    engine = w.spine.engine
    assert engine.stopped and engine.scheduler.pending() == [] and engine.proposal is None
    assert not engine.tracker.authority
    assert engine.scheduler.generation > generation
    w.press()  # GO is refused while stopped
    w.snapshot(3)
    w.op("resync")
    w.wait(5000)
    assert len(w.attempts) == before
    assert engine.scheduler.pending() == []


def test_f6_resume_releases_nothing_stale() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)  # verse_look pending for bar 2
    w.press("stop")
    w.op("resume")
    assert w.spine.engine.stopped, "RESUME without confirming preset B is refused"
    w.op("confirm_preset_b")
    w.op("resume")
    engine = w.spine.engine
    assert not engine.stopped
    assert all(v is None for copy in engine.devices.values() for v in copy.values())
    assert not engine.tracker.authority
    w.op("resync")
    w.wait(3000)  # past verse_look's time
    assert keys(w) == []
    w.press()  # the next authorized GO sends the target section's entry state, as new occurrences
    w.wait(100)
    assert keys(w, "mvp.scene") == ["rehearsal/0/1/chorus_scene"]


def test_f6_resume_needs_stop_first() -> None:
    w = World.make()
    w.op("confirm_preset_b")
    w.op("resume")
    refused = [
        r for r in w.records("action_refused") if r["action"] in ("resume", "confirm_preset_b")
    ]
    assert len(refused) == 2


# --------------------------------------------------------- I-3 / I-12 performer wins


def test_i3_manual_change_cancels_pending_automation_for_that_device() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.press()  # chorus
    w.wait(500)
    w.track(section="chorus", bar=2, downbeat_in_ms=400)
    w.press()  # solo, Q: guitar.snapshot and aum.layer scheduled for the downbeat
    w.wait(100)
    w.snapshot(6)  # the performer changes the guitar before the downbeat
    w.wait(2000)
    assert keys(w, "guitar.snapshot") == []
    assert keys(w, "aum.layer")[-1] == "rehearsal/0/1/solo_pad"
    cancels = [r for r in w.records("cancel") if "I-3" in r["reason"]]
    assert cancels and cancels[0]["intents"] == ["guitar.snapshot"]


def test_stage1_snapshot_go_leaves_helix_cues_to_the_performer() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.press()  # chorus
    w.wait(500)
    w.snapshot(3)  # the guitarist's mapped snapshot press: GO into the solo
    w.wait(2000)
    assert visits(w)[-1] == ("solo", 1)
    assert keys(w, "guitar.snapshot") == []
    # solo_pad accepts only a footswitch as authorization, so a snapshot GO cannot authorize it.
    assert keys(w, "aum.layer") == ["rehearsal/0/1/chorus_scene"]


def test_tracker_reporting_another_song_never_moves_the_engine() -> None:
    w = World.make(milestone=5)
    to_verse(w)
    w.wait(200)
    assisted(w, "verse", bar=1, downbeat_in_ms=1500)
    for _ in range(5):
        w.track(section="chorus", bar=1, p=1.0, song_id="other_song", downbeat_in_ms=1900)
        w.wait(20)
    assert visits(w)[-1] == ("verse", 1)


def test_f7_restart_never_reuses_an_unfired_song_instance() -> None:
    """ADR-016: a song selected but never fired before a restart still uses up its number."""
    w = World.make()
    to_verse(w)  # verse_look planned for instance 0, never fired
    assert w.attempts == []
    r = w.restart()
    r.select()
    assert r.spine.engine.song_instance == 1
    r.press()
    r.wait(100)
    r.press()
    r.wait(2100)
    assert keys(r, "mvp.scene") == ["rehearsal/1/1/verse_look"]
