"""Take annotations and manifests (plan Section 9, ADR-008).

Annotations are the ground truth for one take: section visits, bar lines, beats, intended cue
instants, deviation points, dropouts and sync bursts, all in seconds from the start of the take
plus the exact sample index where it matters. Synthetic takes (T-08) write them exactly; T-09
imports human-made ones. A manifest describes the files of a take and hashes them, so the audio
can stay out of git and still be pinned (ADR-008).
"""

from typing import Any, Literal

from pydantic import Field

from roobotto.config.common import Id, Model, NonNegInt, PosInt, Text

Seconds = float
Stream = Literal["drums", "bass", "chords", "vocal", "room", "guitar", "helix"]


class Visit(Model):
    section: Id
    visit: PosInt
    bars: PosInt
    start_s: Seconds
    end_s: Seconds
    deviation: Literal["repeat", "skip", "extend"] | None = None
    """How this visit departs from the nominal path, when it does."""


class Bar(Model):
    section: Id
    visit: PosInt
    bar: PosInt
    start_s: Seconds
    start_sample: NonNegInt


class Beat(Model):
    section: Id
    visit: PosInt
    bar: PosInt
    beat: PosInt
    at_s: Seconds
    bpm: float


class TempoKnot(Model):
    at_s: Seconds
    bpm: float


class CueInstant(Model):
    """When a song cue was meant to land in this take, per visit of its section."""

    id: Id
    section: Id
    visit: PosInt
    at_s: Seconds


class Deviation(Model):
    kind: Literal["repeat", "skip", "extend"]
    at_s: Seconds
    """The first instant at which this take differs from its twin."""
    section: Id
    visit: PosInt
    modeled: bool
    """Whether the song file allows this move (a repeat edge, an extendable section)."""
    detail: Text


class Dropout(Model):
    stream: Stream
    start_s: Seconds
    end_s: Seconds


class SyncBurst(Model):
    at_s: Seconds
    streams: list[Stream]


class Annotations(Model):
    kind: Literal["annotations"]
    schema_version: Literal[0]
    take: Text
    """``<song id>/<variation or take name>``."""
    song: Id
    variation: Text | None = None
    synthetic: bool
    verified: bool = False
    """Human-verified boundaries (T-09). Synthetic takes are exact, not verified."""
    sample_rate: PosInt
    duration_s: Seconds
    sync_bursts: list[SyncBurst]
    visits: list[Visit]
    bars: list[Bar]
    beats: list[Beat]
    tempo: list[TempoKnot]
    cues: list[CueInstant] = Field(default_factory=lambda: [])
    deviations: list[Deviation] = Field(default_factory=lambda: [])
    dropouts: list[Dropout] = Field(default_factory=lambda: [])
    twin_of: Text | None = None
    """The take this one is a causal twin of: identical up to ``diverges_at_s``."""
    diverges_at_s: Seconds | None = None


class TakeManifest(Model):
    kind: Literal["take_manifest"]
    schema_version: Literal[0]
    take: Text
    song: Id
    take_type: Literal["synthetic", "rehearsal", "bench"]
    variation: Text | None = None
    sample_rate: PosInt
    bit_depth: Literal[16, 24]
    channels: dict[Stream, Text]
    """Stream -> file name, relative to the take folder."""
    sha256: dict[Text, Text]
    """File name -> SHA-256 of its bytes."""
    devices: list[Text] = Field(default_factory=lambda: [])
    consent: Literal["synthetic", "given", "none"]
    render: dict[str, Any] | None = None
    """How a synthetic take was made, enough to make it again."""
