"""The recorder (T-07): capture from one or more audio devices to one FLAC per channel, with
per-block metadata, every MIDI input port logged, sync-burst times, and a manifest.

Rule 8 (AGENTS.md): the audio callback only timestamps and enqueues. ``Callback.__call__`` copies
the block the driver hands it, stamps it with the driver's ADC time and callback time and the
overflow and underflow flags, and appends it to a ``deque``. ``deque.append`` is atomic under
the interpreter lock, so the callback shares no lock with the worker, does no parsing, touches no
disk and no network. The worker thread drains the deques, writes FLAC and metadata, and nothing
else. Essential guitar and vocal audio is only ever captured here, never played out (rule 9).

Real devices come through ``SoundDeviceSource`` (sounddevice, loaded lazily). Tests use
``FakeSource``, which produces blocks with controllable timing, gaps and flags on its own thread.
"""

import hashlib
import importlib
import json
import threading
import time
from collections import deque
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol

import numpy as np
import numpy.typing as npt
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs]
import yaml
from pydantic import Field, ValidationError, field_validator

from roobotto.config.common import Id, Model, NonNegInt, PosInt, Text
from roobotto.config.loader import issues_from_validation_error
from roobotto.engine.show import canonical_json
from roobotto.eval.annotations import Stream, TakeManifest

Array = npt.NDArray[np.float64]
NS = 1_000_000_000


class CaptureError(ValueError):
    pass


# --- Session configuration ---


class DeviceSpec(Model):
    id: Id
    """Roobotto's name for the device in this session."""
    name: Text
    """The operating system's device name (as ``roobotto devices list`` prints it)."""
    channels: dict[NonNegInt, Stream]
    """Input channel index (from 0) -> stream name. Only mapped channels are written."""

    @field_validator("channels", mode="before")
    @classmethod
    def _int_keys(cls, value: Any) -> Any:
        """JSON and YAML may spell the channel index as text; accept digits."""
        if isinstance(value, dict):
            out: dict[Any, Any] = {}
            raw: dict[Any, Any] = value  # pyright: ignore[reportUnknownVariableType]
            for k, v in raw.items():
                out[int(k) if isinstance(k, str) and k.isdigit() else k] = v
            return out
        return value  # pyright: ignore[reportUnknownVariableType]


class ClockConfig(Model):
    """Operator-entered: PortAudio cannot read macOS Aggregate Device settings (E-16)."""

    aggregate_device: Text | None = None
    members: list[Text] = Field(default_factory=lambda: [])
    clock_source: Text | None = None
    drift_correction: list[Text] = Field(default_factory=lambda: [])
    notes: Text | None = None


class BurstConfig(Model):
    """The sync burst: an audio click shared by every input. The recorder records when it
    asked for one; detection is the validator's job. Playback is opt-in and needs an output
    device named here; nothing plays by default."""

    play_on: Text | None = None
    seconds: float = 0.05
    hz: float = 1000.0
    level: float = 0.5


class SessionConfig(Model):
    kind: Literal["session"]
    schema_version: Literal[0]
    id: Id
    song: Id
    take_type: Literal["rehearsal", "bench"]
    sample_rate: PosInt = 48_000
    bit_depth: Literal[16, 24] = 24
    block: PosInt = 512
    devices: list[DeviceSpec] = Field(min_length=1)
    midi_ports: Literal["all"] | list[Text] = "all"
    """Input ports to log; ``all`` opens every input port the system lists."""
    clock: ClockConfig = Field(default_factory=ClockConfig)
    burst: BurstConfig = Field(default_factory=BurstConfig)
    consent: Literal["given", "none"]
    """Whether every performer consented to this recording (ADR-008)."""


def load_session(path: Path) -> SessionConfig:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise CaptureError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise CaptureError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return SessionConfig.model_validate(raw)
    except ValidationError as exc:
        raise CaptureError(
            "\n".join(str(i) for i in issues_from_validation_error(str(path), exc))
        ) from exc


# --- Blocks and the callback ---


