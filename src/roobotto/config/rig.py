"""Rig schema v0: ports, devices and their capability records, input paths, control map.

Safety rules enforced here:
- I-1 / ADR-009: a mock-profile rig may only use mock ports, and in a physical-profile rig
  every command on a non-mock port must be ``bench_verified`` with a bench log. No override.
- I-2: arming is never configured; it is explicit per run.
- I-13: every device that would receive a command's port/channel must have a declared,
  known-safe behavior.
- I-17 / ADR-010: no switch mixes a forward action with a backward or safety action.
- ADR-011: exactly one authoritative input path per source.
- ADR-015: input is interpreted only through a device's report records. Reports may be of any
  status (observing state is harmless), but only a ``bench_verified`` report, or one arriving on
  a mock port, can carry performer intent; the engine enforces that.
"""

from collections import defaultdict
from typing import Annotated, Any, Literal, Self

from pydantic import Field, model_validator

from roobotto.config.common import (
    Channel,
    EvidenceId,
    Id,
    Midi7,
    Model,
    Ms,
    PosFloat,
    PosInt,
    Text,
    raise_if,
)

FORWARD_ACTIONS = frozenset({"go", "extend"})
REQUIRED_GESTURE = {
    "go": "press",
    "extend": "press",
    "stop": "press",
    "back": "tap",
    "manual": "hold",
}
"""ADR-010. GO/EXTEND/STOP act on the press edge; BACK is a tap; MANUAL is a hold."""


class Port(Model):
    """Direction is from Roobotto's point of view: ``out`` is where Roobotto sends."""

    id: Id
    direction: Literal["in", "out", "both"]
    transport: Literal["usb", "din", "virtual", "mock"]


class WireMessage(Model):
    kind: Literal["note_on", "note_off", "control_change", "program_change"]
    data1: Midi7
    data2: Midi7 | None = None

    @model_validator(mode="after")
    def _check_data2(self) -> Self:
        if self.kind == "program_change" and self.data2 is not None:
            raise ValueError("program_change takes one data byte; remove data2")
        if self.kind != "program_change" and self.data2 is None:
            raise ValueError(f"{self.kind} needs data2")
        return self


class Evidence(Model):
    source: EvidenceId | None = None
    """Evidence-register id (docs/evidence-register.md) for a documented claim."""
    bench_log: Text | None = None
    """Path under captures/ of the bench log for a bench_verified command."""


Status = Literal["unknown", "documented", "bench_verified"]


def evidence_errors(status: str, evidence: Evidence) -> list[str]:
    """The evidence a status needs, shared by commands and reports (Section 10.2)."""
    errors: list[str] = []
    if status == "documented" and evidence.source is None:
        errors.append("status 'documented' needs evidence.source (an evidence-register id)")
    if status == "bench_verified":
        log = evidence.bench_log
        if log is None:
            errors.append("status 'bench_verified' needs evidence.bench_log under captures/")
        elif not log.startswith("captures/") or ".." in log.split("/") or log.endswith("/"):
            errors.append(f"bench_log '{log}' must be a file path under captures/")
    return errors


class Command(Model):
    type: Literal["state", "one_shot"]
    """State is safe to resend by explicit resync only; one-shots fire at most once (I-15)."""
    port: Id
    channel: Channel
    values: Annotated[dict[Id, WireMessage | None], Field(min_length=1)]
    """Cue value -> wire message. ``null`` is allowed only while the status is unknown."""
    status: Literal["unknown", "documented", "bench_verified"]
    evidence: Evidence = Field(default_factory=Evidence)
    latency_ms: Ms | None = None
    min_interval_ms: PosInt
    """Per-device rate limit (I-15)."""

    @model_validator(mode="after")
    def _check_status(self) -> Self:
        errors: list[str] = []
        if self.status != "unknown" and any(m is None for m in self.values.values()):
            errors.append(f"status '{self.status}' needs a wire message for every value")
        errors += evidence_errors(self.status, self.evidence)
        raise_if(errors)
        return self


