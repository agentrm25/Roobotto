"""Engine inputs: everything the core consumes, in the order the wrapper received it.

Each input carries ``t_ns``, the time it became available on the injected clock. The consumed
input trace is a list of these (ADR-014). Raw MIDI arrives unclassified: provenance is decided
by the core (Section 5.4), never trusted from outside, except that an adapter may mark a message
as an automated recall, which can only lower its authority.
"""

from typing import Annotated, Literal

from pydantic import Field, TypeAdapter

from roobotto.config.common import Channel, Id, Midi7, Model, Ns
from roobotto.messages import MusicalState

OperatorAction = Literal[
    "arm",
    "disarm",
    "resume",
    "confirm_preset_b",
    "enable_tracker",
    "set_mode",
    "select_song",
    "next_song",
    "resync",
]
"""Deliberate HUD actions (Section 5.3). None of them is performer intent."""


class MidiIn(Model):
    type: Literal["midi"] = "midi"
    t_ns: Ns
    port: Id
    kind: Literal["note_on", "note_off", "control_change", "program_change"]
    channel: Channel
    data1: Midi7
    data2: Midi7 | None = None
    automated: bool = Field(default=False, strict=True)
    """Set by an adapter that knows the message is an automated recall (Showcase, Command
    Center). It can only lower authority (ADR-011)."""


class TrackerIn(Model):
    type: Literal["tracker"] = "tracker"
    t_ns: Ns
    state: MusicalState


class Tick(Model):
    """A timer tick: lets the core act on time passing (send deadlines, staleness)."""

    type: Literal["tick"] = "tick"
    t_ns: Ns


class OperatorIn(Model):
    type: Literal["operator"] = "operator"
    t_ns: Ns
    action: OperatorAction
    value: Id | None = None


class PortStatus(Model):
    type: Literal["port"] = "port"
    t_ns: Ns
    port: Id
    connected: bool = Field(strict=True)


EngineInput = Annotated[
    MidiIn | TrackerIn | Tick | OperatorIn | PortStatus, Field(discriminator="type")
]
INPUT_ADAPTER: TypeAdapter[MidiIn | TrackerIn | Tick | OperatorIn | PortStatus] = TypeAdapter(
    EngineInput
)
