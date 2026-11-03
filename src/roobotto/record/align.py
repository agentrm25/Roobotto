"""Alignment validation and correction for a captured take (T-07, plan Section 9).

Two sources of truth about alignment: the per-block metadata (sample indices and ADC times must
advance together; a jump or an overflow flag marks a gap) and a shared marker signal (a click
split to every input at regular intervals; the same click must land at the same time in every
stream). The validator finds the clicks, measures each stream's offset against the reference
stream at every click, fits a linear drift model, reports the residual after that correction,
and marks invalid intervals: metadata gaps, flagged blocks, and marker intervals where the offset
jumps. Correction writes shifted and drift-compensated copies under ``corrected/``; those are
**idealized** and never the basis of a reported follower result.
"""

import hashlib
import itertools
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np
import numpy.typing as npt
import soundfile as sf  # pyright: ignore[reportMissingTypeStubs]

from roobotto.eval.annotations import TakeManifest

Array = npt.NDArray[np.float64]


@dataclass(frozen=True)
class Interval:
    start_s: float
    end_s: float
    reason: str


@dataclass(frozen=True)
class StreamAlignment:
    stream: str
    markers: int
    offset_ms: float
    """Constant offset against the reference at the first marker, after the fit."""
    drift_ppm: float
    max_residual_ms: float
    """Largest |marker offset - linear model| after correction."""
    max_raw_ms: float
    """Largest |marker offset| before correction."""
    invalid: list[Interval] = field(default_factory=lambda: [])


@dataclass(frozen=True)
class AlignmentReport:
    reference: str
    tolerance_ms: float
    streams: list[StreamAlignment]
    passed: bool
    invalid_total_s: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --- Markers ---


def find_clicks(signal: Array, rate: int, *, min_gap_s: float = 0.25, level: float = 0.3) -> Array:
    """Sample indices where a click starts: the first sample of each run above ``level`` times
    the stream's peak, with runs closer than ``min_gap_s`` merged. Returns float sample times."""
    peak = float(np.max(np.abs(signal))) if len(signal) else 0.0
    if peak <= 0.0:
        return np.zeros(0)
    above = np.abs(signal) >= level * peak
    starts = np.flatnonzero(above & ~np.concatenate([[False], above[:-1]]))
    out: list[int] = []
    gap = int(min_gap_s * rate)
    for s in starts:
        if not out or s - out[-1] >= gap:
            out.append(int(s))
    return np.asarray(out, dtype=np.float64)


def _refine(signal: Array, start: int, window: int = 64) -> float:
    """Sub-sample onset: the zero-order crossing of the click's rising edge."""
    seg = np.abs(signal[max(0, start - window) : start + window])
    if len(seg) < 2:
        return float(start)
    threshold = 0.5 * float(np.max(seg))
    idx = int(np.argmax(seg >= threshold))
    base = max(0, start - window)
    if idx == 0:
        return float(base)
    a, b = seg[idx - 1], seg[idx]
    frac = (threshold - a) / (b - a) if b > a else 0.0
    return float(base + idx - 1 + frac)


# --- Metadata gaps ---


def metadata_gaps(
    blocks: Sequence[Mapping[str, Any]], rate: int, *, tolerance_s: float = 0.002
) -> dict[str, list[Interval]]:
    """Per device: intervals where sample indices or ADC times jumped, or blocks were flagged."""
    by_device: dict[str, list[Mapping[str, Any]]] = {}
    for b in blocks:
        by_device.setdefault(str(b["device"]), []).append(b)
    out: dict[str, list[Interval]] = {}
    for device, items in by_device.items():
        items = sorted(items, key=lambda b: int(b["block"]))
        bad: list[Interval] = []
        for prev, cur in itertools.pairwise(items):
            expected = int(prev["sample_index"]) + int(prev["frames"])
            seam = int(cur["sample_index"]) / rate
            if int(cur["sample_index"]) != expected:
                bad.append(Interval(expected / rate, seam, "sample index jumped: dropped block(s)"))
            d_adc = float(cur["adc_time"]) - float(prev["adc_time"])
            d_samples = (int(cur["sample_index"]) - int(prev["sample_index"])) / rate
            if abs(d_adc - d_samples) > tolerance_s:
                # The driver's clock moved further than the samples it delivered: audio is
                # missing at this seam. In file time the seam is a point; the interval covers
                # the block before it and the lost duration after it.
                lost = max(d_adc - d_samples, 0.0)
                bad.append(
                    Interval(
                        int(prev["sample_index"]) / rate,
                        seam + lost,
                        f"dropped block(s): ADC time moved {d_adc:.4f} s for {d_samples:.4f} s "
                        "of samples",
                    )
                )
        for b in items:
            if b.get("overflow") or b.get("underflow"):
                s = int(b["sample_index"]) / rate
                bad.append(
                    Interval(s, s + int(b["frames"]) / rate, "driver flagged overflow or underflow")
                )
        out[device] = _merge(bad)
    return out