@dataclass(frozen=True)
class Block:
    device: str
    index: int
    """Running block count from this device, from 0."""
    sample_index: int
    """First sample's index in the device's stream, from 0 (frames delivered so far)."""
    adc_time: float
    """The driver's ADC capture time of the first sample (seconds, driver clock)."""
    callback_time: float
    """The driver's time when the callback ran (seconds, same clock)."""
    overflow: bool
    underflow: bool
    frames: int
    samples: npt.NDArray[np.float32]
    """frames x channels, a copy of the driver's buffer."""


class Callback:
    """What the driver calls. It stamps, copies and appends; nothing else (rule 8)."""

    def __init__(self, device: str, queue: deque[Block]) -> None:
        self.device = device
        self.queue = queue
        self._index = 0
        self._samples = 0

    def __call__(self, indata: Any, frames: int, time_info: Any, status: Any) -> None:
        block = Block(
            self.device,
            self._index,
            self._samples,
            float(getattr(time_info, "inputBufferAdcTime", 0.0)),
            float(getattr(time_info, "currentTime", 0.0)),
            bool(getattr(status, "input_overflow", False)),
            bool(getattr(status, "input_underflow", False)),
            frames,
            np.array(indata, dtype=np.float32, copy=True),
        )
        self._index += 1
        self._samples += frames
        self.queue.append(block)


class AudioSource(Protocol):
    device: str

    def start(self, callback: Callback) -> None: ...

    def stop(self) -> None: ...


class SoundDeviceSource:
    """A real input device through sounddevice. Opened only when started."""

    def __init__(self, device: str, name: str, channels: int, rate: int, block: int) -> None:
        self.device = device
        self.name = name
        self.channels = channels
        self.rate = rate
        self.block = block
        self._stream: Any = None

    def start(self, callback: Callback) -> None:
        sd: Any = importlib.import_module("sounddevice")
        self._stream = sd.InputStream(
            device=self.name,
            channels=self.channels,
            samplerate=self.rate,
            blocksize=self.block,
            dtype="float32",
            callback=callback,
        )
        self._stream.start()

    def stop(self) -> None:
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None


@dataclass
class FakeSource:
    """Delivers blocks on its own thread like a driver would. ``drop_blocks`` are block indices
    the driver never delivers (sample and ADC time jump), ``overflow_blocks`` are flagged."""

    device: str
    channels: int
    rate: int
    block: int
    blocks: int
    signal: Callable[[int, int], Array] | None = None
    """(first sample, frames) -> frames x channels samples; zeros when None."""
    drop_blocks: frozenset[int] = frozenset()
    overflow_blocks: frozenset[int] = frozenset()
    adc_offset_s: float = 0.0
    drift_ppm: float = 0.0
    pace_s: float = 0.0
    """Real seconds to wait between blocks (0 delivers as fast as possible)."""
    _thread: threading.Thread | None = None
    _callback: Callback | None = None
    delivered: int = 0

    def start(self, callback: Callback) -> None:
        self._callback = callback
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        assert self._callback is not None
        first = 0

        class _Time:
            def __init__(self, adc: float, now: float) -> None:
                self.inputBufferAdcTime = adc
                self.currentTime = now

        class _Status:
            def __init__(self, overflow: bool) -> None:
                self.input_overflow = overflow
                self.input_underflow = False

        for k in range(self.blocks):
            nominal_t = first / self.rate
            adc = self.adc_offset_s + nominal_t * (1.0 + self.drift_ppm * 1e-6)
            if k in self.drop_blocks:
                first += self.block
                continue
            if self.signal is not None:
                data = np.asarray(self.signal(first, self.block), dtype=np.float32)
            else:
                data = np.zeros((self.block, self.channels), dtype=np.float32)
            self._callback(
                data, self.block, _Time(adc, adc + 0.002), _Status(k in self.overflow_blocks)
            )
            self.delivered += 1
            first += self.block
            if self.pace_s:
                time.sleep(self.pace_s)

    def stop(self) -> None:
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None


# --- MIDI input logging (listen only) ---


