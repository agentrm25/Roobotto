"""``roobotto benchmark <protocol.yaml>`` (T-13): run every method over the corpus and write the
Section 9 metrics as CSV plus a Markdown report with plots.

A protocol names the corpus (take folders, optionally rendered from song files when missing),
the reference take per song, the methods, the feed subsets per method and the fault injections.
The report carries the method x feed-subset matrix, the fault-injection results, assistance
accounting, failure examples, a reduced-scope section, the streams-and-idealized rule, and the
commit, config and manifest hashes. Synthetic results are labeled as such on every report: they
never count as evidence that following is feasible. Plots are SVG written by hand, so no
plotting library is needed. Nothing here fires anything.
"""

import csv
import hashlib
import json
import subprocess
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import Field, ValidationError

from roobotto.config.common import Id, Model, PosInt, Text
from roobotto.config.loader import issues_from_validation_error, load_config
from roobotto.config.song import Song
from roobotto.eval import synth
from roobotto.eval.score import Score
from roobotto.tracker import baselines, follower

NS = 1_000_000_000
FOLLOWER_METHOD = "B3"
METHODS = (*baselines.METHODS, FOLLOWER_METHOD)
METHOD_NAMES = {
    "B0": "Fixed timeline from the annotated song start, at nominal tempo",
    "B1": "Beat tracker (onset PLL) plus pure bar counting, no content observation",
    "B3": "Bar-grid follower v0 on the listed feeds",
}
NOT_BUILT = {
    "B2": "Linear online DTW against the reference take (P1; the pymatchmaker cross-check with it)",
    "B4": "Bar-grid follower on isolated feeds, audio only (the synthetic corpus has no room mic, "
    "so B3 here already runs on isolated feeds)",
    "B5": "B4 plus existing MIDI and controller events",
    "B6": "B5 plus simulated performer presses at 50% of boundaries",
}


class BenchmarkError(ValueError):
    pass


# --- Protocol ---


class Synthesize(Model):
    songs: Text
    """Folder of song YAML files to render when a take is missing."""
    sample_rate: PosInt = synth.DEFAULT_SAMPLE_RATE


class Corpus(Model):
    root: Text
    """Folder holding ``<song>/<take>/`` take folders. Never committed."""
    synthesize: Synthesize | None = None
    songs: Literal["all"] | list[Id] = "all"
    takes: Literal["all"] | list[Text] = "all"


class FaultInjection(Model):
    id: Id
    drop: dict[Text, list[tuple[float, float]]]
    """Feed -> intervals (seconds) whose blocks are withheld."""


class Protocol(Model):
    kind: Literal["protocol"]
    schema_version: Literal[0]
    id: Id
    title: Text
    label: Literal["synthetic", "pilot", "study"]
    corpus: Corpus
    reference: Text = "nominal"
    """The take (variation name) of each song that serves as its reference."""
    block: PosInt = 512
    methods: list[Literal["B0", "B1", "B3"]] = Field(default_factory=lambda: ["B0", "B1", "B3"])
    feed_subsets: dict[Literal["B3"], list[list[Text]]] = Field(default_factory=lambda: {})
    fault_injections: list[FaultInjection] = Field(default_factory=lambda: [])
    follower: dict[str, Any] = Field(default_factory=lambda: {})
    """Overrides for ``FollowerConfig`` (temperature and so on)."""
    out: Text


def load_protocol(path: Path) -> Protocol:
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise BenchmarkError(f"cannot read {path}: {exc.strerror or exc}") from exc
    except yaml.YAMLError as exc:
        raise BenchmarkError(f"{path}: invalid YAML: {exc}") from exc
    try:
        return Protocol.model_validate(raw)
    except ValidationError as exc:
        issues = issues_from_validation_error(str(path), exc)
        raise BenchmarkError("\n".join(str(i) for i in issues)) from exc


# --- Running ---