def _merge(intervals: Sequence[Interval]) -> list[Interval]:
    items = sorted(intervals, key=lambda i: i.start_s)
    out: list[Interval] = []
    for i in items:
        if out and i.start_s <= out[-1].end_s:
            last = out[-1]
            out[-1] = Interval(
                last.start_s,
                max(last.end_s, i.end_s),
                last.reason if last.reason == i.reason else f"{last.reason}; {i.reason}",
            )
        else:
            out.append(i)
    return out


# --- Validation ---


def _load(folder: Path) -> tuple[TakeManifest, dict[str, Array], int]:
    manifest = TakeManifest.model_validate_json(
        (folder / "manifest.json").read_text(encoding="utf-8")
    )
    streams: dict[str, Array] = {}
    for name, file in manifest.channels.items():
        data, rate = sf.read(folder / file, dtype="float64", always_2d=False)
        if rate != manifest.sample_rate:
            raise ValueError(f"{file}: sample rate {rate} differs from the manifest")
        streams[name] = np.asarray(data, dtype=np.float64)
    return manifest, streams, manifest.sample_rate


def validate(
    folder: Path,
    *,
    reference: str | None = None,
    tolerance_ms: float = 1.0,
    blocks: Sequence[Mapping[str, Any]] | None = None,
    device_of: Mapping[str, str] | None = None,
) -> AlignmentReport:
    """Measure every stream against the reference stream with the shared markers, fit a linear
    drift, and mark invalid intervals. ``device_of`` maps stream -> device for metadata gaps."""
    manifest, streams, rate = _load(folder)
    names = list(streams)
    ref_name = reference or names[0]
    if ref_name not in streams:
        raise ValueError(f"no stream '{ref_name}' in the take")
    ref_clicks = find_clicks(streams[ref_name], rate)
    ref_times = np.asarray([_refine(streams[ref_name], int(c)) for c in ref_clicks]) / rate
    if blocks is None:
        from roobotto.record.capture import read_blocks

        blocks = read_blocks(folder)
    gaps = metadata_gaps(blocks, rate) if blocks else {}
    results: list[StreamAlignment] = []
    passed = True
    invalid_total = 0.0
    for name in names:
        signal = streams[name]
        clicks = find_clicks(signal, rate)
        times = np.asarray([_refine(signal, int(c)) for c in clicks]) / rate
        invalid: list[Interval] = []
        device = (device_of or {}).get(name)
        if device and device in gaps:
            invalid += gaps[device]
        n = min(len(times), len(ref_times))
        if name == ref_name or n < 2:
            results.append(StreamAlignment(name, len(times), 0.0, 0.0, 0.0, 0.0, _merge(invalid)))
            invalid_total += sum(i.end_s - i.start_s for i in invalid)
            continue
        offsets = times[:n] - ref_times[:n]
        # Exclude markers inside invalid intervals from the fit.
        usable = np.ones(n, dtype=bool)
        for iv in invalid:
            usable &= ~((ref_times[:n] >= iv.start_s) & (ref_times[:n] <= iv.end_s))
        x, y = ref_times[:n][usable], offsets[usable]
        if len(x) >= 2:
            slope, intercept = np.polyfit(x, y, 1)
        else:
            slope, intercept = 0.0, float(np.mean(offsets)) if n else 0.0
        residual = offsets - (intercept + slope * ref_times[:n])
        # A marker whose residual breaks tolerance marks the interval since the previous marker.
        for k in range(1, n):
            if abs(residual[k]) * 1000 > tolerance_ms and usable[k]:
                invalid.append(
                    Interval(
                        float(ref_times[k - 1]),
                        float(ref_times[k]),
                        f"marker offset jumped by {residual[k] * 1000:.2f} ms",
                    )
                )
        invalid = _merge(invalid)
        ok = np.ones(n, dtype=bool)
        for iv in invalid:
            ok &= ~((ref_times[:n] >= iv.start_s) & (ref_times[:n] <= iv.end_s))
        max_residual = float(np.max(np.abs(residual[ok]))) * 1000 if ok.any() else float("inf")
        sa = StreamAlignment(
            name,
            int(n),
            round(float(intercept + slope * ref_times[0]) * 1000, 4),
            round(float(slope) * 1e6, 3),
            round(max_residual, 4),
            round(float(np.max(np.abs(offsets))) * 1000, 4),
            invalid,
        )
        results.append(sa)
        invalid_total += sum(i.end_s - i.start_s for i in invalid)
        if max_residual > tolerance_ms:
            passed = False
    report = AlignmentReport(ref_name, tolerance_ms, results, passed, round(invalid_total, 4))
    (folder / "alignment.json").write_text(
        json.dumps(report.as_dict(), indent=1) + "\n", encoding="utf-8"
    )
    _ = manifest
    return report


