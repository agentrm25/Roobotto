"""Provenance classification (ADR-011, plan Section 5.4). A pure function that fails closed.

Every raw MIDI input becomes an ``Event`` tagged with its path and provenance. The first rule
that matches wins:

1. A port that is not an input path of exactly one declared source: **unknown**.
2. Any path but the source's authoritative one: a **duplicate**, kept as a state observation
   only. One action arriving on two paths therefore advances once.
3. An adapter-marked automated recall (Showcase, Command Center): **state observation**.
4. A message matching one Roobotto sent within the echo window: **output echo**.
5. A declared switch: **performer intent**. A press edge maps to the switch's press-gesture
   action (GO, STOP, EXTEND). Tap and hold gestures (BACK, MANUAL) are recognized by T-05; until
   then those edges carry no action.
6. A message matching a device's report record (ADR-015). A ``preset`` report is a **state
   observation** (song selection). A ``snapshot`` report within the initialization window after
   that source's preset change is the preset's load-time recall: **state observation**.
   Otherwise a snapshot transition the song maps with ``advance_on`` from the current section is
   **performer intent** (GO into that section), but only when the report is ``bench_verified``
   or arrived on a mock port, and it is the rig's only snapshot report. Any other snapshot change
   is a **state observation**.
7. Anything else: **unknown**.

Only rules 5 and 6 can produce performer intent, matching Section 5.4's two sources.
"""

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from roobotto.config.rig import Rig, Switch
from roobotto.engine.inputs import MidiIn
from roobotto.messages import Action, Event, Provenance

Detail = Literal[
    "unknown_source",
    "ambiguous_source",
    "duplicate_path",
    "automated",
    "echo",
    "switch_press",
    "switch_release",
    "program_change",
    "snapshot_preset_load",
    "snapshot_mapped",
    "snapshot_unverified",
    "snapshot_unmapped",
    "snapshot_other",
    "unmapped",
]


@dataclass(frozen=True)
class SentWire:
    """A message Roobotto sent, remembered for the echo window."""

    t_ns: int
    kind: str
    channel: int
    data1: int
    data2: int | None


@dataclass(frozen=True)
class ProvenanceContext:
    rig: Rig
    now_ns: int
    init_window_ns: int
    echo_window_ns: int
    last_program_ns: Mapping[str, int]
    """Source id -> when its last preset change arrived."""
    recent_sends: Sequence[SentWire]
    mapped_targets: Mapping[int, str]
    """Snapshot number (1-8) -> the section it advances into from the current position."""


@dataclass(frozen=True)
class Classified:
    event: Event
    detail: Detail
    reason: str
    switch: str | None = None
    snapshot: int | None = None
    target: str | None = None
    program: int | None = None
    device: str | None = None
    """The device whose report record interpreted the message."""


def _edge(msg: MidiIn) -> Literal["press", "release"]:
    if msg.kind == "note_on":
        return "press" if (msg.data2 or 0) > 0 else "release"
    if msg.kind == "control_change":
        return "press" if (msg.data2 or 0) >= 64 else "release"
    return "release"


def _match_switch(rig: Rig, msg: MidiIn) -> Switch | None:
    for sw in rig.switches:
        if sw.port != msg.port or sw.channel != msg.channel or sw.number != msg.data1:
            continue
        if sw.kind == "note" and msg.kind in ("note_on", "note_off"):
            return sw
        if sw.kind == "cc" and msg.kind == "control_change":
            return sw
    return None


def _is_echo(msg: MidiIn, ctx: ProvenanceContext) -> bool:
    for sent in ctx.recent_sends:
        if ctx.now_ns - sent.t_ns > ctx.echo_window_ns:
            continue
        if (sent.kind, sent.channel, sent.data1, sent.data2) == (
            msg.kind,
            msg.channel,
            msg.data1,
            msg.data2,
        ):
            return True
    return False