@dataclass(frozen=True)
class Row:
    method: str
    feeds: str
    take: str
    song: str
    variation: str
    fault: str
    score: Score
    manifest_sha256: str
    runtime_s: float

    def as_csv(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "method": self.method,
            "feeds": self.feeds,
            "take": self.take,
            "song": self.song,
            "variation": self.variation,
            "fault": self.fault,
            "streams": "as-captured",
            "idealized": "false",
        }
        for key, value in self.score.as_dict().items():
            if key != "method":
                out[key] = "" if value is None else value
        out["manifest_sha256"] = self.manifest_sha256
        return out


@dataclass(frozen=True)
class BenchmarkResult:
    protocol: Protocol
    out_dir: Path
    rows: list[Row]
    summary: dict[str, Any]


def _git(repo: Path, *args: str) -> str:
    try:
        return subprocess.run(
            ["git", *args], cwd=repo, check=True, capture_output=True, text=True
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return ""


def _take_folders(protocol: Protocol, repo: Path) -> list[Path]:
    root = Path(protocol.corpus.root)
    if not root.is_absolute():
        root = repo / root
    synthesize = protocol.corpus.synthesize
    if synthesize is not None:
        songs_dir = Path(synthesize.songs)
        if not songs_dir.is_absolute():
            songs_dir = repo / songs_dir
        for song_file in sorted(songs_dir.glob("*.yaml")):
            model, _ = load_config(song_file)
            if not isinstance(model, Song):
                continue
            if protocol.corpus.songs != "all" and model.id not in protocol.corpus.songs:
                continue
            wanted = synth.VARIATIONS if protocol.corpus.takes == "all" else protocol.corpus.takes
            missing = [v for v in wanted if not (root / model.id / v / "manifest.json").is_file()]
            if missing:
                synth.render_song_file(
                    song_file, root, list(missing), sample_rate=synthesize.sample_rate
                )
    folders: list[Path] = []
    for manifest in sorted(root.glob("*/*/manifest.json")):
        folder = manifest.parent
        song, take = folder.parent.name, folder.name
        if protocol.corpus.songs != "all" and song not in protocol.corpus.songs:
            continue
        if protocol.corpus.takes != "all" and take not in protocol.corpus.takes:
            continue
        folders.append(folder)
    if not folders:
        raise BenchmarkError(f"no takes found under {root}")
    return folders


def _manifest_hash(folder: Path) -> str:
    return hashlib.sha256((folder / "manifest.json").read_bytes()).hexdigest()


def run_benchmark(
    protocol: Protocol, *, repo: Path, out_dir: Path | None = None
) -> BenchmarkResult:
    out = out_dir or (
        repo / protocol.out if not Path(protocol.out).is_absolute() else Path(protocol.out)
    )
    out.mkdir(parents=True, exist_ok=True)
    folders = _take_folders(protocol, repo)
    by_song: dict[str, list[Path]] = {}
    for f in folders:
        by_song.setdefault(f.parent.name, []).append(f)
    references: dict[tuple[str, tuple[str, ...]], follower.Reference] = {}
    rows: list[Row] = []
    faults: list[tuple[str, Mapping[str, Sequence[tuple[float, float]]] | None]] = [("", None)]
    for fi in protocol.fault_injections:
        drop_map: dict[str, Sequence[tuple[float, float]]] = {
            k: [(float(a), float(b)) for a, b in v] for k, v in fi.drop.items()
        }
        faults.append((fi.id, drop_map))
    for folder in folders:
        song_id, variation = folder.parent.name, folder.name
        take_name = f"{song_id}/{variation}"
        manifest_hash = _manifest_hash(folder)
        reference_folder = folder.parent / protocol.reference
        for method in protocol.methods:
            subsets: list[tuple[str, ...]] = [("drums",)]
            if method == FOLLOWER_METHOD:
                subsets = [
                    tuple(s)
                    for s in protocol.feed_subsets.get("B3", [list(follower.DEFAULT_FEEDS)])
                ]
            for feeds in subsets:
                for fault_id, drop in faults:
                    started = time.perf_counter()
                    if method == FOLLOWER_METHOD:
                        if not (reference_folder / "manifest.json").is_file():
                            raise BenchmarkError(
                                f"{take_name}: reference take '{protocol.reference}' is missing"
                            )
                        key = (song_id, feeds)
                        if key not in references:
                            references[key] = follower.build_reference(
                                [reference_folder], feeds=feeds, block=protocol.block
                            )
                        cfg = follower.FollowerConfig(feeds=feeds, **protocol.follower)
                        score = follower.run_follower(
                            folder, references[key], config=cfg, block=protocol.block, drop=drop
                        ).score
                    else:
                        score = baselines.run_baseline(
                            method, folder, block=protocol.block, drop=drop
                        ).score
                    rows.append(
                        Row(
                            method,
                            "+".join(feeds),
                            take_name,
                            song_id,
                            variation,
                            fault_id,
                            score,
                            manifest_hash,
                            time.perf_counter() - started,
                        )
                    )
    summary = _summary(protocol, repo, folders, rows)
    _write_csv(out / "results.csv", rows)
    _write_timing(out / "timing.csv", rows)
    _write_plots(out, rows)
    (out / "report.md").write_text(_report(protocol, rows, summary), encoding="utf-8")
    (out / "summary.json").write_text(
        json.dumps(summary, indent=1, sort_keys=True) + "\n", encoding="utf-8"
    )
    return BenchmarkResult(protocol, out, rows, summary)


# --- Summary, CSV, plots, report ---


def _config_hash(protocol: Protocol, folders: Sequence[Path]) -> str:
    digest = hashlib.sha256(protocol.model_dump_json().encode())
    for folder in folders:
        song_file = folder / "song.yaml"
        if song_file.is_file():
            digest.update(song_file.read_bytes())
    return digest.hexdigest()


def _summary(
    protocol: Protocol, repo: Path, folders: Sequence[Path], rows: Sequence[Row]
) -> dict[str, Any]:
    manifests = {f"{f.parent.name}/{f.name}": _manifest_hash(f) for f in folders}
    take_types: set[str] = set()
    for f in folders:
        data = json.loads((f / "manifest.json").read_text(encoding="utf-8"))
        take_types.add(str(data.get("take_type")))
    reduced = [f"{m}: deferred, not built. {why}" for m, why in NOT_BUILT.items()]
    reduced.append(
        "Features: drum-band pattern and vocal activity (T-10 P1) are deferred; onset and chroma "
        "only."
    )
    reduced.append(
        "Ablations: with and without the duration prior, and with and without the transition "
        "graph, are deferred."
    )
    if "synthetic" in take_types:
        reduced.append("Takes: synthetic only; no pilot take has been recorded (WP5).")
    if protocol.label != "synthetic" and "synthetic" in take_types:
        reduced.append(
            f"Label '{protocol.label}' requested but the corpus contains synthetic takes; the "
            "report is marked SYNTHETIC."
        )
    return {
        "protocol": protocol.id,
        "title": protocol.title,
        "label": "synthetic" if "synthetic" in take_types else protocol.label,
        "commit": _git(repo, "rev-parse", "HEAD") or "unknown",
        "dirty": bool(_git(repo, "status", "--porcelain")),
        "config_hash": _config_hash(protocol, folders),
        "manifests": manifests,
        "take_types": sorted(take_types),
        "methods": list(protocol.methods),
        "rows": len(rows),
        "reduced_scope": reduced,
    }


def _write_csv(path: Path, rows: Sequence[Row]) -> None:
    if not rows:
        return
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(rows[0].as_csv()))
        writer.writeheader()
        for row in rows:
            writer.writerow(row.as_csv())


