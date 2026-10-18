"""The output supervisor (plan Sections 4, 5.2, 5.5; ADR-009, ADR-012).

Everything that reaches an adapter passes through ``Supervisor.submit``. Per commit:

- Nothing is sent while STOP is active, or for a commit from an older generation (I-18).
- A one-shot or state occurrence already in the fired ledger is never sent again
  automatically (I-5, I-7). Only an explicit operator resync may resend *state*; one-shots are
  never resent (I-15).

Per intent (``device.command = value``):

- The device must be in the rig and owned by Roobotto; the command and value must exist.
- The port must be connected; a disconnected port drops the send, nothing is queued.
- Per-device rate limits apply (I-15): two sends to one device, through any of its commands
  and within one commit too, are at least the larger of the two commands' ``min_interval_ms``
  apart.
- Every device listening on the port and channel must be the intended recipient or declared
  ``ignores`` (I-13). The rig validator checks this too; the supervisor re-checks at runtime.
- Routing: the MIDI adapter only for a physical port in a physical profile, with a
  ``bench_verified`` command, a known wire message, armed outputs and an enabled adapter.
  Everything else goes to the mock adapter (I-1, I-2). There is no override.

Before sending the survivors of a non-resync commit, an "attempting" record is appended to the
ledger and flushed; the result follows the sends. Every attempt becomes an ``OutputAttempt``
record, and every refusal a ``Refusal`` (I-11). An intent refused here (a rate limit, say) is not
retried later: once any intent of the occurrence is attempted, the ledger closes the occurrence.
"""

from collections import defaultdict
from dataclasses import dataclass
from typing import Any, Literal

from roobotto.adapters.base import AdapterName, SendResult
from roobotto.adapters.midi import MidiAdapter
from roobotto.adapters.midi_wire import encode
from roobotto.adapters.mock import MockAdapter
from roobotto.config.rig import Command, Port, Rig, WireMessage
from roobotto.cues.ledger import FiredLedger
from roobotto.messages import (
    Authorization,
    CueClass,
    FiredLedgerRecord,
    OccurrenceId,
    OutputAttempt,
)

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Intent:
    device: str
    command: str
    value: str

    @property
    def key(self) -> str:
        return f"{self.device}.{self.command}"


@dataclass(frozen=True)
class Commit:
    """A request from the engine to send one occurrence's intents now."""

    occurrence: OccurrenceId
    generation: int
    cue_class: CueClass
    intents: tuple[Intent, ...]
    reason: str
    window_end_ns: int
    authorized_by: Authorization | None
    resync: bool = False

    def as_record(self, t_ns: int) -> dict[str, Any]:
        return {
            "type": "commit",
            "t_ns": t_ns,
            "occurrence": self.occurrence.model_dump(mode="json"),
            "generation": self.generation,
            "class": self.cue_class,
            "intents": [f"{i.key}={i.value}" for i in self.intents],
            "reason": self.reason,
            "window_end_ns": self.window_end_ns,
            "authorized_by": self.authorized_by.model_dump(mode="json")
            if self.authorized_by
            else None,
            "resync": self.resync,
        }


@dataclass(frozen=True)
class Refusal:
    t_ns: int
    occurrence: OccurrenceId
    intent: str | None
    reason: str

    def as_record(self) -> dict[str, Any]:
        return {
            "type": "refusal",
            "t_ns": self.t_ns,
            "occurrence": self.occurrence.model_dump(mode="json"),
            "intent": self.intent,
            "reason": self.reason,
        }


@dataclass(frozen=True)
class _Planned:
    intent: Intent
    command: Command
    port: Port
    wire: WireMessage | None
    adapter: AdapterName
    route: str


