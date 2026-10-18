"""JSONL logs: one JSON object per line, each with the run id, config hash and clock time."""

from collections.abc import Mapping
from pathlib import Path
from typing import Any, TextIO

from roobotto.engine.show import canonical_json


class JsonlLog:
    def __init__(self, path: Path | None, *, run_id: str, config_hash: str) -> None:
        """``path=None`` keeps the log in memory only."""
        self.run_id = run_id
        self.config_hash = config_hash
        self.records: list[dict[str, Any]] = []
        self._handle: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("a", encoding="utf-8")

    def write(self, record: Mapping[str, Any]) -> None:
        if "t_ns" not in record:
            raise ValueError("every log record carries its clock time, t_ns")
        line = {"run_id": self.run_id, "config_hash": self.config_hash, **record}
        self.records.append(line)
        if self._handle is not None:
            self._handle.write(canonical_json(line) + "\n")
            self._handle.flush()

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None
