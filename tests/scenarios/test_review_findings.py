"""Regression tests for the outside review of PR #1 and PR #2 (six findings)."""

from pathlib import Path
from typing import Any

from roobotto.adapters.midi import MidiAdapter
from roobotto.adapters.mock import MockAdapter
from roobotto.config.rig import Rig
from roobotto.cues.ledger import FiredLedger, LedgerCorruptError
from roobotto.cues.supervisor import Commit, Intent, Supervisor
from roobotto.engine.show import ShowConfig
from roobotto.messages import Authorization, FiredLedgerRecord, OccurrenceId, OutputAttempt
from spine_support import BAR_MS, MS, World, rig_doc, show


def visits(w: World) -> list[tuple[str, int]]:
    return [(r["section"], r["visit"]) for r in w.records("visit")]


def to_verse(w: World) -> None:
    w.select()
    w.press()
    w.wait(100)
    w.press()


# 1 ----------------------------------------------------------------------------------------


def _show_where_a_scene_looks_like_stop() -> ShowConfig:
    """The MVP's chorus scene happens to be the same bytes as the STOP switch (note 62, ch 16)."""
    doc: dict[str, Any] = rig_doc()
    mvp = next(d for d in doc["devices"] if d["id"] == "mvp")
    mvp["receives"] = [{"port": "mock_out_mvp", "channel": 16, "behavior": "intended"}]
    mvp["commands"]["scene"]["channel"] = 16
    mvp["commands"]["scene"]["values"]["chorus"] = {"kind": "note_on", "data1": 62, "data2": 127}
    base = show()
    return ShowConfig(Rig.model_validate(doc), base.songs, base.setlist, base.settings)


def test_unrelated_output_never_suppresses_stop() -> None:
    w = World(_show_where_a_scene_looks_like_stop())
    to_verse(w)
    w.wait(500)
    w.press()  # chorus: Roobotto sends note_on ch16 62 127 to the MVP
    assert [a.bytes_sent for a in w.sent("mvp.scene")] == [[0x9F, 62, 127]]
    w.wait(50)
    w.press("stop")  # same bytes, but from the footswitch input
    assert w.spine.engine.stopped


def test_echo_is_scoped_to_the_device_that_reports_through_the_source() -> None:
    """The Helix's echo of Roobotto's own CC69 is still an echo."""
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.press()
    w.wait(500)
    w.press()  # solo: guitar.snapshot=lead, CC69 value 3
    w.wait(50)
    [event] = [r for r in w.snapshot(4) if r["type"] == "event"]
    assert event["provenance"] == "output_echo"


# 2 ----------------------------------------------------------------------------------------


def test_automated_preset_load_starts_the_initialization_window() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.runner.midi("mock_in_helix", "program_change", 1, 5, None, automated=True)
    w.wait(10)
    [event] = [r for r in w.snapshot(2) if r["type"] == "event"]
    assert event["detail"] == "snapshot_preset_load" and event["mapped_action"] is None
    assert visits(w)[-1] == ("verse", 1)


def test_duplicate_path_preset_load_starts_the_initialization_window() -> None:
    w = World.make()
    to_verse(w)
    w.wait(500)
    w.program(5, port="mock_in_helix_h4")
    w.wait(10)
    w.snapshot(2)
    assert visits(w)[-1] == ("verse", 1)


# 3 ----------------------------------------------------------------------------------------


def test_lost_tracker_state_never_fires_and_suspends_authority() -> None:
    w = World.make(milestone=5)
    w.select()
    w.press()  # intro via GO
    w.op("set_mode", "assisted")
    w.track(section="intro", bar=1, downbeat_in_ms=1000)
    w.op("enable_tracker")
    w.wait(20)
    w.track(section="verse", bar=1, p=0.97, downbeat_in_ms=BAR_MS - 50)
    assert visits(w)[-1] == ("verse", 1)  # tracker-entered; verse_look pending for bar 2
    t_enter = w.clock.now_ns()
    while w.clock.now_ns() < t_enter + 2500 * MS:
        w.track(section="verse", bar=1, p=0.97, mode="lost", downbeat_in_ms=BAR_MS)
        w.wait(20)
    assert w.sent("mvp.scene") == []
    assert not w.spine.engine.tracker.authority and w.spine.engine.mode == "manual"


# 4 ----------------------------------------------------------------------------------------


def test_resume_sends_nothing_before_the_next_authorized_go() -> None:
    w = World.make(milestone=5)
    to_verse(w)
    w.op("set_mode", "assisted")
    w.wait(200)
    w.press("stop")
    w.op("confirm_preset_b")
    w.op("resume")
    w.track(section="verse", bar=2, downbeat_in_ms=1000)
    w.op("enable_tracker")
    assert w.spine.engine.tracker.authority
    w.wait(20)
    for _ in range(50):
        w.track(section="chorus", bar=1, p=0.99, downbeat_in_ms=BAR_MS - 50)
        w.wait(20)
    assert w.attempts == []
    assert visits(w)[-1] == ("verse", 1)
    w.press()  # the next authorized GO, quantized to the tracker's predicted downbeat
    w.wait(2100)
    assert {a.occurrence.cue_id for a in w.attempts} == {
        "chorus_scene",
        "chorus_synth",
        "chorus_lights",
    }
    assert not w.spine.engine.awaiting_go