def _write_timing(path: Path, rows: Sequence[Row]) -> None:
    """Wall-clock per run, kept apart from the results: a measurement of this machine, not part
    of the reproducibility claim."""
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["method", "feeds", "take", "fault", "runtime_s"])
        for r in rows:
            writer.writerow([r.method, r.feeds, r.take, r.fault, round(r.runtime_s, 3)])


def _recovered(group: Sequence[Row]) -> int:
    return sum(1 for r in group if r.score.recovery_bars is not None and r.score.recovery_bars <= 2)


def _mean(values: Sequence[float]) -> float:
    return sum(values) / len(values) if values else 0.0


def _groups(rows: Sequence[Row], fault: str = "") -> dict[tuple[str, str], list[Row]]:
    out: dict[tuple[str, str], list[Row]] = {}
    for r in rows:
        if r.fault == fault:
            out.setdefault((r.method, r.feeds), []).append(r)
    return out


def _svg_bars(
    path: Path, title: str, labels: Sequence[str], values: Sequence[float], unit: str = ""
) -> None:
    width, height, left, bottom = 720, 320, 60, 60
    plot_w, plot_h = width - left - 20, height - bottom - 40
    top = max([*values, 1e-9])
    bar_w = plot_w / max(len(values), 1)
    parts = [
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
        'font-family="sans-serif" font-size="12">',
        f'<rect width="{width}" height="{height}" fill="white"/>',
        f'<text x="{left}" y="24" font-size="16">{title}</text>',
        f'<line x1="{left}" y1="{40 + plot_h}" x2="{left + plot_w}" y2="{40 + plot_h}" '
        'stroke="black"/>',
        f'<line x1="{left}" y1="40" x2="{left}" y2="{40 + plot_h}" stroke="black"/>',
        f'<text x="{left - 8}" y="44" text-anchor="end">{top:.3g}{unit}</text>',
        f'<text x="{left - 8}" y="{40 + plot_h}" text-anchor="end">0</text>',
    ]
    for i, (label, value) in enumerate(zip(labels, values, strict=True)):
        h = plot_h * value / top
        x = left + i * bar_w + bar_w * 0.15
        y = 40 + plot_h - h
        parts.append(
            f'<rect x="{x:.1f}" y="{y:.1f}" width="{bar_w * 0.7:.1f}" height="{h:.1f}" '
            'fill="#2a4d8f"/>'
        )
        parts.append(
            f'<text x="{x + bar_w * 0.35:.1f}" y="{y - 4:.1f}" text-anchor="middle">'
            f"{value:.3g}</text>"
        )
        parts.append(
            f'<text x="{x + bar_w * 0.35:.1f}" y="{40 + plot_h + 16}" text-anchor="middle">'
            f"{label}</text>"
        )
    parts.append("</svg>\n")
    path.write_text("\n".join(parts), encoding="utf-8")


