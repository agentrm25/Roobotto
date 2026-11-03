"""Command-line entry point: ``validate-config``, ``schemas export``, ``run --dry-run``, ``bench``,
``devices``, ``doctor``, ``synth``, ``replay`` and ``ui``."""

import argparse
import json
import sys
import webbrowser
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from roobotto.config import schemas
from roobotto.config.validate import validate_files
from roobotto.devices import doctor as doctor_mod
from roobotto.devices import probe
from roobotto.engine.dry_run import DryRunError, dry_run
from roobotto.eval import replay as replay_mod
from roobotto.eval import synth
from roobotto.ui import worksheet
from roobotto.ui.server import make_server

DEFAULT_DOC = Path("docs/hardware-verification.md")


def default_rig() -> Path:
    """The private rig when it exists (ADR-008), else the template."""
    private = Path("local/rig.yaml")
    return private if private.exists() else Path("examples/rig.template.yaml")


def default_config_dir() -> Path:
    """The private config folder when it exists, else the examples."""
    return Path("local") if Path("local").is_dir() else Path("examples")


def _validate_config(args: argparse.Namespace) -> int:
    report = validate_files([Path(p) for p in args.paths])
    if not report.configs and report.ok:
        print("no config files found", file=sys.stderr)
        return 1
    for issue in report.issues:
        print(issue, file=sys.stderr)
    if report.issues:
        print(f"{len(report.issues)} problem(s) found", file=sys.stderr)
        return 1
    print(f"OK: {len(report.configs)} file(s) valid")
    return 0


def _schemas_export(args: argparse.Namespace) -> int:
    out = Path(args.out)
    if args.check:
        bad = schemas.stale(out)
        for path in bad:
            print(f"stale or missing: {path}", file=sys.stderr)
        return 1 if bad else 0
    for path in schemas.export(out):
        print(f"wrote {path}")
    return 0


def _run(args: argparse.Namespace) -> int:
    if not args.dry_run:
        print(
            "live runs are not available yet: this build has no MIDI port backend, and real "
            "output needs a bench_verified profile plus explicit arming. Use --dry-run.",
            file=sys.stderr,
        )
        return 2
    try:
        lines = dry_run(
            [Path(p) for p in args.paths],
            song=args.song,
            performance=args.performance,
            trace=Path(args.trace) if args.trace else None,
        )
    except DryRunError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for line in lines:
        print(line)
    return 0


def _bench_list(args: argparse.Namespace) -> int:
    for test in worksheet.TESTS:
        print(f"{test.id} {test.title}")
    return 0


def _bench_show(args: argparse.Namespace) -> int:
    test = worksheet.TESTS_BY_ID.get(args.test_id)
    if test is None:
        print(f"unknown test '{args.test_id}'; see `roobotto bench list`", file=sys.stderr)
        return 1
    print(f"{test.id} {test.title}\n\nMethod: {test.method}")
    print(f"\nPass: {test.passes}\n\nGates: {test.gates}")
    return 0


def _parse_value(spec: str) -> tuple[str, worksheet.WireValue]:
    """``name=kind:data1[:data2]``, as in ``chorus=note_on:60:100``."""
    name, _, rest = spec.partition("=")
    parts = rest.split(":")
    if not name or len(parts) not in (2, 3) or not all(parts):
        raise worksheet.WorksheetError(f"--value '{spec}' must be name=kind:data1[:data2]")
    try:
        data = [int(x) for x in parts[1:]]
    except ValueError as exc:
        raise worksheet.WorksheetError(f"--value '{spec}': data bytes must be integers") from exc
    wire = worksheet.capability_from_dict(
        {
            "device": "x",
            "kind": "command",
            "name": "x",
            "status": "documented",
            "values": {
                name: {
                    "kind": parts[0],
                    "data1": data[0],
                    "data2": data[1] if len(data) == 2 else None,
                }
            },
        }
    ).values
    assert wire is not None
    return name, wire[name]


def _bench_record(args: argparse.Namespace) -> int:
    capability = None
    try:
        if args.record:
            device, _, rest = args.record.partition(".")
            kind, name = (
                ("report", rest.removeprefix("reports."))
                if rest.startswith("reports.")
                else ("command", rest)
            )
            if not args.status:
                raise worksheet.WorksheetError("--record needs --status documented|bench_verified")
            values = dict(_parse_value(v) for v in args.value) or None
            capability = worksheet.CapabilityChange(
                device=device,
                kind=kind,
                name=name,
                status=args.status,
                source=args.source,
                values=values,
                other_recipients=args.recipients or "",
                side_effects=args.side_effects or "",
                path_checked=args.path_checked or "",
            )
        elif args.value or args.status:
            raise worksheet.WorksheetError("--status and --value need --record")
        result = worksheet.Result(
            test_id=args.test_id,
            date=args.date or date.today().isoformat(),
            outcome=args.outcome,
            notes=args.notes,
            bench_log=args.log,
            capability=capability,
        )
        written = worksheet.record(result, Path(args.doc), Path(args.rig))
    except worksheet.WorksheetError as exc:
        print(f"not recorded: {exc}", file=sys.stderr)
        return 1
    for path in written:
        print(f"wrote {path}")
    return 0


