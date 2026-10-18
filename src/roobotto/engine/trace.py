"""The consumed input trace (ADR-014).

A header line names what the replay claim covers (config hash, seed, performance, whether the
MIDI adapter was enabled, and the fired ledger loaded at start). Every following line is one
input, in the order the engine consumed it, with the time it became available.
"""

import json
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, TextIO

from pydantic import Field

from roobotto.config.common import Id, Model, NonNegInt, Sha256
from roobotto.engine.inputs import INPUT_ADAPTER, MidiIn, OperatorIn, PortStatus, Tick, TrackerIn
from roobotto.engine.show import canonical_json
from roobotto.messages import FiredLedgerRecord, SongInstanceRecord

InputT = MidiIn | TrackerIn | Tick | OperatorIn | PortStatus


class TraceHeader(Model):
    type: Literal["header"] = "header"
    format: Literal[1] = 1
    config_hash: Sha256
    seed: NonNegInt
    performance: Id
    midi_enabled: bool = Field(strict=True)
    initial_ledger: list[FiredLedgerRecord | SongInstanceRecord] = Field(default_factory=lambda: [])


@dataclass(frozen=True)
class Trace:
    header: TraceHeader
    inputs: tuple[InputT, ...]


class TraceError(ValueError):
    pass


class TraceWriter:
    def __init__(self, path: Path | None, header: TraceHeader) -> None:
        self.header = header
        self.lines: list[str] = []
        self._handle: TextIO | None = None
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
            self._handle = path.open("w", encoding="utf-8")
        self._emit(canonical_json(header.model_dump(mode="json")))

    def _emit(self, line: str) -> None:
        self.lines.append(line)
        if self._handle is not None:
            self._handle.write(line + "\n")
            self._handle.flush()

    def write(self, inp: InputT) -> None:
        self._emit(canonical_json(inp.model_dump(mode="json")))

    def trace(self) -> Trace:
        return parse_trace(self.lines)

    def close(self) -> None:
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def parse_trace(lines: Iterable[str]) -> Trace:
    rows = [line for line in lines if line.strip()]
    if not rows:
        raise TraceError("empty trace: the first line must be a header")
    try:
        header = TraceHeader.model_validate(json.loads(rows[0]))
        inputs = tuple(INPUT_ADAPTER.validate_python(json.loads(row)) for row in rows[1:])
    except ValueError as exc:
        raise TraceError(f"invalid trace: {exc}") from exc
    return Trace(header, inputs)


def load_trace(path: Path) -> Trace:
    return parse_trace(path.read_text(encoding="utf-8").split("\n"))