class MidiInputLogger:
    """Logs every message on every input port with the monotonic time it was received. Listen
    only: it never opens an output. The opener is injectable so tests use none of the real ports."""

    def __init__(
        self,
        ports: Sequence[str],
        path: Path,
        *,
        opener: Callable[[str, Callable[[Any], None]], Any] | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        self.ports = list(ports)
        self.path = path
        self._opener = opener or _open_mido_input
        self._clock = clock_ns
        self._open: list[Any] = []
        self._handle: Any = None
        self.count = 0

    def start(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self.path.open("a", encoding="utf-8")
        for port in self.ports:
            self._open.append(self._opener(port, self._make(port)))

    def _make(self, port: str) -> Callable[[Any], None]:
        def on_message(msg: Any) -> None:
            record: dict[str, Any] = {"t_ns": self._clock(), "port": port, "message": str(msg)}
            if hasattr(msg, "bytes"):
                record["bytes"] = list(msg.bytes())
            self.count += 1
            if self._handle is not None:
                self._handle.write(canonical_json(record) + "\n")
                self._handle.flush()

        return on_message

    def stop(self) -> None:
        for port in self._open:
            close = getattr(port, "close", None)
            if close is not None:
                close()
        self._open = []
        if self._handle is not None:
            self._handle.close()
            self._handle = None


def _open_mido_input(name: str, callback: Callable[[Any], None]) -> Any:
    mido: Any = importlib.import_module("mido")
    return mido.open_input(name, callback=callback)


def midi_input_names() -> list[str]:
    mido: Any = importlib.import_module("mido")
    return list(mido.get_input_names())


# --- The session ---


@dataclass
class _Writer:
    files: dict[str, Any]
    """stream name -> open SoundFile."""
    meta: Any
    """open handle for blocks.jsonl."""
    frames: dict[str, int] = field(default_factory=lambda: {})
    thread_ids: set[int] = field(default_factory=lambda: set())


@dataclass(frozen=True)
class CaptureResult:
    folder: Path
    manifest: TakeManifest
    blocks_written: dict[str, int]
    overflow_blocks: int
    midi_messages: int


class Session:
    """Owns the sources, the queues, the worker and the files for one take."""

    def __init__(
        self,
        config: SessionConfig,
        folder: Path,
        sources: Sequence[AudioSource],
        *,
        midi: MidiInputLogger | None = None,
        clock_ns: Callable[[], int] = time.monotonic_ns,
    ) -> None:
        if config.consent != "given" and config.take_type == "rehearsal":
            raise CaptureError("a rehearsal take needs consent: given (ADR-008)")
        self.config = config
        self.folder = folder
        self.sources = list(sources)
        self.midi = midi
        self._clock = clock_ns
        self.queues: dict[str, deque[Block]] = {s.device: deque() for s in self.sources}
        self._callbacks = {
            s.device: Callback(s.device, self.queues[s.device]) for s in self.sources
        }
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        self._writer: _Writer | None = None
        self.overflow_blocks = 0
        self.bursts: list[dict[str, Any]] = []
        self.started_ns: int | None = None
        self.stopped_ns: int | None = None

    # -- lifecycle --

    def start(self) -> None:
        self.folder.mkdir(parents=True, exist_ok=True)
        specs = {d.id: d for d in self.config.devices}
        files: dict[str, Any] = {}
        subtype = "PCM_24" if self.config.bit_depth == 24 else "PCM_16"
        for spec in specs.values():
            for _, stream in sorted(spec.channels.items()):
                if stream in files:
                    raise CaptureError(f"stream '{stream}' is mapped twice")
                files[stream] = sf.SoundFile(
                    self.folder / f"{stream}.flac",
                    "w",
                    self.config.sample_rate,
                    1,
                    subtype,
                    format="FLAC",
                )
        meta = (self.folder / "blocks.jsonl").open("w", encoding="utf-8")
        self._writer = _Writer(files, meta, {s: 0 for s in files})
        self.started_ns = self._clock()
        if self.midi is not None:
            self.midi.start()
        self._worker = threading.Thread(target=self._drain_forever, daemon=True)
        self._worker.start()
        for source in self.sources:
            source.start(self._callbacks[source.device])

    def mark_burst(self, which: Literal["start", "end"]) -> None:
        """Record that the sync burst was asked for now; playback is opt-in and external."""
        self.bursts.append({"which": which, "t_ns": self._clock()})

    def stop(self) -> CaptureResult:
        for source in self.sources:
            source.stop()
        self._stop.set()
        if self._worker is not None:
            self._worker.join(timeout=60)
        self._drain()  # whatever arrived after the worker saw the stop flag
        self.stopped_ns = self._clock()
        if self.midi is not None:
            self.midi.stop()
        writer = self._writer
        assert writer is not None
        for handle in writer.files.values():
            handle.close()
        writer.meta.close()
        manifest = self._manifest(writer)
        (self.folder / "manifest.json").write_text(
            manifest.model_dump_json(indent=1) + "\n", encoding="utf-8"
        )
        (self.folder / "capture.json").write_text(
            json.dumps(self._capture_info(writer), indent=1, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        return CaptureResult(
            self.folder,
            manifest,
            dict(writer.frames),
            self.overflow_blocks,
            self.midi.count if self.midi else 0,
        )

    # -- the worker --

    def _drain_forever(self) -> None:
        while not self._stop.is_set():
            if not self._drain():
                time.sleep(0.002)

    def _drain(self) -> bool:
        """Write every queued block. Returns whether anything was written."""
        writer = self._writer
        assert writer is not None
        wrote = False
        specs = {d.id: d for d in self.config.devices}
        for device, queue in self.queues.items():
            while queue:
                block = queue.popleft()
                wrote = True
                writer.thread_ids.add(threading.get_ident())
                if block.overflow or block.underflow:
                    self.overflow_blocks += 1
                for channel, stream in specs[device].channels.items():
                    column = block.samples[:, channel] if block.samples.ndim == 2 else block.samples
                    writer.files[stream].write(column)
                    writer.frames[stream] += block.frames
                writer.meta.write(
                    canonical_json(
                        {
                            "device": block.device,
                            "block": block.index,
                            "sample_index": block.sample_index,
                            "frames": block.frames,
                            "adc_time": block.adc_time,
                            "callback_time": block.callback_time,
                            "overflow": block.overflow,
                            "underflow": block.underflow,
                            "received_ns": self._clock(),
                        }
                    )
                    + "\n"
                )
        return wrote

    # -- files --

    def _manifest(self, writer: _Writer) -> TakeManifest:
        channels: dict[str, str] = {s: f"{s}.flac" for s in writer.files}
        hashes = {
            f: hashlib.sha256((self.folder / f).read_bytes()).hexdigest() for f in channels.values()
        }
        return TakeManifest(
            kind="take_manifest",
            schema_version=0,
            take=f"{self.config.song}/{self.config.id}",
            song=self.config.song,
            take_type=self.config.take_type,
            sample_rate=self.config.sample_rate,
            bit_depth=self.config.bit_depth,
            channels=channels,  # pyright: ignore[reportArgumentType]
            sha256=hashes,
            devices=[d.name for d in self.config.devices],
            consent=self.config.consent,
        )

    def _capture_info(self, writer: _Writer) -> dict[str, Any]:
        return {
            "kind": "capture",
            "schema_version": 0,
            "session": self.config.id,
            "started_ns": self.started_ns,
            "stopped_ns": self.stopped_ns,
            "sample_rate": self.config.sample_rate,
            "block": self.config.block,
            "devices": [d.model_dump() for d in self.config.devices],
            "clock": self.config.clock.model_dump(),
            "burst": self.config.burst.model_dump(),
            "bursts": self.bursts,
            "frames": dict(writer.frames),
            "overflow_blocks": self.overflow_blocks,
            "blocks_file": "blocks.jsonl",
            "midi_log": "midi.jsonl" if self.midi else None,
            "midi_ports": self.midi.ports if self.midi else [],
            "streams": "as-captured",
        }


def read_blocks(folder: Path) -> list[dict[str, Any]]:
    path = folder / "blocks.jsonl"
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line]


def burst_signal(rate: int, seconds: float, hz: float, level: float) -> Array:
    t = np.arange(round(seconds * rate)) / rate
    return level * np.sin(2 * np.pi * hz * t)
