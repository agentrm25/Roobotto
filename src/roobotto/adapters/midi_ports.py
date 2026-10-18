"""Real MIDI output ports, through mido and python-rtmidi.

Nothing in Roobotto constructs this backend by default, and no test opens a real port. To send
to hardware, a run must build it explicitly, hand it to ``MidiAdapter``, and every send must
still pass the supervisor (physical profile, ``bench_verified`` command, armed outputs). No live
run exists yet: ``roobotto run`` without ``--dry-run`` is refused.

Rig port ids are Roobotto's names. The operating system's port names are machine-specific, so
they are passed in separately (``port_names``) and never stored in the rig.

The bytes come from ``midi_wire.encode``; this module does no channel arithmetic (I-10).
"""

import importlib
from collections.abc import Callable, Mapping
from typing import Any, Protocol


class OutputPort(Protocol):
    def send(self, msg: Any) -> None: ...

    def close(self) -> None: ...


Opener = Callable[[str], OutputPort]


def _mido() -> Any:
    """mido ships no type information, so it is loaded dynamically behind typed wrappers."""
    return importlib.import_module("mido")


def _open_output(name: str) -> OutputPort:
    port: OutputPort = _mido().open_output(name)
    return port


def output_names() -> list[str]:
    """The operating system's MIDI output port names. Opens nothing."""
    names: list[str] = list(_mido().get_output_names())
    return names


class MidoBackend:
    """A ``MidiBackend`` that writes to real ports. Ports are opened on first use."""

    def __init__(self, port_names: Mapping[str, str], *, opener: Opener | None = None) -> None:
        self._names = dict(port_names)
        self._opener = opener or _open_output
        self._open: dict[str, OutputPort] = {}

    def send(self, port: str, data: bytes) -> None:
        name = self._names.get(port)
        if name is None:
            raise KeyError(f"no system port name is configured for rig port '{port}'")
        out = self._open.get(port)
        if out is None:
            out = self._opener(name)
            self._open[port] = out
        out.send(_mido().Message.from_bytes(list(data)))

    def close(self) -> None:
        for out in self._open.values():
            out.close()
        self._open.clear()