def classify(msg: MidiIn, event_id: str, ctx: ProvenanceContext) -> Classified:
    def make(
        provenance: Provenance,
        detail: Detail,
        reason: str,
        *,
        source: str = "unknown",
        action: Action | None = None,
        switch: str | None = None,
        snapshot: int | None = None,
        target: str | None = None,
        program: int | None = None,
        device: str | None = None,
    ) -> Classified:
        event = Event(
            id=event_id,
            available_ns=msg.t_ns,
            source=source,
            path=msg.port,
            kind=msg.kind,
            channel=msg.channel,
            data1=msg.data1,
            data2=msg.data2,
            provenance=provenance,
            mapped_action=action,
        )
        return Classified(event, detail, reason, switch, snapshot, target, program, device)

    sources = [s for s in ctx.rig.inputs if any(p.port == msg.port for p in s.paths)]
    if not sources:
        return make(
            "unknown",
            "unknown_source",
            f"port '{msg.port}' is not an input path of any declared source; fails closed",
        )
    if len(sources) > 1:
        names = ", ".join(s.id for s in sources)
        return make(
            "unknown",
            "ambiguous_source",
            f"port '{msg.port}' belongs to several sources ({names}); fails closed",
        )
    src = sources[0]
    path = next(p for p in src.paths if p.port == msg.port)
    if not path.authoritative:
        return make(
            "state_observation",
            "duplicate_path",
            f"not the authoritative path for source '{src.id}': a duplicate, never authority "
            "(ADR-011)",
            source=src.id,
        )
    if msg.automated:
        return make(
            "state_observation",
            "automated",
            "automated recall: state observation only (ADR-011)",
            source=src.id,
        )
    if _is_echo(msg, ctx):
        return make(
            "output_echo",
            "echo",
            "matches a message Roobotto sent within the echo window (ADR-011)",
            source=src.id,
        )

    switch = _match_switch(ctx.rig, msg)
    if switch is not None:
        edge = _edge(msg)
        if edge == "press":
            for ctl in ctx.rig.controls:
                if ctl.switch == switch.id and ctl.gesture == "press":
                    return make(
                        "performer_intent",
                        "switch_press",
                        f"press edge of '{switch.id}' maps to {ctl.action.upper()}",
                        source=src.id,
                        action=ctl.action,
                        switch=switch.id,
                    )
        return make(
            "performer_intent",
            "switch_press" if edge == "press" else "switch_release",
            f"raw {edge} of '{switch.id}'; tap and hold gestures are recognized from T-05",
            source=src.id,
            switch=switch.id,
        )

    matches = [
        (device_id, report)
        for device_id, report in ctx.rig.reports()
        if report.source == src.id
        and report.channel == msg.channel
        and report.kind == msg.kind
        and (report.number is None or report.number == msg.data1)
    ]
    if matches:
        device_id, report = matches[0]
        if report.meaning == "preset":
            return make(
                "state_observation",
                "program_change",
                f"'{device_id}' preset change: song selection, never a GO (Section 8)",
                source=src.id,
                program=msg.data1,
                device=device_id,
            )
        value = msg.data2
        if value is None or value not in report.values:
            return make(
                "unknown",
                "snapshot_other",
                f"value {value} is not a snapshot that '{device_id}' reports; fails closed",
                source=src.id,
                device=device_id,
            )
        snapshot = report.values.index(value) + 1
        last_program = ctx.last_program_ns.get(src.id)
        if last_program is not None and ctx.now_ns - last_program <= ctx.init_window_ns:
            return make(
                "state_observation",
                "snapshot_preset_load",
                "snapshot recalled by a preset load (initialization window); never a GO",
                source=src.id,
                snapshot=snapshot,
                device=device_id,
            )
        target = ctx.mapped_targets.get(snapshot)
        if target is not None and len(ctx.rig.reports("snapshot")) == 1:
            port = next((p for p in ctx.rig.ports if p.id == msg.port), None)
            if report.status != "bench_verified" and (port is None or port.transport != "mock"):
                return make(
                    "state_observation",
                    "snapshot_unverified",
                    f"snapshot {snapshot} is mapped into '{target}', but the report is "
                    f"'{report.status}', not bench_verified; it cannot authorize a GO (ADR-015)",
                    source=src.id,
                    snapshot=snapshot,
                    device=device_id,
                )
            return make(
                "performer_intent",
                "snapshot_mapped",
                f"snapshot {snapshot} is mapped by advance_on into '{target}'",
                source=src.id,
                action="go",
                snapshot=snapshot,
                target=target,
                device=device_id,
            )
        return make(
            "state_observation",
            "snapshot_unmapped",
            f"snapshot {snapshot} is not mapped from the current section; does not advance",
            source=src.id,
            snapshot=snapshot,
            device=device_id,
        )

    return make(
        "unknown",
        "unmapped",
        f"no switch or report record matches on source '{src.id}'; fails closed",
        source=src.id,
    )