# 5 ----------------------------------------------------------------------------------------


def _rec(cue: str) -> FiredLedgerRecord:
    occ = OccurrenceId(performance="show", song_instance=0, section_visit=1, cue_id=cue)
    return FiredLedgerRecord(occurrence=occ, first_attempt_ns=1, result="attempting")


def test_torn_tail_is_repaired_before_new_appends(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    FiredLedger(path).append(_rec("a"))
    with path.open("a") as handle:
        handle.write('{"record": "fired", "occurrence": {"perf')
    ledger = FiredLedger(path)
    assert ledger.dropped_torn_line
    ledger.append(_rec("b"))
    ledger.append(_rec("c"))
    reloaded = FiredLedger(path)
    assert not reloaded.dropped_torn_line
    assert all(reloaded.contains(_rec(c).occurrence) for c in "abc")


def test_complete_last_record_without_newline_is_kept(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    FiredLedger(path).append(_rec("a"))
    path.write_text(path.read_text().rstrip("\n"))
    ledger = FiredLedger(path)
    assert ledger.contains(_rec("a").occurrence) and not ledger.dropped_torn_line
    ledger.append(_rec("b"))
    assert FiredLedger(path).contains(_rec("b").occurrence)


def test_corrupt_terminated_last_line_refuses_to_start(tmp_path: Path) -> None:
    """A torn write never ends in a newline; a terminated bad line is corruption."""
    path = tmp_path / "l.jsonl"
    FiredLedger(path).append(_rec("a"))
    with path.open("a") as handle:
        handle.write("garbage\n")
    try:
        FiredLedger(path)
    except LedgerCorruptError:
        return
    raise AssertionError("a corrupt, newline-terminated last line must stop the load")


# 6 ----------------------------------------------------------------------------------------


def _commit(*intents: tuple[str, str, str], cue: str) -> Commit:
    occ = OccurrenceId(performance="p", song_instance=0, section_visit=1, cue_id=cue)
    return Commit(
        occurrence=occ,
        generation=0,
        cue_class="standard",
        intents=tuple(Intent(*i) for i in intents),
        reason="test",
        window_end_ns=10**12,
        authorized_by=Authorization(event_id="e1", action="go"),
    )


def _supervisor(the_rig: Rig) -> Supervisor:
    return Supervisor(
        the_rig, FiredLedger(), mock=MockAdapter(), midi=MidiAdapter(), config_hash="c" * 64
    )


def test_rate_limit_is_per_device_across_commands() -> None:
    doc: dict[str, Any] = rig_doc()
    synth = next(d for d in doc["devices"] if d["id"] == "synth")
    synth["commands"]["mode"] = {
        "type": "state",
        "port": "mock_out_synth",
        "channel": 2,
        "values": {"mono": {"kind": "control_change", "data1": 7, "data2": 1}},
        "status": "documented",
        "evidence": {"source": "E-01"},
        "min_interval_ms": 250,
    }
    two_commands = Rig.model_validate(doc)

    def submit(sup: Supervisor, c: Commit, now: int) -> tuple[list[Any], list[Any]]:
        result = sup.submit(c, now_ns=now, generation=0, stopped=False, armed=False)
        return result.attempts, result.refusals

    sup = _supervisor(two_commands)
    attempts, _ = submit(sup, _commit(("synth", "patch", "bright"), cue="a"), 10**9)
    assert len(attempts) == 1
    attempts, refusals = submit(sup, _commit(("synth", "mode", "mono"), cue="b"), 10**9 + 1)
    assert attempts == [] and "rate limit" in refusals[0].reason
    attempts, _ = submit(sup, _commit(("synth", "mode", "mono"), cue="c"), 10**9 + 250 * MS)
    assert len(attempts) == 1

    # Within one cue the second command is spaced out, not dropped (ADR-017). The device's
    # 250 ms rate limit is longer than the 10 ms spacing, so it sets the gap.
    sup = _supervisor(two_commands)
    both = _commit(("synth", "patch", "bright"), ("synth", "mode", "mono"), cue="d")
    result = sup.submit(both, now_ns=10**9, generation=0, stopped=False, armed=False)
    assert [a.command for a in result.attempts] == ["synth.patch"]
    assert result.refusals == []
    assert [(d.intent, d.send_at_ns) for d in result.deferred] == [("synth.mode", 10**9 + 250 * MS)]
    flush_args: dict[str, Any] = {
        "generation": 0,
        "stopped": False,
        "armed": False,
        "overridden": frozenset[str](),
    }
    assert sup.flush(now_ns=10**9 + 249 * MS, **flush_args) == []
    [(_, attempt)] = sup.flush(now_ns=10**9 + 250 * MS, **flush_args)
    assert isinstance(attempt, OutputAttempt) and attempt.command == "synth.mode"


def test_operator_cannot_enable_a_lost_tracker() -> None:
    """Found by the strengthened state machine after the review."""
    w = World.make(milestone=5)
    w.select()
    w.track(section="intro", mode="lost")
    w.op("enable_tracker")
    assert not w.spine.engine.tracker.authority
    w.track(section="intro", song_id="other_song")
    w.op("enable_tracker")
    assert not w.spine.engine.tracker.authority
    w.track(section="intro")
    w.op("enable_tracker")
    assert w.spine.engine.tracker.authority