def _write_plots(out: Path, rows: Sequence[Row]) -> None:
    groups = _groups(rows)
    labels = [f"{m} {f}" if m == FOLLOWER_METHOD else m for m, f in groups]
    _svg_bars(
        out / "accuracy_by_method.svg",
        "Section-and-visit accuracy per beat, clean takes, as-captured streams",
        labels,
        [_mean([r.score.section_visit_accuracy for r in g]) for g in groups.values()],
    )
    _svg_bars(
        out / "boundary_timing.svg",
        "Boundary timing error, p95 of |first entry - annotated start|, clean takes",
        labels,
        [_mean([r.score.boundary_error_p95_s for r in g]) for g in groups.values()],
        " s",
    )
    _svg_bars(
        out / "coverage_vs_wrong.svg",
        "Production-equivalent accuracy (coverage of the right cues) per beat, clean takes",
        labels,
        [_mean([r.score.production_equivalent_accuracy for r in g]) for g in groups.values()],
    )


def _table(headers: Sequence[str], body: Sequence[Sequence[Any]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "---|" * len(headers)]
    for row in body:
        lines.append("| " + " | ".join("" if v is None else str(v) for v in row) + " |")
    return "\n".join(lines)


def _fmt(value: float | None, digits: int = 3) -> str:
    return "" if value is None else f"{value:.{digits}f}"


def _report(protocol: Protocol, rows: Sequence[Row], summary: dict[str, Any]) -> str:
    label = str(summary["label"]).upper()
    banner = (
        "> **SYNTHETIC CORPUS.** These results come from rendered takes and are **not evidence** "
        "that following is feasible on real performances (plan Section 9). They test the code."
        if label == "SYNTHETIC"
        else f"> Label: **{label}**. Gates are proposed targets, not current capability."
    )
    parts = [f"# {protocol.title} ({label})", "", banner, ""]
    parts += [
        "## Provenance",
        "",
        _table(
            ["Item", "Value"],
            [
                ["Protocol", protocol.id],
                [
                    "Commit",
                    summary["commit"] + (" (working tree dirty)" if summary["dirty"] else ""),
                ],
                ["Config hash (protocol + song files)", summary["config_hash"]],
                ["Methods", ", ".join(summary["methods"])],
                ["Block size", f"{protocol.block} samples"],
                ["Takes", len(summary["manifests"])],
            ],
        ),
        "",
        "Manifest hashes (SHA-256 of each take's `manifest.json`, which itself hashes the audio):",
        "",
        _table(
            ["Take", "manifest.json SHA-256"],
            [[k, v] for k, v in sorted(summary["manifests"].items())],
        ),
        "",
    ]
    groups = _groups(rows)
    parts += [
        "## Results by method and feed subset",
        "",
        "Clean takes, as-captured streams, per annotated beat (the estimate in force at mid-beat). "
        "Means over takes; the CSV has every take.",
        "",
        _table(
            [
                "Method",
                "Feeds",
                "Takes",
                "Section+visit",
                "Bar",
                "Prod.-equiv.",
                "Tempo err",
                "Boundary median (s)",
                "Boundary p95 (s)",
                "Recovered within 2 bars",
            ],
            [
                [
                    f"{m} ({METHOD_NAMES.get(m, '')})",
                    f,
                    len(g),
                    _fmt(_mean([r.score.section_visit_accuracy for r in g])),
                    _fmt(_mean([r.score.bar_accuracy for r in g])),
                    _fmt(_mean([r.score.production_equivalent_accuracy for r in g])),
                    _fmt(_mean([r.score.tempo_error_ratio for r in g]), 4),
                    _fmt(_mean([r.score.boundary_error_median_s for r in g])),
                    _fmt(_mean([r.score.boundary_error_p95_s for r in g])),
                    f"{_recovered(g)}"
                    f"/{sum(1 for r in g if r.score.accuracy_after_deviation is not None)}",
                ]
                for (m, f), g in groups.items()
            ],
        ),
        "",
        "![Accuracy by method](accuracy_by_method.svg)",
        "",
        "![Boundary timing](boundary_timing.svg)",
        "",
        "![Coverage](coverage_vs_wrong.svg)",
        "",
        "### Per take",
        "",
        _table(
            [
                "Method",
                "Feeds",
                "Take",
                "Section+visit",
                "Bar",
                "Before dev.",
                "After dev.",
                "Recovery (bars)",
                "Unexpected boundaries",
            ],
            [
                [
                    r.method,
                    r.feeds,
                    r.take,
                    _fmt(r.score.section_visit_accuracy),
                    _fmt(r.score.bar_accuracy),
                    _fmt(r.score.accuracy_before_deviation),
                    _fmt(r.score.accuracy_after_deviation),
                    "" if r.score.recovery_bars is None else r.score.recovery_bars,
                    r.score.unexpected_boundaries,
                ]
                for r in rows
                if r.fault == ""
            ],
        ),
        "",
    ]
    fault_rows = [r for r in rows if r.fault]
    parts += ["## Fault injections", ""]
    if fault_rows:
        parts += [
            "Blocks of the named feed are withheld for the interval (the feed reads `lost`, not "
            "silent). "
            "Compared with the same method, feeds and take without the fault.",
            "",
            _table(
                ["Fault", "Method", "Feeds", "Take", "Section+visit", "Clean", "Delta"],
                [
                    [
                        r.fault,
                        r.method,
                        r.feeds,
                        r.take,
                        _fmt(r.score.section_visit_accuracy),
                        _fmt(_clean_score(rows, r)),
                        _fmt(r.score.section_visit_accuracy - (_clean_score(rows, r) or 0.0), 3),
                    ]
                    for r in fault_rows
                ],
            ),
            "",
        ]
    else:
        parts += ["None in this protocol.", ""]
    boundaries = _boundary_counts(rows)
    parts += [
        "## Assistance accounting",
        "",
        "What each method was told, beyond the audio (audit F-12). Every method here is anchored "
        "at the **annotated song start**: it is told about exactly one boundary, the first, at "
        "time "
        "zero relative to it, by annotation. No method receives existing MIDI (B5) or simulated "
        "presses (B6); those methods are not built. Conclusions therefore read as audio-only "
        "results with the song start given.",
        "",
        _table(
            ["Method", "Boundaries in corpus", "Told about", "By which input", "When"],
            [
                [m, boundaries, "1 per take (song start)", "annotation", "at the boundary (0 s)"]
                for m in protocol.methods
            ],
        ),
        "",
    ]
    parts += ["## Failure examples", ""]
    worst = sorted(
        (r for r in rows if r.fault == ""), key=lambda r: r.score.section_visit_accuracy
    )[:6]
    parts += [
        _table(
            ["Method", "Feeds", "Take", "Section+visit", "What happened"],
            [
                [r.method, r.feeds, r.take, _fmt(r.score.section_visit_accuracy), _explain(r)]
                for r in worst
            ],
        ),
        "",
    ]
    parts += ["## Reduced scope", ""]
    parts += [f"- {item}" for item in summary["reduced_scope"]]
    parts += [
        "",
        "## Streams and the idealized label",
        "",
        "Every result above was computed on the takes' **as-captured** streams: each block became "
        "available when its last sample was captured, and nothing read past that moment (I-9). No "
        "result in this report is **idealized** (computed on drift-corrected streams); the "
        "benchmark has no corrected-stream path, so the `idealized` column in `results.csv` is "
        "`false` throughout. Wall-clock per run is in `timing.csv`, measured on the machine that "
        "produced this report, and is not part of the reproducibility claim.",
        "",
    ]
    return "\n".join(parts)


def _clean_score(rows: Sequence[Row], faulty: Row) -> float | None:
    for r in rows:
        if r.fault == "" and (r.method, r.feeds, r.take) == (
            faulty.method,
            faulty.feeds,
            faulty.take,
        ):
            return r.score.section_visit_accuracy
    return None


def _boundary_counts(rows: Sequence[Row]) -> int:
    seen: dict[str, int] = {}
    for r in rows:
        seen[r.take] = r.score.boundaries_scored + 1
    return sum(seen.values())


def _explain(r: Row) -> str:
    s = r.score
    if s.accuracy_after_deviation is not None and s.recovery_bars is None:
        return (
            f"never recovered after the {r.variation} (before "
            f"{_fmt(s.accuracy_before_deviation)}, after {_fmt(s.accuracy_after_deviation)})"
        )
    if s.accuracy_after_deviation is not None:
        return f"recovered {s.recovery_bars} bars after the {r.variation}"
    if s.tempo_error_ratio > 0.03:
        return f"tempo error {s.tempo_error_ratio:.3f}: lost the beat grid ({r.variation})"
    return f"boundaries {_fmt(s.boundary_error_median_s)} s late at the median ({r.variation})"
