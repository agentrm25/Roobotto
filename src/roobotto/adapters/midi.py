"""The generic MIDI output adapter. Disabled by default (ADR-009).

The adapter is disabled unless it is constructed with a backend that opens real ports. No such
backend ships yet: it needs mido/python-rtmidi, a dependency the owner has not approved
(AGENTS.md). Even with a backend, the output supervisor routes a command here only when the rig
profile is physical, the command is ``bench_verified``, its port is physical, and outputs are
armed for this run (I-1, I-2). There is no override.
"""

from typing import Protocol

from roobotto.adapters.base import AdapterName, SendResult


class MidiBackend(Protocol):
    def send(self, port: str, data: bytes) -> None: ...


class NullBackend:
    """Discards every message. Replay uses it so routing decisions match the recorded run."""

    def send(self, port: str, data: bytes) -> None:
        return None


class MidiAdapter:
    def __init__(self, backend: MidiBackend | None = None) -> None:
        self._backend = backend

    @property
    def name(self) -> AdapterName:
        return "midi"

    @property
    def enabled(self) -> bool:
        return self._backend is not None

    def send(self, port: str, data: bytes, label: str) -> SendResult:
        if self._backend is None:
            return "failed"
        try:
            self._backend.send(port, data)
        except Exception:
            return "failed"
        # MIDI has no acknowledgement: the send was attempted, the effect is unknown.
        return "attempted"
