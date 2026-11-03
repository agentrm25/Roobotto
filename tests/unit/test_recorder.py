"""T-07 acceptance (software side, fake devices): the callback only enqueues; a clean run drops
no buffers; an injected discontinuity is detected and marked invalid; a 10-minute marker
recording aligns within 1 ms after correction, or the error is reported; per-block metadata,
clock configuration, MIDI logging, sync bursts and the manifest are written."""

import inspect
import json
import re
import threading
from collections import deque
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs]
import yaml

from roobotto.cli import main
from roobotto.eval.annotations import TakeManifest
from roobotto.record import align, capture
from roobotto.record.capture import (
    Callback,
    FakeSource,
    MidiInputLogger,
    Session,
    SessionConfig,
    load_session,
)

RATE = 8000
BLOCK = 256


def _config(**overrides: Any) -> SessionConfig:
    data: dict[str, Any] = {
        "kind": "session",
        "schema_version": 0,
        "id": "take_1",
        "song": "synthetic_anthem",
        "take_type": "bench",
        "sample_rate": RATE,
        "block": BLOCK,
        "devices": [
            {"id": "helix", "name": "Helix Stadium", "channels": {0: "guitar", 1: "vocal"}},
            {"id": "iface", "name": "USB Interface", "channels": {0: "room"}},
        ],
        "midi_ports": ["KeyLab mk3"],
        "clock": {
            "aggregate_device": "Rehearsal Agg",
            "members": ["Helix Stadium", "USB Interface"],
            "clock_source": "Helix Stadium",
            "drift_correction": ["USB Interface"],
        },
        "consent": "given",
    }
    data.update(overrides)
    return SessionConfig.model_validate(data)


def _clicks(
    rate: int, period_s: float, *, offset_s: float = 0.0, drift_ppm: float = 0.0, channels: int = 1
) -> Callable[[int, int], np.ndarray]:
    """A click every ``period_s`` seconds (first at 1 s), optionally late by an offset and drifting."""
    click = np.zeros(int(0.005 * rate))
    click[: len(click)] = 0.8 * np.sin(2 * np.pi * 1000.0 * np.arange(len(click)) / rate)

    def make(first: int, frames: int) -> np.ndarray:
        out = np.zeros((frames, channels))
        t0 = first / rate
        # Clicks that start in this block, in the stream's own (drifted, offset) time.
        k_start = int(np.floor((t0 - 1.0 - offset_s) / period_s)) - 1
        for k in range(max(k_start, 0), k_start + 4):
            nominal = 1.0 + k * period_s
            at = (nominal + offset_s) * (1.0 + drift_ppm * 1e-6)
            s = round(at * rate) - first
            if -len(click) < s < frames:
                a, b = max(s, 0), min(s + len(click), frames)
                out[a:b, :] += click[a - s : b - s, None]
        return out

    return make


def _fake_midi_opener(messages: list[str]) -> Callable[[str, Callable[[Any], None]], Any]:
    class _Port:
        def __init__(self, cb: Callable[[Any], None]) -> None:
            for m in messages:
                cb(m)

        def close(self) -> None:
            pass

    return lambda name, cb: _Port(cb)


def _run(
    config: SessionConfig,
    folder: Path,
    sources: list[FakeSource],
    midi: MidiInputLogger | None = None,
) -> capture.CaptureResult:
    session = Session(config, folder, sources, midi=midi)
    session.start()
    session.mark_burst("start")
    for s in sources:
        s.stop()
    session.mark_burst("end")
    return session.stop()


def test_callback_only_timestamps_and_enqueues() -> None:
    src = inspect.getsource(Callback.__call__)
    for forbidden in (
        "write",
        "open(",
        "json",
        "sf.",
        "Lock",
        "acquire",
        "sleep",
        "print",
        "socket",
    ):
        assert forbidden not in src, forbidden
    queue: deque[capture.Block] = deque()
    cb = Callback("dev", queue)
    buffer = np.ones((BLOCK, 2), dtype=np.float32)

    class T:
        inputBufferAdcTime = 1.5
        currentTime = 1.502

    class S:
        input_overflow = True
        input_underflow = False

    cb(buffer, BLOCK, T(), S())
    buffer[:] = 0  # the driver reuses its buffer; the block must be a copy
    block = queue.popleft()
    assert block.samples.sum() == 2 * BLOCK and block.overflow and block.adc_time == 1.5
    assert block.sample_index == 0 and block.index == 0 and block.frames == BLOCK


