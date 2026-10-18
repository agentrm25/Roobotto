"""MIDI wire encoding and the single channel conversion (ADR-007, I-10)."""

import re
from pathlib import Path

import pytest

from roobotto.adapters.midi_wire import convert_channel, decode, encode

SRC = Path(__file__).parents[2] / "src" / "roobotto"


@pytest.mark.parametrize("channel", range(1, 17))
def test_channel_round_trip(channel: int) -> None:
    wire = convert_channel(channel, to="wire")
    assert wire == channel - 1
    assert convert_channel(wire, to="config") == channel


@pytest.mark.parametrize(
    ("channel", "to"), [(0, "wire"), (17, "wire"), (-1, "config"), (16, "config")]
)
def test_channel_out_of_range(channel: int, to: str) -> None:
    with pytest.raises(ValueError):
        convert_channel(channel, to=to)  # type: ignore[arg-type]


def test_encode_and_decode() -> None:
    assert encode("note_on", 3, 61, 127) == bytes([0x92, 61, 127])
    assert encode("control_change", 1, 69, 2) == bytes([0xB0, 69, 2])
    assert encode("program_change", 16, 5) == bytes([0xCF, 5])
    assert decode(bytes([0x92, 61, 127])) == ("note_on", 3, 61, 127)
    assert decode(bytes([0xCF, 5])) == ("program_change", 16, 5, None)


@pytest.mark.parametrize(
    "data", [b"", bytes([0xF0, 1]), bytes([0x90, 60]), bytes([0xC0, 1, 2]), bytes([0x90, 200, 1])]
)
def test_decode_rejects_malformed(data: bytes) -> None:
    with pytest.raises(ValueError):
        decode(data)


def test_encode_rejects_bad_data() -> None:
    with pytest.raises(ValueError):
        encode("program_change", 1, 5, 5)
    with pytest.raises(ValueError):
        encode("note_on", 1, 60)
    with pytest.raises(ValueError):
        encode("note_on", 1, 128, 1)


def test_i10_channel_conversion_lives_in_one_function() -> None:
    """No module but midi_wire.py does channel arithmetic or masks the channel nibble."""
    pattern = re.compile(r"channel\s*[-+]\s*1\b|0x0[fF]\b|&\s*15\b|\|\s*\(?channel\b")
    offenders = [
        f"{path.relative_to(SRC)}:{n}"
        for path in SRC.rglob("*.py")
        if path.name != "midi_wire.py"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if pattern.search(line)
    ]
    assert offenders == []
    wire = (SRC / "adapters" / "midi_wire.py").read_text(encoding="utf-8")
    assert wire.count("channel - 1") == 1 and wire.count("channel + 1") == 1
