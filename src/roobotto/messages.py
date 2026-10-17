"""Runtime message schemas v0 (plan Section 4): Event, MusicalState, CueOccurrence,
Decision, OutputAttempt and the fired-ledger record.

These are the contracts between adapters, tracker, engine, supervisor and recorder. They carry
no behavior; the rules that can be checked on a single message are checked here.
"""

from typing import Annotated, Literal, Self

from pydantic import Field, model_validator

from roobotto.config.common import (
    Channel,
    Id,
    IntentKey,
    Midi7,
    Model,
    Ms,
    NonNegInt,
    Ns,
    PosFloat,
    PosInt,
    Prob,
    Sha256,
    Text,
    raise_if,
)
from roobotto.config.song import StateValue

_PROB_EPSILON = 1e-9

Provenance = Literal["performer_intent", "state_observation", "output_echo", "unknown"]
Action = Literal["go", "back", "manual", "stop", "extend"]
CueClass = Literal["decorative", "standard", "critical"]


class Event(Model):
    """One input observed by an adapter. Fail closed: provenance is required, never defaulted."""

    id: Text
    available_ns: Ns
    """When the event became available to the engine, on the injected clock."""
    source: Id
    path: Id
    """The port it arrived on. Only the source's authoritative path may carry authority."""
    kind: Literal["note_on", "note_off", "control_change", "program_change"]
    channel: Channel | None = None
    data1: Midi7 | None = None
    data2: Midi7 | None = None
    provenance: Provenance
    mapped_action: Action | None = None

    @model_validator(mode="after")
    def _only_performer_intent_maps_to_action(self) -> Self:
        if self.mapped_action is not None and self.provenance != "performer_intent":
            raise ValueError(
                f"event '{self.id}': mapped_action '{self.mapped_action}' requires provenance "
                f"'performer_intent', not '{self.provenance}' (ADR-011, I-16)"
            )
        return self


class SongRef(Model):
    id: Id
    p: Prob


class Hypothesis(Model):
    section: Id
    visit: PosInt
    bar: PosInt
    p: Prob


class Beat(Model):
    phase: Annotated[float, Field(strict=True, ge=0, lt=1, allow_inf_nan=False)]
    tempo_bpm: PosFloat
    tempo_sd: Ms


class Downbeat(Model):
    t_ns: Ns
    sd_ms: Ms


class Boundary(Model):
    t_ns: Ns
    sd_ms: Ms
    successors: dict[Id, Prob] = Field(default_factory=lambda: {})


Health = Literal["ok", "degraded", "lost"]


class MusicalState(Model):
    """Tracker -> engine. The engine drops lower ``seq`` and any earlier ``tracker_session``."""

    tracker_session: Annotated[str, Field(strict=True, pattern=r"^[0-9a-f]{8,32}$")]
    seq: NonNegInt
    t_ns: Ns
    song: SongRef
    mode: Literal["following", "lost", "manual"]
    hypotheses: list[Hypothesis] = Field(default_factory=lambda: [])
    beat: Beat | None = None
    next_downbeat: Downbeat | None = None
    boundary: Boundary | None = None
    health: dict[Id, Health] = Field(default_factory=lambda: {})

    @model_validator(mode="after")
    def _check(self) -> Self:
        errors: list[str] = []
        if sum(h.p for h in self.hypotheses) > 1 + _PROB_EPSILON:
            errors.append("hypothesis probabilities sum to more than 1")
        if self.boundary and sum(self.boundary.successors.values()) > 1 + _PROB_EPSILON:
            errors.append("boundary successor probabilities sum to more than 1")
        if self.mode == "following" and (not self.hypotheses or self.beat is None):
            errors.append("mode 'following' needs at least one hypothesis and a beat estimate")
        if self.next_downbeat and self.next_downbeat.t_ns < self.t_ns:
            errors.append("next_downbeat is earlier than the time the estimate is valid for")
        if self.boundary and self.boundary.t_ns < self.t_ns:
            errors.append("boundary is earlier than the time the estimate is valid for")
        raise_if(errors)
        return self


class OccurrenceId(Model):
    """Cue identity (ADR-012, I-6). The scheduler generation is deliberately not a field."""

    performance: Id
    song_instance: NonNegInt
    section_visit: PosInt
    cue_id: Id

    @property
    def key(self) -> str:
        return f"{self.performance}/{self.song_instance}/{self.section_visit}/{self.cue_id}"


class Authorization(Model):
    """The performer action that authorized an occurrence (Section 5.4)."""

    event_id: Text
    action: Literal["go", "back"]


Lifecycle = Literal["planned", "scheduled", "committed", "sent", "cancelled", "expired"]


class CueOccurrence(Model):
    id: OccurrenceId
    generation: NonNegInt
    """Invalidates pending work only; never part of the identity."""
    cue_class: CueClass = Field(alias="class")
    intents: Annotated[dict[IntentKey, StateValue], Field(min_length=1)]
    due_ns: Ns
    send_deadline_ns: Ns
    lifecycle: Lifecycle
    authorized_by: Authorization | None = None

    @model_validator(mode="after")
    def _check(self) -> Self:
        errors: list[str] = []
        if self.send_deadline_ns > self.due_ns:
            errors.append("send_deadline_ns must not be after due_ns")
        if (
            self.cue_class == "critical"
            and self.lifecycle in ("committed", "sent")
            and self.authorized_by is None
        ):
            errors.append(
                "a critical occurrence cannot be committed or sent without the performer action "
                "that authorized it (ADR-013, I-14)"
            )
        raise_if(errors)
        return self


class Decision(Model):
    kind: Literal["fire", "hold", "ask", "abstain"]
    reason: Text
    """Refusing to act is a first-class outcome: it is logged and shown on the HUD."""
    evidence: list[Text] = Field(default_factory=lambda: [])
    occurrence: OccurrenceId | None = None
    go_timing_class: Literal["Q", "D", "L", "F"] | None = None
    """Quantized, Deadline missed, Late window, or No lock (Section 8)."""

    @model_validator(mode="after")
    def _fire_names_an_occurrence(self) -> Self:
        if self.kind == "fire" and self.occurrence is None:
            raise ValueError("a 'fire' decision must name the occurrence it fires")
        return self


class OutputAttempt(Model):
    occurrence: OccurrenceId
    generation: NonNegInt
    adapter: Literal["mock", "midi", "osc", "artnet"]
    device: Id
    command: IntentKey
    value: StateValue
    bytes_sent: list[Annotated[int, Field(strict=True, ge=0, le=255)]] = Field(
        default_factory=lambda: []
    )
    send_time_ns: Ns
    config_hash: Sha256
    armed: bool = Field(strict=True)
    reason: Text
    result: Literal["attempted", "confirmed", "failed", "unknown"]

    @model_validator(mode="after")
    def _physical_needs_arming(self) -> Self:
        if self.adapter != "mock" and not self.armed:
            raise ValueError(
                f"adapter '{self.adapter}' is physical but outputs were not armed (I-2)"
            )
        return self


class FiredLedgerRecord(Model):
    """Append-only, written to disk before the send. Any record blocks a second send (I-5, I-7).

    Keyed by occurrence only: the generation is not a field (I-6).
    """

    occurrence: OccurrenceId
    first_attempt_ns: Ns
    result: Literal["attempting", "attempted", "confirmed", "failed", "unknown"]
