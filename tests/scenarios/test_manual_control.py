"""T-05 acceptance: manual control and the GO timing policy (plan Appendix B, Sections 5.3, 8).

Table-driven where the card asks for it. The BACK/MANUAL switch is ``back_sw``: a tap (released
before 500 ms) is BACK, a 500 ms hold is MANUAL.
"""

from collections.abc import Callable
from copy import deepcopy
from typing import Any

import pytest

from roobotto.config.rig import Rig
from roobotto.config.song import Song
from roobotto.engine.show import ShowConfig
from spine_support import BAR_MS, MS, SONG_DOC, World, rig_doc, show

SECTIONS = ["intro", "verse", "chorus", "solo", "outro"]


def tap(w: World, ms: int = 100) -> None:
    w.press("back")
    w.wait(ms)
    w.release("back")


def hold(w: World, ms: int = 600) -> None:
    w.press("back")
    w.wait(ms)
    w.release("back")


def visits(w: World) -> list[tuple[str, int, str]]:
    return [(r["section"], r["visit"], r["via"]) for r in w.records("visit")]


def keys(w: World, cue: str | None = None) -> list[str]:
    return [a.occurrence.key for a in w.attempts if cue is None or a.occurrence.cue_id == cue]


def to_verse(w: World) -> None:
    w.select()
    w.press()
    w.wait(100)
    w.press()


def go_records(w: World) -> list[dict[str, Any]]:
    return w.records("go")


# ---------------------------------------------------------- 1. BACK and MANUAL never issue GO


def _stage_none(w: World) -> None:
    pass


def _stage_selected(w: World) -> None:
    w.select()


def _stage_intro(w: World) -> None:
    w.select()
    w.press()


def _stage_verse_pending(w: World) -> None:
    to_verse(w)
    w.wait(200)


def _stage_chorus_sent(w: World) -> None:
    to_verse(w)
    w.wait(200)
    w.press()


def _stage_quantized_pending(w: World) -> None:
    to_verse(w)
    w.wait(200)
    w.track(section="verse", bar=1, downbeat_in_ms=1000)
    w.press()


def _stage_stopped(w: World) -> None:
    to_verse(w)
    w.press("stop")


def _stage_after_resume(w: World) -> None:
    to_verse(w)
    w.press("stop")
    w.op("confirm_preset_b")
    w.op("resume")


STAGES: dict[str, Callable[[World], None]] = {
    "no song": _stage_none,
    "song selected": _stage_selected,
    "intro": _stage_intro,
    "verse, cue pending": _stage_verse_pending,
    "chorus just sent": _stage_chorus_sent,
    "quantized GO pending": _stage_quantized_pending,
    "stopped": _stage_stopped,
    "after RESUME": _stage_after_resume,
}


@pytest.mark.parametrize("gesture", ["tap", "hold"])
@pytest.mark.parametrize("stage", STAGES)
def test_back_and_manual_never_issue_go(stage: str, gesture: str) -> None:
    w = World.make(milestone=5)
    STAGES[stage](w)
    gos = len(go_records(w))
    before = visits(w)
    current = before[-1][0] if before else None
    (tap if gesture == "tap" else hold)(w)
    w.wait(3000)
    assert len(go_records(w)) == gos, "a gesture must never issue a GO"
    new = visits(w)[len(before) :]
    assert all(via != "go" for _, _, via in new)
    if gesture == "hold":
        assert new == [], "MANUAL never changes the section"
    for section, _, via in new:
        assert via == "back"
        assert current is not None and SECTIONS.index(section) <= SECTIONS.index(current)


def test_a_back_press_edge_alone_does_nothing() -> None:
    w = World.make()
    to_verse(w)
    before = len(w.spine.records)
    w.press("back")
    assert [r["type"] for r in w.spine.records[before:]] == ["event"]


# ----------------------------------------------- 2. early, late and unlocked presses


@pytest.mark.parametrize(
    ("tracker", "expected"),
    [
        (None, "F"),
        ({"mode": "lost", "downbeat_in_ms": 1000}, "F"),
        ({"downbeat_in_ms": 2500}, "F"),
        ({"song_id": "other_song", "downbeat_in_ms": 1000}, "F"),
        ({"downbeat_in_ms": 1000}, "Q"),
        ({"downbeat_in_ms": 1600}, "L"),
        ({"downbeat_in_ms": 0}, "L"),
    ],
    ids=[
        "no tracker",
        "tracker lost",
        "downbeat too far",
        "other song",
        "early",
        "late",
        "on the downbeat",
    ],
)
def test_presses_land_in_the_right_class(tracker: dict[str, Any] | None, expected: str) -> None:
    w = World.make()
    to_verse(w)
    w.wait(200)
    if tracker is not None:
        w.track(section="verse", bar=1, **tracker)
    w.press()
    assert go_records(w)[-1]["timing_class"] == expected


