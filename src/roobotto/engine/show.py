"""Everything one run is configured with, and its hash (ADR-014).

Engine settings are runtime parameters that are not part of the YAML schemas. They are hashed
with the rig, songs and setlist, so a replay claim names exactly what it covers.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from functools import cached_property
from pathlib import Path
from typing import Annotated, Any

from pydantic import Field

from roobotto.config.common import Id, Model, NonNegInt, PosInt, Prob
from roobotto.config.rig import Rig
from roobotto.config.setlist import Setlist
from roobotto.config.song import Song
from roobotto.config.validate import validate_files


class EngineSettings(Model):
    performance: Id = "rehearsal"
    """Part of every occurrence id. Keep it the same across a restart so the fired ledger
    blocks resends (ADR-012)."""
    milestone: Annotated[int, Field(strict=True, ge=1, le=8)] = 1
    """Gates tracker-fired cues: decorative from M4, standard from M5. Critical never (I-14)."""
    seed: NonNegInt = 0
    """Recorded for the replay claim. The core uses no randomness today."""
    tracker_stale_ms: PosInt = 200
    """No accepted MusicalState for this long suspends tracker authority. [assumed]"""
    relocalize_min_p: Prob = 0.9
    """Top-hypothesis probability needed before the tracker may move the engine. [assumed]"""


class ShowConfigError(ValueError):
    pass


def canonical_json(data: Any) -> str:
    return json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


@dataclass(frozen=True)
class ShowConfig:
    rig: Rig
    songs: Mapping[str, Song]
    setlist: Setlist | None = None
    settings: EngineSettings = field(default_factory=EngineSettings)

    @cached_property
    def config_hash(self) -> str:
        data = {
            "rig": self.rig.model_dump(by_alias=True, mode="json"),
            "songs": {
                k: self.songs[k].model_dump(by_alias=True, mode="json") for k in sorted(self.songs)
            },
            "setlist": (
                self.setlist.model_dump(by_alias=True, mode="json") if self.setlist else None
            ),
            "settings": self.settings.model_dump(mode="json"),
        }
        return hashlib.sha256(canonical_json(data).encode()).hexdigest()


def load_show(paths: Iterable[Path], settings: EngineSettings | None = None) -> ShowConfig:
    """Validate the files together (cross-file checks included) and bundle them."""
    report = validate_files(paths)
    if not report.ok:
        raise ShowConfigError("\n".join(str(i) for i in report.issues))
    rigs = [c for c in report.configs.values() if isinstance(c, Rig)]
    songs = {c.id: c for c in report.configs.values() if isinstance(c, Song)}
    setlists = [c for c in report.configs.values() if isinstance(c, Setlist)]
    if len(rigs) != 1:
        raise ShowConfigError(f"exactly one rig is needed, found {len(rigs)}")
    if not songs:
        raise ShowConfigError("at least one song is needed")
    if len(setlists) > 1:
        raise ShowConfigError("at most one setlist may be given")
    return ShowConfig(
        rig=rigs[0],
        songs=songs,
        setlist=setlists[0] if setlists else None,
        settings=settings or EngineSettings(),
    )
