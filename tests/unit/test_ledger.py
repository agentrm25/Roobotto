"""The fired ledger: persisted before the send, loaded at start, at most once (Section 5.5)."""

from pathlib import Path

import pytest

from roobotto.cues.ledger import FiredLedger, LedgerCorruptError
from roobotto.messages import FiredLedgerRecord, OccurrenceId


def rec(cue: str = "a", instance: int = 0, result: str = "attempting") -> FiredLedgerRecord:
    occ = OccurrenceId(performance="show", song_instance=instance, section_visit=1, cue_id=cue)
    return FiredLedgerRecord.model_validate(
        {"occurrence": occ, "first_attempt_ns": 5, "result": result}
    )


def test_append_is_on_disk_before_returning(tmp_path: Path) -> None:
    path = tmp_path / "logs" / "show.ledger.jsonl"
    ledger = FiredLedger(path)
    ledger.append(rec())
    assert path.read_text().count("\n") == 1
    assert FiredLedger(path).contains(rec().occurrence)


def test_any_record_blocks_including_unknown_results(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    ledger = FiredLedger(path)
    for i, result in enumerate(["attempting", "attempted", "confirmed", "failed", "unknown"]):
        ledger.append(rec(cue=f"c{i}", result=result))
    reloaded = FiredLedger(path)
    assert all(reloaded.contains(rec(cue=f"c{i}").occurrence) for i in range(5))


def test_torn_final_line_is_dropped(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    FiredLedger(path).append(rec())
    with path.open("a") as handle:
        handle.write('{"occurrence": {"perf')
    ledger = FiredLedger(path)
    assert ledger.dropped_torn_line and ledger.contains(rec().occurrence)


def test_corrupt_middle_line_refuses_to_load(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    FiredLedger(path).append(rec())
    good = path.read_text()
    path.write_text(good + "garbage\n" + good.replace('"a"', '"b"'))
    with pytest.raises(LedgerCorruptError):
        FiredLedger(path)


def test_next_song_instance_continues_after_restart() -> None:
    ledger = FiredLedger()
    assert ledger.next_song_instance("show") == 0
    ledger.append(rec(instance=3))
    assert ledger.next_song_instance("show") == 4
    assert ledger.next_song_instance("other") == 0


def test_preload_is_memory_only(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError):
        FiredLedger(tmp_path / "l.jsonl").preload([rec()])


def test_song_instances_are_persisted_and_continue_after_restart(tmp_path: Path) -> None:
    from roobotto.messages import SongInstanceRecord

    path = tmp_path / "l.jsonl"
    ledger = FiredLedger(path)
    ledger.append(SongInstanceRecord(performance="show", song_instance=0, song="s", started_ns=1))
    ledger.append(SongInstanceRecord(performance="show", song_instance=1, song="s", started_ns=2))
    reloaded = FiredLedger(path)
    assert reloaded.next_song_instance("show") == 2
    assert not reloaded.contains(rec(instance=1).occurrence), "an instance record blocks nothing"


def test_old_fired_lines_without_a_record_field_still_load(tmp_path: Path) -> None:
    path = tmp_path / "l.jsonl"
    path.write_text(
        '{"occurrence": {"performance": "show", "song_instance": 4, "section_visit": 1, '
        '"cue_id": "a"}, "first_attempt_ns": 5, "result": "attempted"}\n'
    )
    ledger = FiredLedger(path)
    assert ledger.contains(rec(cue="a", instance=4).occurrence)
    assert ledger.next_song_instance("show") == 5