# --- Correction (idealized) ---


def correct(folder: Path, report: AlignmentReport | None = None) -> Path:
    """Write drift-corrected copies of every stream under ``corrected/`` with their own
    manifest, labeled idealized. The as-captured files are never touched."""
    manifest, streams, rate = _load(folder)
    if report is None:
        data = json.loads((folder / "alignment.json").read_text(encoding="utf-8"))
        report = AlignmentReport(
            data["reference"],
            data["tolerance_ms"],
            [
                StreamAlignment(
                    s["stream"],
                    s["markers"],
                    s["offset_ms"],
                    s["drift_ppm"],
                    s["max_residual_ms"],
                    s["max_raw_ms"],
                    [Interval(**i) for i in s["invalid"]],
                )
                for s in data["streams"]
            ],
            data["passed"],
            data["invalid_total_s"],
        )
    out = folder / "corrected"
    out.mkdir(exist_ok=True)
    by_name = {s.stream: s for s in report.streams}
    channels: dict[str, str] = {}
    hashes: dict[str, str] = {}
    n_ref = len(streams[report.reference])
    for name, signal in streams.items():
        sa = by_name.get(name)
        if sa is None or name == report.reference:
            corrected = (
                signal[:n_ref] if len(signal) >= n_ref else np.pad(signal, (0, n_ref - len(signal)))
            )
        else:
            # Sample k of the corrected stream is the captured stream at time
            # t_k + offset + drift * t_k, read by linear interpolation.
            t = np.arange(n_ref) / rate
            source_t = t + sa.offset_ms / 1000 + sa.drift_ppm * 1e-6 * t
            corrected = np.interp(
                source_t * rate, np.arange(len(signal)), signal, left=0.0, right=0.0
            )
        file = f"{name}.flac"
        sf.write(
            out / file,
            corrected,
            rate,
            subtype="PCM_24" if manifest.bit_depth == 24 else "PCM_16",
            format="FLAC",
        )
        channels[name] = file
        hashes[file] = hashlib.sha256((out / file).read_bytes()).hexdigest()
    corrected_manifest = TakeManifest(
        kind="take_manifest",
        schema_version=0,
        take=manifest.take + "/corrected",
        song=manifest.song,
        take_type=manifest.take_type,
        variation=manifest.variation,
        sample_rate=rate,
        bit_depth=manifest.bit_depth,
        channels=channels,  # pyright: ignore[reportArgumentType]
        sha256=hashes,
        devices=manifest.devices,
        consent=manifest.consent,
        render={
            "idealized": True,
            "corrected_from": manifest.take,
            "model": "linear offset + drift from the shared markers",
        },
    )
    (out / "manifest.json").write_text(
        corrected_manifest.model_dump_json(indent=1) + "\n", encoding="utf-8"
    )
    (out / "IDEALIZED.md").write_text(
        "These streams are drift-corrected copies for annotation and measurement only. A follower\n"
        "result computed on them is **idealized** and never a reported result (plan Section 9).\n",
        encoding="utf-8",
    )
    return out
