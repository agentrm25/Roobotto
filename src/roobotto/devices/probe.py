"""What this machine can see: MIDI port names and audio devices, read without opening anything.

mido and sounddevice are loaded lazily. sounddevice needs the PortAudio library, which the
macOS wheel bundles and Linux does not ship by default, so its absence is reported, not raised.
The ``System`` protocol lets CI run every tool against ``FakeSystem``.
"""

import importlib
import importlib.metadata
import sys
from collections.abc import Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Protocol

import yaml

from roobotto.config.loader import load_config
from roobotto.config.rig import Rig

MODULES = ("mido", "rtmidi", "sounddevice")
DISTRIBUTIONS = {"rtmidi": "python-rtmidi"}
"""Module name -> distribution name, where they differ."""


@dataclass(frozen=True)
class AudioDevice:
    index: int
    name: str
    inputs: int
    outputs: int
    default_samplerate: float
    hostapi: str


class System(Protocol):
    def python_version(self) -> tuple[int, int, int]: ...

    def module_version(self, name: str) -> str | None:
        """The module's version, or ``None`` when it cannot be imported."""
        ...

    def midi_inputs(self) -> list[str]: ...

    def midi_outputs(self) -> list[str]: ...

    def audio_devices(self) -> list[AudioDevice]: ...


@dataclass(frozen=True)
class FakeSystem:
    midi_inputs_: Sequence[str] = ()
    midi_outputs_: Sequence[str] = ()
    audio: Sequence[AudioDevice] = ()
    modules: dict[str, str | None] = field(default_factory=lambda: dict.fromkeys(MODULES, "0"))
    python: tuple[int, int, int] = (3, 12, 0)

    def __init__(
        self,
        midi_inputs: Sequence[str] = (),
        midi_outputs: Sequence[str] = (),
        audio: Sequence[AudioDevice] = (),
        modules: dict[str, str | None] | None = None,
        python: tuple[int, int, int] = (3, 12, 0),
    ) -> None:
        object.__setattr__(self, "midi_inputs_", tuple(midi_inputs))
        object.__setattr__(self, "midi_outputs_", tuple(midi_outputs))
        object.__setattr__(self, "audio", tuple(audio))
        object.__setattr__(self, "modules", dict(modules or dict.fromkeys(MODULES, "0")))
        object.__setattr__(self, "python", python)

    def python_version(self) -> tuple[int, int, int]:
        return self.python

    def module_version(self, name: str) -> str | None:
        return self.modules.get(name)

    def midi_inputs(self) -> list[str]:
        return list(self.midi_inputs_)

    def midi_outputs(self) -> list[str]:
        return list(self.midi_outputs_)

    def audio_devices(self) -> list[AudioDevice]:
        if self.modules.get("sounddevice") is None:
            raise RuntimeError("sounddevice is not available: PortAudio library not found")
        return list(self.audio)


class LiveSystem:
    """The real machine. Reads names only; opens no port and no stream."""

    def python_version(self) -> tuple[int, int, int]:
        v = sys.version_info
        return (v.major, v.minor, v.micro)

    def module_version(self, name: str) -> str | None:
        try:
            importlib.import_module(name)
        except Exception:  # an import failure of any kind means not available
            return None
        try:
            return importlib.metadata.version(DISTRIBUTIONS.get(name, name))
        except importlib.metadata.PackageNotFoundError:
            return "unknown"

    def midi_inputs(self) -> list[str]:
        mido: Any = importlib.import_module("mido")
        return list(mido.get_input_names())

    def midi_outputs(self) -> list[str]:
        mido: Any = importlib.import_module("mido")
        return list(mido.get_output_names())

    def audio_devices(self) -> list[AudioDevice]:
        try:
            sd: Any = importlib.import_module("sounddevice")
        except OSError as exc:
            raise RuntimeError(f"sounddevice is not available: {exc}") from exc
        hostapis: list[Any] = list(sd.query_hostapis())
        out: list[AudioDevice] = []
        for index, dev in enumerate(sd.query_devices()):
            api = hostapis[int(dev["hostapi"])]["name"] if hostapis else ""
            out.append(
                AudioDevice(
                    index,
                    str(dev["name"]),
                    int(dev["max_input_channels"]),
                    int(dev["max_output_channels"]),
                    float(dev["default_samplerate"]),
                    str(api),
                )
            )
        return out


def live_system() -> System:
    return LiveSystem()


@dataclass(frozen=True)
class DeviceReport:
    midi_inputs: list[str]
    midi_outputs: list[str]
    audio: list[AudioDevice]
    errors: list[str]

    def as_dict(self) -> dict[str, Any]:
        return {
            "midi_inputs": self.midi_inputs,
            "midi_outputs": self.midi_outputs,
            "audio": [asdict(a) for a in self.audio],
            "errors": self.errors,
        }


def list_devices(system: System) -> DeviceReport:
    errors: list[str] = []
    midi_in: list[str] = []
    midi_out: list[str] = []
    audio: list[AudioDevice] = []
    try:
        midi_in = system.midi_inputs()
        midi_out = system.midi_outputs()
    except Exception as exc:  # reported, never raised, so the rest still lists
        errors.append(f"MIDI ports could not be listed: {exc}")
    try:
        audio = system.audio_devices()
    except Exception as exc:
        errors.append(f"audio devices could not be listed: {exc}")
    return DeviceReport(midi_in, midi_out, audio, errors)


def render(report: DeviceReport) -> list[str]:
    lines = ["MIDI inputs:"]
    lines += [f"  {n}" for n in report.midi_inputs] or ["  (none)"]
    lines += ["MIDI outputs:"]
    lines += [f"  {n}" for n in report.midi_outputs] or ["  (none)"]
    lines += ["Audio devices:"]
    lines += [
        f"  [{a.index}] {a.name}: {a.inputs} in / {a.outputs} out, {a.default_samplerate:g} Hz"
        + (f" ({a.hostapi})" if a.hostapi else "")
        for a in report.audio
    ] or ["  (none)"]
    lines += [f"note: {e}" for e in report.errors]
    return lines


def ports_template(rig_path: Path, report: DeviceReport) -> str:
    """A ``ports`` file skeleton for the rig, with the names seen on this machine as comments."""
    model, issues = load_config(rig_path)
    if issues or not isinstance(model, Rig):
        raise ValueError("the rig file does not validate:\n" + "\n".join(map(str, issues)))
    body = yaml.safe_dump(
        {
            "kind": "ports",
            "schema_version": 0,
            "id": f"{model.id}_ports",
            "rig": model.id,
            "ports": {p.id: None for p in model.ports},
        },
        sort_keys=False,
    )
    head = [
        f"# Ports for rig '{model.id}': rig port id -> system MIDI port name. Save as",
        "# local/ports.yaml (private). Mock ports can stay null.",
        f"# inputs seen: {', '.join(report.midi_inputs) or '(none)'}",
        f"# outputs seen: {', '.join(report.midi_outputs) or '(none)'}",
    ]
    for port in model.ports:
        head.append(f"#   {port.id}: {port.direction}, {port.transport}")
    return "\n".join(head) + "\n" + body