class Report(Model):
    """A message a device sends to Roobotto, and what it means (ADR-015, plan Section 4).

    ``snapshot``: a controller whose values report the active snapshot. ``values`` lists the data
    value reported for snapshot 1, 2, 3 and so on, in order. ``preset``: a Program Change sent
    when the device changes preset (song selection, H-2).
    """

    meaning: Literal["snapshot", "preset"]
    source: Id
    """The input source it arrives from. Only that source's authoritative path counts."""
    channel: Channel
    kind: Literal["control_change", "program_change"]
    number: Midi7 | None = None
    """The controller number, for ``control_change``."""
    values: list[Midi7] = Field(default_factory=lambda: [])
    status: Status
    evidence: Evidence = Field(default_factory=Evidence)

    @model_validator(mode="after")
    def _check(self) -> Self:
        errors: list[str] = []
        if self.meaning == "snapshot":
            if self.kind != "control_change" or self.number is None:
                errors.append("a snapshot report is a control_change with a number")
            if not self.values:
                errors.append("a snapshot report lists the value for each snapshot in values")
            for dup in sorted({v for v in self.values if self.values.count(v) > 1}):
                errors.append(f"value {dup} is listed for two snapshots")
        elif self.kind != "program_change" or self.number is not None or self.values:
            errors.append("a preset report is a program_change with no number and no values")
        errors += evidence_errors(self.status, self.evidence)
        raise_if(errors)
        return self


class Listen(Model):
    """A port/channel on which a device may respond to what Roobotto sends."""

    port: Id
    channel: Channel
    behavior: Literal["intended", "ignores", "acts", "unknown"]
    """Required, never defaulted. ``ignores`` is the only value that is safe for a bystander."""


class DeviceCapability(Model):
    id: Id
    kind: Id
    owner: Literal["roobotto", "performer"]
    """Exactly one owner at a time (Section 5.2)."""
    receives: list[Listen] = Field(default_factory=lambda: [])
    commands: dict[Id, Command] = Field(default_factory=lambda: {})
    reports: dict[Id, Report] = Field(default_factory=lambda: {})
    """What the device sends to Roobotto (ADR-015)."""

    @model_validator(mode="after")
    def _check_receives(self) -> Self:
        seen: set[tuple[str, int]] = set()
        errors: list[str] = []
        for listen in self.receives:
            key = (listen.port, listen.channel)
            if key in seen:
                errors.append(
                    f"device '{self.id}': duplicate receives entry "
                    f"{listen.port} ch {listen.channel}"
                )
            seen.add(key)
        raise_if(errors)
        return self


class InputPath(Model):
    port: Id
    authoritative: bool = Field(strict=True)


class InputSource(Model):
    id: Id
    paths: Annotated[list[InputPath], Field(min_length=1)]

    @model_validator(mode="after")
    def _one_authoritative(self) -> Self:
        count = sum(1 for p in self.paths if p.authoritative)
        ports = [p.port for p in self.paths]
        errors: list[str] = []
        if count != 1:
            errors.append(
                f"source '{self.id}' has {count} authoritative paths; "
                "it must have exactly one (ADR-011)"
            )
        for dup in sorted({p for p in ports if ports.count(p) > 1}):
            errors.append(f"source '{self.id}': port '{dup}' listed twice")
        raise_if(errors)
        return self


class Switch(Model):
    id: Id
    port: Id
    channel: Channel
    kind: Literal["note", "cc"]
    number: Midi7


class Control(Model):
    switch: Id
    gesture: Literal["press", "tap", "hold"]
    action: Literal["go", "back", "manual", "stop", "extend"]


