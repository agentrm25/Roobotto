"""Deterministic replay (ADR-014, I-19) and the future-read guard (I-9)."""

import json
from pathlib import Path

import pytest

from roobotto.adapters.midi import MidiAdapter
from roobotto.engine.clock import SimClock
from roobotto.engine.guard import FutureReadError, FutureReadGuard, GuardedSeries
from roobotto.engine.inputs import MidiIn
from roobotto.engine.spine import ReplayMismatchError, Runner, Spine, replay
from roobotto.engine.trace import TraceError, TraceWriter, load_trace, parse_trace
from roobotto.record.jsonl import JsonlLog
from spine_support import BAR_MS, MS, RecordingBackend, World, show


def messy_session(start_ms: int = 1_000) -> World:
    """One run with loss, reordering, simultaneous manual input, staleness, a tracker restart
    and a clock jump."""
    w = World(show(milestone=5), clock=SimClock(start_ms * MS))
    w.select()
    w.press()
    w.wait(100)
    w.press()  # verse
    w.op("set_mode", "assisted")
    w.track(seq=1, section="verse", bar=1, downbeat_in_ms=BAR_MS - 150)
    w.op("enable_tracker")
    # Loss: sequence numbers 2-4 never arrive.
    w.wait(20)
    w.track(seq=5, section="verse", bar=1, downbeat_in_ms=BAR_MS - 170)
    # Reordering: 7 arrives before 6.
    w.wait(20)
    w.track(seq=7, section="verse", bar=1, downbeat_in_ms=BAR_MS - 190)
    w.track(seq=6, section="verse", bar=1, downbeat_in_ms=BAR_MS - 190)
    # A press stamped before engine time (delivered late).
    late = MidiIn.model_validate(
        {
            "t_ns": w.clock.now_ns() - 15 * MS,
            "port": "mock_in_helix",
            "kind": "control_change",
            "channel": 1,
            "data1": 69,
            "data2": 5,
        }
    )
    w.runner.submit(late)
    # Simultaneous manual input: a GO press and a tracker packet with the same timestamp.
    w.wait(1500)
    w.track(seq=8, section="verse", bar=2, downbeat_in_ms=300)
    w.press()  # chorus, same instant
    # Staleness: nothing from the tracker for 400 ms with authority on.
    w.wait(400)
    # Tracker restart: a new session.
    w.track(session="bbbbbbbb", seq=1, section="chorus", bar=1, downbeat_in_ms=500)
    w.track(session="aaaaaaaa", seq=9, section="chorus", bar=1, downbeat_in_ms=500)  # old session
    w.op("enable_tracker")
    w.op("set_mode", "assisted")
    w.track(session="bbbbbbbb", seq=2, section="solo", bar=1, p=0.99, downbeat_in_ms=BAR_MS - 50)
    # Clock jump backwards.
    w.clock.jump_to(w.clock.now_ns() - 300 * MS)
    w.runner.tick()
    w.wait(800)
    w.press()  # authorizes a proposal or advances
    w.runner.port("mock_out_mvp", False)
    w.runner.port("mock_out_mvp", True)
    w.press("stop")
    w.op("confirm_preset_b")
    w.op("resume")
    w.wait(500)
    w.press()
    w.wait(2500)
    return w


def test_messy_session_exercises_what_it_claims() -> None:
    w = messy_session()
    reasons = " ".join(str(r.get("reason", "")) for r in w.spine.records)
    assert "only the newest matters" in reasons  # reordering and duplicates dropped
    assert "earlier tracker session" in reasons
    assert "new tracker session" in reasons
    assert "stale tracker stream" in reasons
    assert "engine time never moves back" in reasons
    assert w.records("stop") and w.records("resume") and w.attempts


def test_replay_gives_the_same_normalized_hash() -> None:
    w = messy_session()
    replayed = replay(w.show, w.runner.trace.trace())
    assert replayed.decision_hash() == w.spine.decision_hash()
    assert replayed.records == w.spine.records


