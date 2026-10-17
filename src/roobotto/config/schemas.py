"""JSON Schema export: ``schemas/`` is generated from the pydantic models (ADR-004)."""

import json
from pathlib import Path

from pydantic import BaseModel

from roobotto.config.rig import DeviceCapability, Rig
from roobotto.config.setlist import Setlist
from roobotto.config.song import Cue, Song
from roobotto.messages import (
    CueOccurrence,
    Decision,
    Event,
    FiredLedgerRecord,
    MusicalState,
    OutputAttempt,
)

SCHEMAS: dict[str, type[BaseModel]] = {
    "song": Song,
    "cue": Cue,
    "device_capability": DeviceCapability,
    "rig": Rig,
    "setlist": Setlist,
    "event": Event,
    "musical_state": MusicalState,
    "cue_occurrence": CueOccurrence,
    "decision": Decision,
    "output_attempt": OutputAttempt,
    "fired_ledger_record": FiredLedgerRecord,
}


def render(model: type[BaseModel]) -> str:
    return json.dumps(model.model_json_schema(by_alias=True), indent=2, sort_keys=True) + "\n"


def export(out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for name, model in SCHEMAS.items():
        target = out_dir / f"{name}.schema.json"
        target.write_text(render(model), encoding="utf-8")
        written.append(target)
    return written


def stale(out_dir: Path) -> list[Path]:
    """Schema files that are missing or differ from what the models generate."""
    bad: list[Path] = []
    for name, model in SCHEMAS.items():
        target = out_dir / f"{name}.schema.json"
        if not target.exists() or target.read_text(encoding="utf-8") != render(model):
            bad.append(target)
    return bad