class Timing(Model):
    """Physical intervals, in milliseconds (ADR-005). Defaults are the plan's starting values."""

    echo_window_ms: PosInt = 250
    init_window_ms: PosInt = 500
    tap_max_ms: PosInt = 500
    hold_ms: PosInt = 500
    send_margin_ms: Ms = 5
    late_window_bars: PosFloat = 0.25

    @model_validator(mode="after")
    def _check_gestures(self) -> Self:
        if self.hold_ms < self.tap_max_ms:
            raise ValueError(
                f"hold_ms ({self.hold_ms}) must be at least tap_max_ms ({self.tap_max_ms}) "
                "so one press is never both a tap and a hold (I-17)"
            )
        return self


class Rig(Model):
    kind: Literal["rig"]
    schema_version: Literal[0]
    id: Id
    profile: Literal["mock", "physical"]
    ports: Annotated[list[Port], Field(min_length=1)]
    devices: list[DeviceCapability] = Field(default_factory=lambda: [])
    inputs: list[InputSource] = Field(default_factory=lambda: [])
    switches: list[Switch] = Field(default_factory=lambda: [])
    controls: list[Control] = Field(default_factory=lambda: [])
    timing: Timing = Field(default_factory=Timing)

    @model_validator(mode="before")
    @classmethod
    def _reject_arming(cls, data: Any) -> Any:
        if isinstance(data, dict):
            keys = {str(k).lower() for k in data}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
            if keys & {"armed", "arm", "arming", "enabled", "override"}:
                raise ValueError(
                    "arming and overrides are never configured: outputs are armed explicitly "
                    "for each run and never persist (ADR-009, I-1, I-2)"
                )
        return data  # pyright: ignore[reportUnknownVariableType]

    def device(self, device_id: str) -> DeviceCapability | None:
        return next((d for d in self.devices if d.id == device_id), None)

    def reports(self, meaning: str | None = None) -> list[tuple[str, Report]]:
        """``(device id, report)`` for every report, optionally of one meaning."""
        return [
            (d.id, r)
            for d in self.devices
            for r in d.reports.values()
            if meaning is None or r.meaning == meaning
        ]

    @model_validator(mode="after")
    def _check_reports(self) -> Self:
        sources = {i.id for i in self.inputs}
        seen: dict[tuple[str, int, str, int | None], str] = {}
        errors: list[str] = []
        for dev in self.devices:
            for name, report in dev.reports.items():
                label = f"report {dev.id}.{name}"
                if report.source not in sources:
                    errors.append(f"{label}: unknown input source '{report.source}'")
                key = (report.source, report.channel, report.kind, report.number)
                if key in seen:
                    errors.append(
                        f"{label}: the same message is already {seen[key]}; a message must "
                        "mean one thing (ADR-015)"
                    )
                seen[key] = label
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_unique_ids(self) -> Self:
        errors: list[str] = []
        groups: dict[str, list[str]] = {
            "port": [p.id for p in self.ports],
            "device": [d.id for d in self.devices],
            "input source": [i.id for i in self.inputs],
            "switch": [s.id for s in self.switches],
        }
        for label, ids in groups.items():
            for dup in sorted({i for i in ids if ids.count(i) > 1}):
                errors.append(f"duplicate {label} id '{dup}'")
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_ports(self) -> Self:
        ports = {p.id: p for p in self.ports}
        errors: list[str] = []

        def need(port_id: str, what: str, directions: set[str]) -> None:
            port = ports.get(port_id)
            if port is None:
                errors.append(f"{what}: unknown port '{port_id}'")
            elif port.direction not in directions:
                errors.append(
                    f"{what}: port '{port_id}' is '{port.direction}', "
                    f"needs one of {sorted(directions)}"
                )

        for dev in self.devices:
            for listen in dev.receives:
                need(listen.port, f"device '{dev.id}' receives", {"out", "both"})
            for name, cmd in dev.commands.items():
                need(cmd.port, f"command {dev.id}.{name}", {"out", "both"})
        for src in self.inputs:
            for path in src.paths:
                need(path.port, f"input source '{src.id}'", {"in", "both"})
        for sw in self.switches:
            need(sw.port, f"switch '{sw.id}'", {"in", "both"})
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_profile(self) -> Self:
        """I-1: unverified commands reach the mock adapter only; there is no override."""
        transport = {p.id: p.transport for p in self.ports}
        errors: list[str] = []
        if self.profile == "mock":
            for port in self.ports:
                if port.transport != "mock":
                    errors.append(
                        f"port '{port.id}' uses transport '{port.transport}' in a mock profile; "
                        "a mock profile may only use mock ports"
                    )
        else:
            for dev in self.devices:
                for name, cmd in dev.commands.items():
                    if transport.get(cmd.port) != "mock" and cmd.status != "bench_verified":
                        errors.append(
                            f"command {dev.id}.{name} is '{cmd.status}' but its port "
                            f"'{cmd.port}' is physical; only bench_verified commands may be in "
                            "a physical profile (I-1)"
                        )
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_collisions(self) -> Self:
        """I-13: every device that would receive a command must have known-safe behavior."""
        listeners: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
        for dev in self.devices:
            for listen in dev.receives:
                listeners[(listen.port, listen.channel)].append((dev.id, listen.behavior))
        errors: list[str] = []
        for dev in self.devices:
            for name, cmd in dev.commands.items():
                label = f"command {dev.id}.{name} on {cmd.port} ch {cmd.channel}"
                mine = [b for d, b in listeners[(cmd.port, cmd.channel)] if d == dev.id]
                if mine != ["intended"]:
                    errors.append(
                        f"{label}: device '{dev.id}' must list this port/channel in receives "
                        "with behavior 'intended'"
                    )
                for other, behavior in listeners[(cmd.port, cmd.channel)]:
                    if other != dev.id and behavior != "ignores":
                        errors.append(
                            f"{label} would also reach '{other}' whose declared behavior is "
                            f"'{behavior}'; it must be 'ignores' or filtered onto another "
                            "port (I-13)"
                        )
        raise_if(errors)
        return self

    @model_validator(mode="after")
    def _check_controls(self) -> Self:
        switches = {s.id for s in self.switches}
        errors: list[str] = []
        by_switch: dict[str, list[Control]] = defaultdict(list)
        for ctl in self.controls:
            if ctl.switch not in switches:
                errors.append(f"control {ctl.action}: unknown switch '{ctl.switch}'")
            by_switch[ctl.switch].append(ctl)
            wanted = REQUIRED_GESTURE[ctl.action]
            if ctl.gesture != wanted:
                reason = {
                    "go": "GO is never inferred from a multi-gesture switch",
                    "extend": "EXTEND acts on the press edge",
                    "stop": "STOP acts on the press edge and is never a long press",
                    "back": "BACK is a tap",
                    "manual": "MANUAL is a hold",
                }[ctl.action]
                errors.append(
                    f"control {ctl.action} on '{ctl.switch}' uses gesture '{ctl.gesture}'; "
                    f"it must be '{wanted}' ({reason}, ADR-010)"
                )
        actions = [c.action for c in self.controls]
        for dup in sorted({a for a in actions if actions.count(a) > 1}):
            errors.append(f"action '{dup}' is mapped more than once")
        for needed in ("go", "stop"):
            if needed not in actions:
                errors.append(f"the control map needs a '{needed}' control (ADR-010)")
        for switch_id, group in by_switch.items():
            names = sorted(c.action for c in group)
            if len(names) < 2:
                continue
            if FORWARD_ACTIONS & set(names):
                errors.append(
                    f"switch '{switch_id}' mixes a forward action with {names}; no switch may "
                    "combine a forward action with a backward or safety action (I-17)"
                )
            elif "stop" in names:
                errors.append(f"switch '{switch_id}': STOP must have its own switch (ADR-010)")
            gestures = [c.gesture for c in group]
            for dup in sorted({g for g in gestures if gestures.count(g) > 1}):
                errors.append(f"switch '{switch_id}' has two controls on gesture '{dup}'")
        raise_if(errors)
        return self
