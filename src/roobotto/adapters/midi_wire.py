"""MIDI wire encoding. The only place MIDI channel numbers are converted (ADR-007, I-10).

Config and logs number channels 1-16. The wire carries 0-15 in the low nibble of the status
byte. ``convert_channel`` is the single conversion; everything else calls it.
"""

from typing import Literal

WireKind = Literal["note_on", "note_off", "control_change", "program_change"]

_STATUS: dict[WireKind, int] = {
    "note_off": 0x80,
    "note_on": 0x90,
    "control_change": 0xB0,
    "program_change": 0xC0,
}
_KIND_OF_STATUS: dict[int, WireKind] = {status: kind for kind, status in _STATUS.items()}


def convert_channel(channel: int, *, to: Literal["wire", "config"]) -> int:
    """Convert a channel number: config 1-16 to wire 0-15, or back."""
    if to == "wire":
        if not 1 <= channel <= 16:
            raise ValueError(f"config channel {channel} is outside 1-16")
        return channel - 1
    if not 0 <= channel <= 15:
        raise ValueError(f"wire channel {channel} is outside 0-15")
    return channel + 1


def encode(kind: WireKind, channel: int, data1: int, data2: int | None = None) -> bytes:
    """Bytes for one channel message. ``channel`` is 1-16, as in config."""
    if not 0 <= data1 <= 127 or (data2 is not None and not 0 <= data2 <= 127):
        raise ValueError("MIDI data bytes are 0-127")
    status = _STATUS[kind] | convert_channel(channel, to="wire")
    if kind == "program_change":
        if data2 is not None:
            raise ValueError("program_change takes one data byte")
        return bytes((status, data1))
    if data2 is None:
        raise ValueError(f"{kind} needs two data bytes")
    return bytes((status, data1, data2))


def decode(data: bytes) -> tuple[WireKind, int, int, int | None]:
    """``(kind, channel 1-16, data1, data2)`` for one channel message."""
    if not data:
        raise ValueError("empty MIDI message")
    kind = _KIND_OF_STATUS.get(data[0] & 0xF0)
    if kind is None:
        raise ValueError(f"unsupported MIDI status byte 0x{data[0]:02x}")
    channel = convert_channel(data[0] & 0x0F, to="config")
    expected = 2 if kind == "program_change" else 3
    if len(data) != expected or any(b > 127 for b in data[1:]):
        raise ValueError(f"malformed {kind} message: {data.hex()}")
    return kind, channel, data[1], (data[2] if expected == 3 else None)
