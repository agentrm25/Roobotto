"""Two commands to one device in one cue are spaced out, never dropped silently (ADR-017)."""

from copy import deepcopy
from typing import Any

from roobotto.config.song import Song
from roobotto.engine.show import ShowConfig
from roobotto.engine.spine import replay
from roobotto.messages import FiredLedgerRecord
from spine_support import MS, SONG_DOC, World, show

OCC = "rehearsal/0/1/chorus_lights"


def to_chorus(w: World) -> int:
    """Select, then GO three times; returns the clock time of the chorus GO."""
    w.select()
    w.press()
    w.wait(100)
    w.press()
    w.wait(100)
    w.press()
    return w.clock.now_ns()


def lights(w: World) -> list[tuple[str, int]]:
    return [(a.command, a.send_time_ns) for a in w.attempts if a.device == "lights"]


def ledger_results(w: World, key: str) -> list[str]:
    return [
        r.result
        for r in w.ledger.records()
        if isinstance(r, FiredLedgerRecord) and r.occurrence.key == key
    ]


def test_second_command_goes_ten_ms_later() -> None:
    w = World.make()
    t = to_chorus(w)
    assert lights(w) == [("lights.color", t)]
    assert ledger_results(w, OCC) == ["attempting"]
    w.wait(50)
    assert lights(w) == [("lights.color", t), ("lights.strobe", t + 10 * MS)]
    assert ledger_results(w, OCC) == ["attempting", "attempted"]
    [deferred] = w.records("deferred")
    assert deferred["intent"] == "lights.strobe" and deferred["send_at_ns"] == t + 10 * MS


def test_stop_inside_the_gap_drops_the_deferred_send() -> None:
    w = World.make()
    to_chorus(w)
    w.wait(5)
    w.press("stop")
    w.wait(100)
    assert [c for c, _ in lights(w)] == ["lights.color"]
    assert any(
        "STOP" in r["reason"] for r in w.records("refusal") if r["intent"] == "lights.strobe"
    )
    assert ledger_results(w, OCC)[-1] != "attempting", "the ledger closes the occurrence"


def test_leaving_the_section_inside_the_gap_drops_it() -> None:
    w = World.make()
    to_chorus(w)
    w.wait(5)
    w.press()  # GO into the solo: a new generation
    w.wait(100)
    assert [c for c, _ in lights(w)] == ["lights.color"]


def test_port_disconnect_inside_the_gap_drops_it() -> None:
    w = World.make()
    to_chorus(w)
    w.wait(5)
    w.runner.port("mock_out_lights", False)
    w.wait(100)
    assert [c for c, _ in lights(w)] == ["lights.color"]


def test_restart_inside_the_gap_never_resends() -> None:
    w = World.make()
    to_chorus(w)
    w.wait(5)
    r = w.restart()
    r.wait(1000)
    assert lights(r) == []
    assert ledger_results(r, OCC) == ["attempting"], (
        "at most once: the strobe is lost, not repeated"
    )


def test_spacing_past_a_skip_window_is_refused() -> None:
    doc: dict[str, Any] = deepcopy(SONG_DOC)
    cue = next(c for c in doc["cues"] if c["id"] == "chorus_lights")
    cue["on_miss"] = "skip"
    base = show()
    w = World(
        ShowConfig(base.rig, {"spine_test": Song.model_validate(doc)}, base.setlist, base.settings)
    )
    to_chorus(w)
    w.wait(50)
    assert [c for c, _ in lights(w)] == ["lights.color"]
    [refusal] = [r for r in w.records("refusal") if r["intent"] == "lights.strobe"]
    assert "on_miss window" in refusal["reason"]


def test_deferred_sends_replay_identically() -> None:
    w = World.make()
    to_chorus(w)
    w.wait(500)
    assert len(lights(w)) == 2
    assert replay(w.show, w.runner.trace.trace()).decision_hash() == w.spine.decision_hash()


def test_performer_change_inside_the_gap_drops_it() -> None:
    from roobotto.adapters.midi import MidiAdapter
    from roobotto.adapters.mock import MockAdapter
    from roobotto.cues.ledger import FiredLedger
    from roobotto.cues.supervisor import Commit, Intent, Supervisor
    from roobotto.messages import Authorization, OccurrenceId

    sup = Supervisor(
        show().rig, FiredLedger(), mock=MockAdapter(), midi=MidiAdapter(), config_hash="c" * 64
    )
    occ = OccurrenceId(performance="p", song_instance=0, section_visit=1, cue_id="x")
    commit = Commit(
        occurrence=occ,
        generation=0,
        cue_class="standard",
        intents=(Intent("lights", "color", "warm"), Intent("lights", "strobe", "hit")),
        reason="test",
        window_end_ns=10**12,
        authorized_by=Authorization(event_id="e1", action="go"),
    )
    assert (
        len(sup.submit(commit, now_ns=10**9, generation=0, stopped=False, armed=False).deferred)
        == 1
    )
    [(_, dropped)] = sup.flush(
        now_ns=10**9 + 10 * MS,
        generation=0,
        stopped=False,
        armed=False,
        overridden=frozenset({"lights"}),
    )
    assert "I-12" in getattr(dropped, "reason", "")