def test_quantized_go_lands_on_the_downbeat_and_late_go_is_sent_now() -> None:
    w = World.make()
    to_verse(w)
    w.wait(200)
    w.track(section="verse", bar=1, downbeat_in_ms=1000)
    t = w.clock.now_ns()
    w.press()
    w.wait(1500)
    sent = {a.send_time_ns for a in w.attempts if a.occurrence.cue_id == "chorus_scene"}
    assert sent == {t + 1000 * MS - 5 * MS}, "sent at the deadline: the downbeat minus the margin"

    w2 = World.make()
    to_verse(w2)
    w2.wait(200)
    w2.track(section="verse", bar=1, downbeat_in_ms=1700)
    t2 = w2.clock.now_ns()
    w2.press()
    assert {a.send_time_ns for a in w2.attempts if a.occurrence.cue_id == "chorus_scene"} == {t2}


# ------------------------------------- 3. a missed deadline follows on_miss, never a bar later


def _latency_show(on_miss: Any) -> ShowConfig:
    doc: dict[str, Any] = rig_doc()
    mvp = next(d for d in doc["devices"] if d["id"] == "mvp")
    mvp["commands"]["scene"]["latency_ms"] = 20
    song_doc: dict[str, Any] = deepcopy(SONG_DOC)
    cue = next(c for c in song_doc["cues"] if c["id"] == "chorus_scene")
    cue["on_miss"] = on_miss
    base = show()
    return ShowConfig(
        Rig.model_validate(doc),
        {"spine_test": Song.model_validate(song_doc)},
        base.setlist,
        base.settings,
    )


@pytest.mark.parametrize(
    ("on_miss", "sends"),
    [("skip", False), ("send_now", True), ({"fire_late_within_beats": 1}, True)],
    ids=["skip", "send_now", "fire_late_within_1_beat"],
)
def test_deadline_missed_follows_on_miss_and_is_never_deferred(on_miss: Any, sends: bool) -> None:
    w = World(_latency_show(on_miss))
    to_verse(w)
    w.wait(200)
    w.track(section="verse", bar=1, downbeat_in_ms=10)  # 20 ms latency + 5 ms margin: missed
    t = w.clock.now_ns()
    w.press()
    assert go_records(w)[-1]["timing_class"] == "D"
    w.wait(2 * BAR_MS + 500)
    sent = [a.send_time_ns for a in w.attempts if a.occurrence.cue_id == "chorus_scene"]
    assert sent == ([t, t] if sends else []), "sent at once or not at all, never a bar later"


# --------------------------------------------- 4. BACK before the send deadline cancels the GO


def test_back_before_the_send_deadline_cancels_the_go_with_no_output() -> None:
    w = World.make()
    to_verse(w)
    verse_anchor = w.records("visit")[-1]["anchor_ns"]
    w.wait(200)
    w.track(section="verse", bar=1, downbeat_in_ms=1000)
    w.press()  # GO into the chorus, quantized: nothing sent yet
    assert visits(w)[-1][:2] == ("chorus", 1)
    tap(w)  # released well before the deadline
    [back] = [r for r in w.records("back") if r["event"] == "go_cancelled"]
    assert (back["section"], back["visit"]) == ("verse", 1)
    assert back["reinstated"] == ["rehearsal/0/1/verse_look"]
    w.wait(3000)
    assert [a.occurrence.cue_id for a in w.attempts] == ["verse_look"], "no chorus output at all"
    assert w.attempts[0].send_time_ns == verse_anchor + BAR_MS * MS - 5 * MS
    assert w.spine.engine.visit is not None and w.spine.engine.visit.section == "verse"


# ---------------------------------------------- 5. BACK after the send restores as new occurrences


def test_back_after_the_send_restores_the_previous_section_as_new_occurrences() -> None:
    w = World.make()
    to_verse(w)
    w.wait(200)
    w.press()  # chorus, sent at once (no lock)
    w.wait(300)
    w.press()  # solo, sent at once
    w.wait(300)
    tap(w)
    assert visits(w)[-1] == ("chorus", 2, "back")
    chorus = sorted({k for k in keys(w) if "chorus" in k})
    assert chorus == sorted(
        f"rehearsal/0/{v}/{c}"
        for v in (1, 2)
        for c in ("chorus_scene", "chorus_synth", "chorus_lights")
    )
    [record] = [r for r in w.records("back") if r["event"] == "reentered"]
    commits = [r for r in w.records("commit") if r["occurrence"]["section_visit"] == 2]
    assert commits and all(r["authorized_by"]["action"] == "back" for r in commits)
    assert all(r["authorized_by"]["event_id"] == record["event_id"] for r in commits)


def test_back_cannot_go_before_the_first_section() -> None:
    w = World.make()
    w.select()
    w.press()  # intro, sent... nothing to send in the intro
    w.wait(200)
    tap(w)  # nothing from the GO was sent: the GO into the intro is cancelled
    assert w.spine.engine.visit is None
    tap(w)
    assert w.records("action_refused")[-1]["action"] == "back"


# ---------------------------------------------- 6. a hold while a cue is pending toggles MANUAL