def test_clean_run_drops_no_buffers(tmp_path: Path) -> None:
    blocks = 200
    sources = [
        FakeSource("helix", 2, RATE, BLOCK, blocks, signal=_clicks(RATE, 2.0, channels=2)),
        FakeSource("iface", 1, RATE, BLOCK, blocks, signal=_clicks(RATE, 2.0)),
    ]
    midi = MidiInputLogger(
        ["KeyLab mk3"],
        tmp_path / "take" / "midi.jsonl",
        opener=_fake_midi_opener(["note_on channel=0 note=60 velocity=100 time=0"]),
    )
    result = _run(_config(), tmp_path / "take", sources, midi)
    assert result.blocks_written == {
        "guitar": blocks * BLOCK,
        "vocal": blocks * BLOCK,
        "room": blocks * BLOCK,
    }
    assert result.overflow_blocks == 0 and result.midi_messages == 1
    for stream in ("guitar", "vocal", "room"):
        data, rate = sf.read(tmp_path / "take" / f"{stream}.flac")
        assert rate == RATE and len(data) == blocks * BLOCK
    meta = capture.read_blocks(tmp_path / "take")
    assert len(meta) == 2 * blocks
    assert {m["device"] for m in meta} == {"helix", "iface"}
    assert all(
        {
            "device",
            "block",
            "sample_index",
            "frames",
            "adc_time",
            "callback_time",
            "overflow",
            "underflow",
            "received_ns",
        }
        <= set(m)
        for m in meta
    )
    helix = sorted((m for m in meta if m["device"] == "helix"), key=lambda m: m["block"])
    assert [m["sample_index"] for m in helix] == [k * BLOCK for k in range(blocks)]
    # The writer only ever ran on the worker, never on a driver thread.
    info = json.loads((tmp_path / "take" / "capture.json").read_text(encoding="utf-8"))
    assert (
        info["clock"]["aggregate_device"] == "Rehearsal Agg"
        and info["clock"]["clock_source"] == "Helix Stadium"
    )
    assert [b["which"] for b in info["bursts"]] == ["start", "end"]
    assert info["streams"] == "as-captured" and info["midi_ports"] == ["KeyLab mk3"]
    manifest = TakeManifest.model_validate_json(
        (tmp_path / "take" / "manifest.json").read_text(encoding="utf-8")
    )
    assert (
        manifest.take_type == "bench"
        and manifest.consent == "given"
        and manifest.song == "synthetic_anthem"
    )
    assert set(manifest.channels) == {"guitar", "vocal", "room"} and manifest.devices == [
        "Helix Stadium",
        "USB Interface",
    ]
    assert set(manifest.sha256) == {"guitar.flac", "vocal.flac", "room.flac"}
    log = (tmp_path / "take" / "midi.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(log) == 1 and json.loads(log[0])["port"] == "KeyLab mk3"
    report = align.validate(
        tmp_path / "take",
        reference="room",
        device_of={"guitar": "helix", "vocal": "helix", "room": "iface"},
    )
    assert report.passed and all(not s.invalid for s in report.streams)
    assert all(s.markers >= 2 for s in report.streams)


def test_writer_never_runs_on_the_callback_thread(tmp_path: Path) -> None:
    sources = [
        FakeSource("helix", 2, RATE, BLOCK, 50, pace_s=0.001),
        FakeSource("iface", 1, RATE, BLOCK, 50),
    ]
    session = Session(_config(), tmp_path / "t", sources)
    session.start()
    driver_threads = {s._thread.ident for s in sources if s._thread is not None}  # pyright: ignore[reportPrivateUsage]
    for s in sources:
        s.stop()
    session.stop()
    writer = session._writer  # pyright: ignore[reportPrivateUsage]
    assert writer is not None
    assert writer.thread_ids and not (writer.thread_ids & driver_threads)
    assert threading.get_ident() in writer.thread_ids or len(writer.thread_ids) == 1


def test_injected_discontinuity_is_detected_and_marked_invalid(tmp_path: Path) -> None:
    blocks = 400  # 12.8 s at 8 kHz
    dropped = frozenset({150})
    sources = [
        FakeSource("helix", 2, RATE, BLOCK, blocks, signal=_clicks(RATE, 1.0, channels=2)),
        FakeSource("iface", 1, RATE, BLOCK, blocks, signal=_clicks(RATE, 1.0), drop_blocks=dropped),
    ]
    result = _run(_config(), tmp_path / "take", sources)
    assert (
        result.blocks_written["room"] == (blocks - 1) * BLOCK
    )  # the dropped block is gone, not zero-filled
    assert result.blocks_written["guitar"] == blocks * BLOCK
    report = align.validate(
        tmp_path / "take",
        reference="guitar",
        device_of={"guitar": "helix", "vocal": "helix", "room": "iface"},
    )
    room = next(s for s in report.streams if s.stream == "room")
    gap_start = 150 * BLOCK / RATE
    assert room.invalid, report
    assert any(i.start_s <= gap_start <= i.end_s and "dropped" in i.reason for i in room.invalid)
    assert report.invalid_total_s > 0
    vocal = next(s for s in report.streams if s.stream == "vocal")
    assert not vocal.invalid
    # After the gap the room stream is early by one block; markers after it are flagged, the
    # marker fit still covers the good part, and the report says what it measured.
    assert room.max_raw_ms >= BLOCK / RATE * 1000 - 1
    saved = json.loads((tmp_path / "take" / "alignment.json").read_text(encoding="utf-8"))
    assert saved["streams"][0]["stream"] == "guitar"


def test_ten_minute_marker_recording_aligns_within_1ms_after_correction(tmp_path: Path) -> None:
    seconds = 600
    blocks = seconds * RATE // BLOCK
    period = 10.0
    sources = [
        FakeSource("helix", 1, RATE, BLOCK, blocks, signal=_clicks(RATE, period)),
        FakeSource(
            "iface",
            1,
            RATE,
            BLOCK,
            blocks,
            signal=_clicks(RATE, period, offset_s=0.0007, drift_ppm=50.0),
            drift_ppm=50.0,
        ),
    ]
    config = _config(
        devices=[
            {"id": "helix", "name": "Helix Stadium", "channels": {0: "guitar"}},
            {"id": "iface", "name": "USB Interface", "channels": {0: "room"}},
        ]
    )
    _run(config, tmp_path / "take", sources)
    report = align.validate(
        tmp_path / "take", reference="guitar", device_of={"guitar": "helix", "room": "iface"}
    )
    room = next(s for s in report.streams if s.stream == "room")
    assert room.markers >= 59
    assert room.max_raw_ms > 25  # 50 ppm over 10 minutes is 30 ms: uncorrected, it fails
    assert abs(room.drift_ppm - 50.0) < 5 and abs(room.offset_ms - 0.7) < 0.3
    assert room.max_residual_ms <= 1.0 and report.passed, report
    out = align.correct(tmp_path / "take", report)
    assert (out / "IDEALIZED.md").is_file()
    corrected = TakeManifest.model_validate_json(
        (out / "manifest.json").read_text(encoding="utf-8")
    )
    assert corrected.render and corrected.render["idealized"] is True
    assert corrected.take.endswith("/corrected")
    # Re-validate the corrected copy: the markers now coincide within tolerance, raw.
    again = align.validate(out, reference="guitar")
    room2 = next(s for s in again.streams if s.stream == "room")
    assert room2.max_raw_ms <= 1.0 and again.passed
    # The as-captured files were not touched.
    original = TakeManifest.model_validate_json(
        (tmp_path / "take" / "manifest.json").read_text(encoding="utf-8")
    )
    import hashlib

    assert (
        original.sha256["room.flac"]
        == hashlib.sha256((tmp_path / "take" / "room.flac").read_bytes()).hexdigest()
    )


def test_misaligned_recording_reports_the_measured_error(tmp_path: Path) -> None:
    """A stream that jumps mid-take cannot be fixed by a linear model: the report says so."""
    blocks = 300

    def jumpy(first: int, frames: int) -> np.ndarray:
        base = _clicks(RATE, 1.0)
        late = _clicks(RATE, 1.0, offset_s=0.004)
        return late(first, frames) if first / RATE > 5.0 else base(first, frames)

    sources = [
        FakeSource("helix", 1, RATE, BLOCK, blocks, signal=_clicks(RATE, 1.0)),
        FakeSource("iface", 1, RATE, BLOCK, blocks, signal=jumpy),
    ]
    config = _config(
        devices=[
            {"id": "helix", "name": "A", "channels": {0: "guitar"}},
            {"id": "iface", "name": "B", "channels": {0: "room"}},
        ]
    )
    _run(config, tmp_path / "take", sources)
    report = align.validate(tmp_path / "take", reference="guitar")
    room = next(s for s in report.streams if s.stream == "room")
    assert room.invalid and any("jumped" in i.reason for i in room.invalid)
    assert room.max_raw_ms >= 3.0


def test_session_refuses_a_rehearsal_without_consent(tmp_path: Path) -> None:
    with pytest.raises(capture.CaptureError, match="consent"):
        Session(_config(take_type="rehearsal", consent="none"), tmp_path / "t", [])
    Session(_config(take_type="bench", consent="none"), tmp_path / "t", [])  # a bench take is fine


def test_session_file_loads_and_rejects_bad_streams(tmp_path: Path) -> None:
    path = tmp_path / "session.yaml"
    path.write_text(
        yaml.safe_dump(_config().model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    assert load_session(path).id == "take_1"
    bad = _config().model_dump(mode="json")
    bad["devices"][0]["channels"] = {"0": "nope"}
    path.write_text(yaml.safe_dump(bad, sort_keys=False), encoding="utf-8")
    with pytest.raises(capture.CaptureError, match=re.escape("nope") + "|channels"):
        load_session(path)
    assert "session" in __import__("roobotto.config.schemas", fromlist=["SCHEMAS"]).SCHEMAS


def test_cli_record_validate_and_correct(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sources = [
        FakeSource("helix", 1, RATE, BLOCK, 120, signal=_clicks(RATE, 1.0)),
        FakeSource("iface", 1, RATE, BLOCK, 120, signal=_clicks(RATE, 1.0, offset_s=0.0005)),
    ]
    config = _config(
        devices=[
            {"id": "helix", "name": "A", "channels": {0: "guitar"}},
            {"id": "iface", "name": "B", "channels": {0: "room"}},
        ]
    )
    _run(config, tmp_path / "take", sources)
    assert main(["record", "validate", str(tmp_path / "take"), "--reference", "guitar"]) == 0
    out = capsys.readouterr().out
    assert "room" in out and "drift" in out and "PASS" in out
    assert main(["record", "correct", str(tmp_path / "take")]) == 0
    assert "idealized" in capsys.readouterr().out.lower()
    assert (tmp_path / "take" / "corrected" / "room.flac").is_file()
    assert main(["record", "validate", str(tmp_path / "nope")]) == 1
    # Capture needs a session file and real devices; without sounddevice it refuses cleanly.
    session = tmp_path / "s.yaml"
    session.write_text(
        yaml.safe_dump(config.model_dump(mode="json"), sort_keys=False), encoding="utf-8"
    )
    code = main(
        ["record", "session", str(session), "--out", str(tmp_path / "live"), "--seconds", "1"]
    )
    assert code in (0, 1)
    assert not (tmp_path / "live" / "manifest.json").is_file() or code == 0
