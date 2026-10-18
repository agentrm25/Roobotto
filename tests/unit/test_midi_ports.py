"""The mido backend, driven through an injected fake port. No test opens a real port."""

from typing import Any

import pytest

from roobotto.adapters.midi import MidiAdapter
from roobotto.adapters.midi_ports import MidoBackend
from roobotto.adapters.midi_wire import encode


class FakePort:
    def __init__(self, name: str) -> None:
        self.name = name
        self.sent: list[Any] = []
        self.closed = False

    def send(self, msg: Any) -> None:
        self.sent.append(msg)

    def close(self) -> None:
        self.closed = True


def make() -> tuple[MidoBackend, dict[str, FakePort]]:
    opened: dict[str, FakePort] = {}

    def opener(name: str) -> FakePort:
        opened[name] = FakePort(name)
        return opened[name]

    return MidoBackend({"usb_out": "Fake MIDI Out 1"}, opener=opener), opened


def test_bytes_reach_the_named_port_unchanged() -> None:
    backend, opened = make()
    data = encode("note_on", 3, 61, 127)
    backend.send("usb_out", data)
    backend.send("usb_out", encode("program_change", 16, 5))
    port = opened["Fake MIDI Out 1"]
    assert [m.bytes() for m in port.sent] == [list(data), [0xCF, 5]]
    assert port.sent[0].type == "note_on" and port.sent[0].channel == 2  # mido counts 0-15
    backend.close()
    assert port.closed


def test_unconfigured_port_is_an_error_and_the_adapter_reports_failure() -> None:
    backend, opened = make()
    with pytest.raises(KeyError):
        backend.send("other", b"\x90\x3c\x7f")
    assert MidiAdapter(backend).send("other", b"\x90\x3c\x7f", "x") == "failed"
    assert opened == {}


def test_nothing_constructs_a_real_backend_by_default() -> None:
    assert not MidiAdapter().enabled