class Supervisor:
    def __init__(
        self,
        rig: Rig,
        ledger: FiredLedger,
        *,
        mock: MockAdapter,
        midi: MidiAdapter,
        config_hash: str,
    ) -> None:
        self.rig = rig
        self.ledger = ledger
        self.mock = mock
        self.midi = midi
        self.config_hash = config_hash
        self.disconnected: set[str] = set()
        self._last_send: dict[str, tuple[int, int]] = {}
        """Device id -> (time of its last send, that command's min interval in ns)."""
        self._ports = {p.id: p for p in rig.ports}
        self._listeners: dict[tuple[str, int], list[tuple[str, str]]] = defaultdict(list)
        for dev in rig.devices:
            for listen in dev.receives:
                self._listeners[(listen.port, listen.channel)].append((dev.id, listen.behavior))

    def set_port(self, port: str, connected: bool) -> None:
        if connected:
            self.disconnected.discard(port)
        else:
            self.disconnected.add(port)

    def _route(
        self, command: Command, port: Port, wire: WireMessage | None, armed: bool
    ) -> tuple[AdapterName, str]:
        if self.rig.profile != "physical" or port.transport == "mock":
            return "mock", "mock port"
        if command.status != "bench_verified" or command.evidence.bench_log is None or wire is None:
            return "mock", "unverified mapping: mock only, no override (I-1)"
        if not armed:
            return "mock", "outputs not armed: mock only (I-2)"
        if not self.midi.enabled:
            return "mock", "MIDI adapter disabled: mock only"
        return "midi", "armed and bench_verified"

    def _check(
        self,
        intent: Intent,
        now_ns: int,
        armed: bool,
        resync: bool,
        recent: dict[str, tuple[int, int]],
    ) -> _Planned | str:
        device = self.rig.device(intent.device)
        if device is None:
            return f"device '{intent.device}' is not in the rig"
        if device.owner != "roobotto":
            return f"device '{intent.device}' is owned by the performer (one owner per device)"
        command = device.commands.get(intent.command)
        if command is None:
            return f"device '{intent.device}' has no command '{intent.command}'"
        if intent.value not in command.values:
            return f"'{intent.value}' is not a value of {intent.key}"
        if resync and command.type != "state":
            return "one-shots are never resent; resync covers retryable state only (I-15)"
        port = self._ports.get(command.port)
        if port is None:
            return f"unknown port '{command.port}'"
        if command.port in self.disconnected:
            return f"port '{command.port}' is disconnected; nothing is queued or resent later"
        interval = command.min_interval_ms * NS_PER_MS
        last = recent.get(intent.device)
        if last is not None:
            required = max(interval, last[1])
            if now_ns - last[0] < required:
                return (
                    f"rate limit: device '{intent.device}' needs {required // NS_PER_MS} ms "
                    "between sends (I-15)"
                )
        listeners = self._listeners[(command.port, command.channel)]
        if (intent.device, "intended") not in listeners:
            return (
                f"'{intent.device}' does not list {command.port} ch {command.channel} as an "
                "intended input (I-13)"
            )
        for other, behavior in listeners:
            wanted = "intended" if other == intent.device else "ignores"
            if behavior != wanted:
                return (
                    f"recipient '{other}' on {command.port} ch {command.channel} is "
                    f"'{behavior}', not '{wanted}' (I-13)"
                )
        wire = command.values[intent.value]
        adapter, route = self._route(command, port, wire, armed)
        return _Planned(intent, command, port, wire, adapter, route)

    def submit(
        self,
        commit: Commit,
        *,
        now_ns: int,
        generation: int,
        stopped: bool,
        armed: bool,
    ) -> tuple[list[OutputAttempt], list[Refusal]]:
        occurrence = commit.occurrence

        def refuse_all(reason: str) -> tuple[list[OutputAttempt], list[Refusal]]:
            return [], [Refusal(now_ns, occurrence, None, reason)]

        if stopped:
            return refuse_all("STOP is active: nothing is sent (I-18)")
        if commit.generation != generation:
            return refuse_all(
                f"stale generation {commit.generation} (current {generation}): pending work "
                "was invalidated"
            )
        if not commit.resync and self.ledger.contains(occurrence):
            return refuse_all("occurrence is already in the fired ledger: at most once (I-5, I-7)")
        if commit.resync and commit.cue_class == "critical":
            return refuse_all("resync never resends a critical cue (I-14)")

        refusals: list[Refusal] = []
        planned: list[_Planned] = []
        recent = dict(self._last_send)
        for intent in commit.intents:
            checked = self._check(intent, now_ns, armed, commit.resync, recent)
            if isinstance(checked, str):
                refusals.append(Refusal(now_ns, occurrence, intent.key, checked))
            else:
                planned.append(checked)
                recent[intent.device] = (now_ns, checked.command.min_interval_ms * NS_PER_MS)
        if not planned:
            return [], refusals

        if not commit.resync:
            self.ledger.append(
                FiredLedgerRecord(
                    occurrence=occurrence, first_attempt_ns=now_ns, result="attempting"
                )
            )
        attempts: list[OutputAttempt] = []
        for p in planned:
            data = (
                encode(p.wire.kind, p.command.channel, p.wire.data1, p.wire.data2)
                if p.wire is not None
                else b""
            )
            target = self.midi if p.adapter == "midi" else self.mock
            result: SendResult = target.send(p.port.id, data, f"{occurrence.key} {p.intent.key}")
            self._last_send[p.intent.device] = (now_ns, p.command.min_interval_ms * NS_PER_MS)
            attempts.append(
                OutputAttempt(
                    occurrence=occurrence,
                    generation=commit.generation,
                    adapter=p.adapter,
                    device=p.intent.device,
                    command=p.intent.key,
                    value=p.intent.value,
                    bytes_sent=list(data),
                    send_time_ns=now_ns,
                    config_hash=self.config_hash,
                    armed=armed,
                    reason=f"{commit.reason}; {p.route}",
                    result=result,
                )
            )
        if not commit.resync:
            self.ledger.append(
                FiredLedgerRecord(
                    occurrence=occurrence,
                    first_attempt_ns=now_ns,
                    result=_combine([a.result for a in attempts]),
                )
            )
        return attempts, refusals


def _combine(
    results: list[Literal["attempted", "confirmed", "failed", "unknown"]],
) -> Literal["attempted", "confirmed", "failed", "unknown"]:
    if "failed" in results:
        return "failed"
    if "unknown" in results:
        return "unknown"
    if all(r == "confirmed" for r in results):
        return "confirmed"
    return "attempted"