def _keep_tracking(w: World, ms: int, downbeat_abs_ns: int, section: str = "verse") -> None:
    """Advance ``ms`` with a fresh packet every 50 ms pointing at one absolute downbeat."""
    end = w.clock.now_ns() + ms * MS
    while w.clock.now_ns() < end:
        w.wait(50)
        ahead = max(0, (downbeat_abs_ns - w.clock.now_ns()) // MS)
        w.track(section=section, bar=2, downbeat_in_ms=ahead)


def test_hold_while_a_cue_is_pending_toggles_manual_without_firing_anything() -> None:
    w = World.make(milestone=5)
    to_verse(w)
    w.op("set_mode", "assisted")
    w.track(section="verse", bar=1, downbeat_in_ms=1200)  # mid-bar: a quantized GO
    w.op("enable_tracker")
    downbeat = w.clock.now_ns() + 1200 * MS
    w.press()  # GO into the chorus, quantized for that downbeat
    pending = {i.key for i in w.spine.engine.scheduler.pending()}
    assert pending and w.attempts == []
    w.press("back")
    _keep_tracking(w, 600, downbeat, "verse")  # before the downbeat the band is still in the verse
    engine = w.spine.engine
    [off] = [r for r in w.records("manual") if r["event"] == "authority_off"]
    at_hold = [r for r in w.spine.records if r["t_ns"] == off["t_ns"]]
    assert not {"commit", "output_attempt", "cancel", "visit"} & {r["type"] for r in at_hold}
    assert {i.key for i in engine.scheduler.pending()} == pending
    assert not engine.tracker.authority and engine.mode == "manual"
    w.release("back")
    assert w.attempts == []
    # A second hold turns back on the authority the performer's own hold turned off.
    w.press("back")
    _keep_tracking(w, 600, downbeat, "verse")
    w.release("back")
    assert engine.tracker.authority and engine.mode == "assisted"


def test_manual_before_m3_is_logged_with_no_effect() -> None:
    w = World.make(milestone=2)
    to_verse(w)
    hold(w)
    assert w.records("manual")[-1]["event"] == "no_effect"


def test_manual_cannot_restore_authority_the_system_suspended() -> None:
    w = World.make(milestone=5)
    to_verse(w)
    w.op("set_mode", "assisted")
    w.track(section="verse", bar=1)
    w.op("enable_tracker")
    w.wait(300)  # the stream goes stale: the system suspends authority
    assert not w.spine.engine.tracker.authority
    w.press("back")
    _keep_tracking(w, 600, w.clock.now_ns() + 2000 * MS)
    w.release("back")
    assert not w.spine.engine.tracker.authority
    assert "only the operator" in w.records("action_refused")[-1]["reason"]


# ------------------------------------------------------------ 7. STOP works at every stage


def _back_switch_down(w: World) -> None:
    to_verse(w)
    w.press("back")
    w.wait(200)


def _hold_fired_switch_down(w: World) -> None:
    to_verse(w)
    w.press("back")
    w.wait(600)


def _late_go(w: World) -> None:
    to_verse(w)
    w.wait(200)
    w.track(section="verse", bar=1, downbeat_in_ms=1700)
    w.press()


def _after_back_cancel(w: World) -> None:
    _stage_quantized_pending(w)
    tap(w)


def _after_back_reentry(w: World) -> None:
    _stage_chorus_sent(w)
    w.wait(200)
    tap(w)


STOP_STAGES: dict[str, Callable[[World], None]] = {
    "BACK switch held down": _back_switch_down,
    "hold fired, switch still down": _hold_fired_switch_down,
    "quantized GO pending": _stage_quantized_pending,
    "late GO just sent": _late_go,
    "after BACK cancelled a GO": _after_back_cancel,
    "after BACK re-entered": _after_back_reentry,
    "verse cue pending": _stage_verse_pending,
}


@pytest.mark.parametrize("stage", STOP_STAGES)
def test_stop_works_at_every_stage(stage: str) -> None:
    w = World.make(milestone=5)
    STOP_STAGES[stage](w)
    sent = len(w.attempts)
    w.press("stop")  # the press edge alone: no release
    engine = w.spine.engine
    assert engine.stopped and engine.scheduler.pending() == [] and engine.proposal is None
    w.release("back")
    tap(w)
    hold(w)
    w.press()
    w.wait(3000)
    assert len(w.attempts) == sent
    assert engine.scheduler.pending() == []


def test_tracker_relocalization_never_moves_a_quantized_gos_entry_cues() -> None:
    """Found while writing T-05: the GO timing policy places entry cues; the tracker re-times
    only the bar-counted cues after them."""
    w = World.make(milestone=5)
    to_verse(w)
    w.op("set_mode", "assisted")
    w.track(section="verse", bar=1, downbeat_in_ms=1200)
    w.op("enable_tracker")
    t = w.clock.now_ns()
    w.press()  # chorus, quantized for t + 1200 ms
    w.wait(100)
    # The tracker claims the chorus began a bar earlier: a relocalization within the visit.
    w.track(section="chorus", bar=1, downbeat_in_ms=1100)
    assert w.records("relocalize")
    w.wait(1500)
    sent = {a.send_time_ns for a in w.attempts if a.occurrence.cue_id == "chorus_scene"}
    assert sent == {t + 1200 * MS - 5 * MS}