def _devices_list(args: argparse.Namespace) -> int:
    report = probe.list_devices(probe.live_system())
    if args.json:
        print(json.dumps(report.as_dict(), indent=1))
    else:
        print("\n".join(probe.render(report)))
    return 0


def _devices_ports_template(args: argparse.Namespace) -> int:
    report = probe.list_devices(probe.live_system())
    try:
        print(probe.ports_template(Path(args.rig), report), end="")
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return 0


def _doctor(args: argparse.Namespace) -> int:
    paths = [Path(p) for p in args.paths] or [default_config_dir()]
    checks = doctor_mod.run_doctor(paths, probe.live_system(), cwd=Path.cwd())
    if args.json:
        print(json.dumps([c.as_dict() for c in checks], indent=1))
    else:
        print("\n".join(doctor_mod.render(checks)))
    return 1 if doctor_mod.failed(checks) else 0


def _synth_list(args: argparse.Namespace) -> int:
    print("variations: " + ", ".join(synth.VARIATIONS))
    print("streams: " + ", ".join(synth.STREAMS))
    return 0


def _synth_render(args: argparse.Namespace) -> int:
    variations = list(args.variation) or list(synth.VARIATIONS)
    unknown = [v for v in variations if v not in synth.VARIATIONS]
    if unknown:
        print(
            f"unknown variation(s): {', '.join(unknown)}; see `roobotto synth list`",
            file=sys.stderr,
        )
        return 2
    out = Path(args.out)
    for song in args.songs:
        try:
            folders = synth.render_song_file(Path(song), out, variations, sample_rate=args.rate)
        except (synth.SynthError, OSError) as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        for folder in folders:
            print(f"wrote {folder}: {synth.dumps(synth.describe(folder))}")
    return 0


