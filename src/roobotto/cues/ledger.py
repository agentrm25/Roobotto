"""The fired ledger (ADR-012, plan Section 5.5).

Append-only. Before a send, the supervisor appends an "attempting" record and the ledger flushes
it to disk; then the send happens and the result is appended. Any record for an occurrence
blocks a second automatic send (I-5, I-7). If the process dies between the record and the send,
the cue is lost rather than duplicated.

The ledger also records each song instance as it starts (ADR-016). A restart continues song
instances after the highest one recorded, so no occurrence id is ever reused, fired or not.

A torn final line can only come from a crash during the write, which happens before the send
or before the song instance is used, so it is dropped on load. A malformed line anywhere else
stops the load: the ledger is not trusted and the run must not start.
"""

import json
import os
from collections.abc import Sequence
from pathlib import Path

from pydantic import TypeAdapter, ValidationError

from roobotto.messages import FiredLedgerRecord, LedgerRecord, OccurrenceId

_RECORD: TypeAdapter[LedgerRecord] = TypeAdapter(LedgerRecord)


class LedgerCorruptError(RuntimeError):
    pass


class FiredLedger:
    def __init__(self, path: Path | None = None) -> None:
        """``path=None`` keeps the ledger in memory (tests, replay, dry runs)."""
        self._path = path
        self._records: list[LedgerRecord] = []
        self._keys: set[str] = set()
        self.dropped_torn_line = False
        if path is not None and path.exists():
            self._load(path)

    @property
    def path(self) -> Path | None:
        return self._path

    def _load(self, path: Path) -> None:
        lines = path.read_text(encoding="utf-8").split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        for index, line in enumerate(lines):
            try:
                record = _RECORD.validate_python(json.loads(line))
            except (json.JSONDecodeError, ValidationError) as exc:
                if index == len(lines) - 1:
                    self.dropped_torn_line = True
                    break
                raise LedgerCorruptError(
                    f"{path}: line {index + 1} is not a ledger record"
                ) from exc
            self._remember(record)

    def _remember(self, record: LedgerRecord) -> None:
        self._records.append(record)
        if isinstance(record, FiredLedgerRecord):
            self._keys.add(record.occurrence.key)

    def contains(self, occurrence: OccurrenceId) -> bool:
        return occurrence.key in self._keys

    def records(self) -> Sequence[LedgerRecord]:
        return tuple(self._records)

    def next_song_instance(self, performance: str) -> int:
        """One past the highest song instance ``performance`` has used, fired or not, so a
        restart never reuses an occurrence id and turns no past cue into new work (I-5, I-6)."""
        used = [
            r.occurrence.song_instance if isinstance(r, FiredLedgerRecord) else r.song_instance
            for r in self._records
            if (r.occurrence.performance if isinstance(r, FiredLedgerRecord) else r.performance)
            == performance
        ]
        return max(used) + 1 if used else 0

    def append(self, record: LedgerRecord) -> None:
        """Persist the record (flushed and fsynced) before returning."""
        if self._path is not None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(record.model_dump(mode="json"), sort_keys=True) + "\n"
            with self._path.open("a", encoding="utf-8") as handle:
                handle.write(line)
                handle.flush()
                os.fsync(handle.fileno())
        self._remember(record)

    def preload(self, records: Sequence[LedgerRecord]) -> None:
        """Seed an in-memory ledger (replay of a run that started with a ledger)."""
        if self._path is not None:
            raise RuntimeError("preload is for in-memory ledgers only")
        for record in records:
            self._remember(record)
