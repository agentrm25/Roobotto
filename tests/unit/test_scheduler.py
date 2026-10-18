"""Scheduler: prepare once, generations invalidate pending work (ADR-012, I-6, I-7)."""

from roobotto.cues.scheduler import Scheduler
from roobotto.messages import OccurrenceId
from spine_support import song

CUES = {c.id: c for c in song().cues}


def occ(cue_id: str, visit: int = 1) -> OccurrenceId:
    return OccurrenceId(performance="p", song_instance=0, section_visit=visit, cue_id=cue_id)


def test_an_occurrence_is_prepared_once() -> None:
    s = Scheduler()
    cue = CUES["chorus_scene"]
    item = s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None)
    assert item is not None
    assert s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None) is None
    s.schedule(item, due_ns=10, send_at_ns=5, window_end_ns=20, timing_class=None)
    s.commit(item)
    assert s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None) is None
    assert s.prepare(occ("chorus_scene", visit=2), cue, dict(cue.state), None, None) is not None


def test_cancel_all_bumps_generation_and_nothing_is_due() -> None:
    s = Scheduler()
    cue = CUES["chorus_scene"]
    item = s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None)
    assert item is not None
    s.schedule(item, due_ns=10, send_at_ns=5, window_end_ns=20, timing_class=None)
    assert s.due(5) == [item]
    assert s.cancel_all() == [item]
    assert s.generation == 1 and s.due(100) == [] and s.outcome(item.key) == "cancelled"


def test_cancel_device_removes_only_that_device() -> None:
    s = Scheduler()
    cue = CUES["chorus_scene"]
    item = s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None)
    assert item is not None
    s.schedule(item, due_ns=10, send_at_ns=5, window_end_ns=20, timing_class=None)
    touched = s.cancel_device("mvp")
    assert [(i.key, removed) for i, removed in touched] == [(item.key, ["mvp.scene"])]
    assert item.intents == {"aum.layer": "pad"}
    assert item.generation == s.generation == 1
    assert s.due(5) == [item]
    s.cancel_device("aum")
    assert s.outcome(item.key) == "cancelled" and s.due(5) == []


def test_stale_generation_items_are_never_due() -> None:
    s = Scheduler()
    cue = CUES["chorus_scene"]
    item = s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None)
    assert item is not None
    s.schedule(item, due_ns=10, send_at_ns=5, window_end_ns=20, timing_class=None)
    s.bump()  # not re-stamped
    assert s.due(5) == []


def test_snapshot_never_carries_the_generation_in_its_identity() -> None:
    s = Scheduler()
    cue = CUES["chorus_scene"]
    item = s.prepare(occ("chorus_scene"), cue, dict(cue.state), None, None)
    assert item is not None
    s.schedule(item, due_ns=10, send_at_ns=5, window_end_ns=20, timing_class=None)
    s.bump([item])
    snap = item.snapshot()
    assert snap.generation == 1 and "generation" not in snap.id.model_dump()