def _replay_features(args: argparse.Namespace) -> int:
    feeds = [f for f in (args.feeds or "").split(",") if f] or None
    drop: dict[str, list[tuple[float, float]]] = {}
    for spec in args.drop:
        try:
            feed, start, end = spec.split(":")
            drop.setdefault(feed, []).append((float(start), float(end)))
        except ValueError:
            print(f"--drop '{spec}' must be feed:start_s:end_s", file=sys.stderr)
            return 2
    try:
        result = replay_mod.replay_take(
            Path(args.take), block=args.block, feeds=feeds, beats=args.beats, drop=drop or None
        )
    except (replay_mod.MissingFeedError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.out:
        with Path(args.out).open("w", encoding="utf-8") as fh:
            for record in result.records:
                fh.write(json.dumps(record, sort_keys=True) + "\n")
    feeds_text = ", ".join(result.feeds)
    print(f"take {result.take}: feeds {feeds_text}, block {result.block} @ {result.rate} Hz")
    print(
        f"{len(result.records)} records; health {json.dumps(result.health_summary, sort_keys=True)}"
    )
    print(f"output hash {result.output_hash}")
    return 0


def _ui(args: argparse.Namespace) -> int:
    server = make_server(
        doc=Path(args.doc),
        rig=Path(args.rig),
        port=args.port,
        config_paths=[Path(p) for p in args.paths] or [default_config_dir()],
    )
    print(
        f"Roobotto workbench at {server.url}  (rig: {args.rig}, docs: {args.doc}). Ctrl-C stops it."
    )
    if not args.no_browser:
        webbrowser.open(server.url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


def _add_paths(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--doc", default=str(DEFAULT_DOC), help=f"verification doc (default: {DEFAULT_DOC})"
    )
    parser.add_argument(
        "--rig",
        default=str(default_rig()),
        help="rig file (default: local/rig.yaml if present, else the template)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="roobotto")
    sub = parser.add_subparsers(dest="command", required=True)

    validate = sub.add_parser("validate-config", help="validate song, rig and setlist YAML files")
    validate.add_argument("paths", nargs="+", help="files or directories")
    validate.set_defaults(func=_validate_config)

    schema = sub.add_parser("schemas", help="JSON Schema tools")
    schema_sub = schema.add_subparsers(dest="schemas_command", required=True)
    export = schema_sub.add_parser("export", help="write JSON Schemas generated from the models")
    export.add_argument("--out", default="schemas", help="output directory (default: schemas)")
    export.add_argument(
        "--check", action="store_true", help="fail if files are stale; write nothing"
    )
    export.set_defaults(func=_schemas_export)

    run = sub.add_parser("run", help="run the cue engine (only --dry-run is available)")
    run.add_argument("paths", nargs="+", help="rig, song and setlist files or directories")
    run.add_argument(
        "--dry-run",
        action="store_true",
        help="print the planned outputs; everything goes to the mock adapter",
    )
    run.add_argument("--song", help="song id for the walkthrough (default: first in setlist)")
    run.add_argument("--performance", default="dry_run", help="performance id (default: dry_run)")
    run.add_argument("--trace", help="replay this consumed input trace instead of a walkthrough")
    run.set_defaults(func=_run)

    bench = sub.add_parser(
        "bench", help="the bench worksheet (plan Section 10.2); the CLI twin of `ui`"
    )
    bench_sub = bench.add_subparsers(dest="bench_command", required=True)
    bench_sub.add_parser("list", help="list the bench tests").set_defaults(func=_bench_list)
    show = bench_sub.add_parser("show", help="print one test's method and pass condition")
    show.add_argument("test_id")
    show.set_defaults(func=_bench_show)
    rec = bench_sub.add_parser("record", help="record a result; never sends MIDI")
    rec.add_argument("test_id")
    rec.add_argument("--date", help="YYYY-MM-DD (default: today)")
    rec.add_argument("--outcome", required=True, choices=["pass", "fail", "partial"])
    rec.add_argument("--notes", default="", help="what happened")
    rec.add_argument("--log", help="bench log path under captures/")
    rec.add_argument(
        "--record", help="capability record to update: device.command or device.reports.name"
    )
    rec.add_argument("--status", choices=["documented", "bench_verified"])
    rec.add_argument("--source", help="evidence-register id, for documented")
    rec.add_argument(
        "--value",
        action="append",
        default=[],
        help="command wire message, name=kind:data1[:data2]; repeatable",
    )
    rec.add_argument("--recipients", help="commands: other recipients checked")
    rec.add_argument("--side-effects", dest="side_effects", help="commands: side effects")
    rec.add_argument("--path-checked", dest="path_checked", help="reports: the input path checked")
    _add_paths(rec)
    rec.set_defaults(func=_bench_record)

    devices = sub.add_parser("devices", help="what this machine can see; opens nothing")
    devices_sub = devices.add_subparsers(dest="devices_command", required=True)
    dev_list = devices_sub.add_parser("list", help="MIDI port names and audio devices")
    dev_list.add_argument("--json", action="store_true")
    dev_list.set_defaults(func=_devices_list)
    tmpl = devices_sub.add_parser(
        "ports-template", help="print a local/ports.yaml skeleton for a rig, with the names seen"
    )
    tmpl.add_argument("rig", help="rig file")
    tmpl.set_defaults(func=_devices_ports_template)

    doctor = sub.add_parser(
        "doctor", help="check this machine and config; each failure names a fix"
    )
    doctor.add_argument(
        "paths", nargs="*", help="config files or folders (default: local/ or examples/)"
    )
    doctor.add_argument("--json", action="store_true")
    doctor.set_defaults(func=_doctor)

    synth_cmd = sub.add_parser("synth", help="synthetic takes with exact annotations (T-08)")
    synth_sub = synth_cmd.add_subparsers(dest="synth_command", required=True)
    synth_sub.add_parser("list", help="the take variations and streams").set_defaults(
        func=_synth_list
    )
    render = synth_sub.add_parser("render", help="render song files to FLAC feeds plus annotations")
    render.add_argument("songs", nargs="+", help="song YAML files")
    render.add_argument(
        "--out",
        required=True,
        help="output folder (use recordings/synthetic; audio is never committed)",
    )
    render.add_argument(
        "--variation",
        action="append",
        default=[],
        help="one of `synth list`; repeatable (default: all)",
    )
    render.add_argument(
        "--rate", type=int, default=synth.DEFAULT_SAMPLE_RATE, help="sample rate (default 48000)"
    )
    render.set_defaults(func=_synth_render)

    replay = sub.add_parser("replay", help="causal replay of takes on the simulated clock (T-10)")
    replay_sub = replay.add_subparsers(dest="replay_command", required=True)
    feats = replay_sub.add_parser("features", help="onset, chroma and input health, block by block")
    feats.add_argument("take", help="take folder with manifest.json and annotations.json")
    feats.add_argument(
        "--block", type=int, default=replay_mod.DEFAULT_BLOCK, help="block size in samples"
    )
    feats.add_argument("--feeds", help="comma-separated feeds (default: all in the take)")
    feats.add_argument(
        "--beats",
        choices=["none", "oracle"],
        default="none",
        help="beat source for beat-synchronous chroma",
    )
    feats.add_argument(
        "--drop",
        action="append",
        default=[],
        help="feed:start_s:end_s, blocks withheld; repeatable",
    )
    feats.add_argument("--out", help="write the records as JSONL")
    feats.set_defaults(func=_replay_features)

    ui = sub.add_parser("ui", help="open the local web workbench (127.0.0.1 only, ADR-018)")
    ui.add_argument(
        "--port",
        type=int,
        default=8765,
        help="port on 127.0.0.1 (default: 8765; 0 picks a free one)",
    )
    ui.add_argument("--no-browser", action="store_true", help="do not open a browser")
    ui.add_argument(
        "paths", nargs="*", help="config files or folders for doctor (default: local/ or examples/)"
    )
    _add_paths(ui)
    ui.set_defaults(func=_ui)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