def test_replay_from_file(tmp_path: Path) -> None:
    w = messy_session()
    path = tmp_path / "logs" / "run.trace.jsonl"
    path.parent.mkdir()
    path.write_text("\n".join(w.runner.trace.lines) + "\n")
    assert replay(w.show, load_trace(path)).decision_hash() == w.spine.decision_hash()


def test_hash_ignores_the_clock_origin_and_run_id() -> None:
    a = messy_session(start_ms=1_000)
    b = messy_session(start_ms=987_654)
    assert a.spine.decision_hash() == b.spine.decision_hash()


def test_hash_changes_when_decisions_change() -> None:
    a = World.make()
    a.select()
    a.press()
    b = World.make()
    b.select()
    b.press()
    b.press()
    assert a.spine.decision_hash() != b.spine.decision_hash()


def test_replay_after_restart_uses_the_recorded_ledger() -> None:
    first = World.make()
    first.select()
    first.press()
    first.wait(100)
    first.press()
    first.wait(100)
    first.press()  # chorus sent
    second = first.restart()
    second.select()
    second.press()
    second.wait(100)
    second.press()
    second.wait(3000)
    trace = second.runner.trace.trace()
    assert trace.header.initial_ledger
    assert replay(second.show, trace).decision_hash() == second.spine.decision_hash()


def test_replay_refuses_a_different_config() -> None:
    w = World.make()
    w.select()
    with pytest.raises(ReplayMismatchError):
        replay(show(milestone=2), w.runner.trace.trace())


def test_replay_never_reaches_hardware() -> None:
    backend = RecordingBackend()
    w = World(show("physical"), midi=MidiAdapter(backend))
    w.op("arm")
    w.select()
    w.press()
    w.wait(100)
    w.press()
    w.wait(100)
    w.press()  # chorus: synth.patch to the (fake) MIDI backend
    assert len(backend.sent) == 1
    replayed = replay(w.show, w.runner.trace.trace())
    assert replayed.decision_hash() == w.spine.decision_hash()
    assert len(backend.sent) == 1
    assert [a.adapter for a in replayed.attempts] == [a.adapter for a in w.attempts]


def test_trace_rejects_garbage() -> None:
    with pytest.raises(TraceError):
        parse_trace([])
    with pytest.raises(TraceError):
        parse_trace(['{"type": "tick", "t_ns": 1}'])


def test_runner_writes_trace_and_jsonl_logs(tmp_path: Path) -> None:
    s = show()
    spine = Spine(s)
    clock = SimClock(5 * MS)
    trace_path = tmp_path / "logs" / "r1.trace.jsonl"
    log_path = tmp_path / "logs" / "r1.jsonl"
    runner = Runner(
        spine,
        clock,
        trace=TraceWriter(trace_path, spine.header()),
        log=JsonlLog(log_path, run_id="r1", config_hash=spine.config_hash),
    )
    seen: list[str] = []
    runner.subscribe(lambda r: seen.append(r["type"]))
    runner.operator("select_song", "spine_test")
    runner.midi("mock_in_switches", "note_on", 16, 60, 127)
    runner.trace.close()
    runner.log.close()
    lines = [json.loads(line) for line in log_path.read_text().splitlines()]
    assert lines and all({"run_id", "config_hash", "t_ns", "type"} <= set(line) for line in lines)
    assert all(line["run_id"] == "r1" and line["config_hash"] == s.config_hash for line in lines)
    assert seen == [line["type"] for line in lines]
    assert len(load_trace(trace_path).inputs) == 2


# --------------------------------------------------------------------------------- I-9


def test_i9_guard_refuses_reads_after_now() -> None:
    guard = FutureReadGuard()
    series: GuardedSeries[str] = GuardedSeries(guard)
    for t in (10, 20, 30):
        series.append(t, f"block{t}")
    with pytest.raises(FutureReadError):
        series.read_until(10)  # before any input: "now" is undefined
    guard.advance(20)
    assert list(series.read_until(20)) == ["block10", "block20"]
    with pytest.raises(FutureReadError):
        series.read_until(21)
    guard.advance(5)  # a clock jump back never reopens the future
    assert guard.now_ns == 20
    with pytest.raises(ValueError):
        series.append(15, "out of order")
