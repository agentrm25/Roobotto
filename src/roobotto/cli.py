"""Command-line entry point: ``validate-config``, ``schemas export``, ``run --dry-run``, ``bench``,
``devices``, ``doctor``, ``synth``, ``replay``, ``benchmark``, ``record`` and ``ui``."""

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
from roobotto.eval import benchmark as benchmark_mod
from roobotto.eval import replay as replay_mod
from roobotto.eval import synth
from roobotto.record import align, capture
from roobotto.tracker import baselines, follower, propose
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
            replay_take=Path(args.replay) if args.replay else None,
            reference=Path(args.reference) if args.reference else None,
            milestone=args.milestone,
            feeds=[f for f in (args.feeds or "").split(",") if f] or None,
            block=args.block,
            log=Path(args.log) if args.log else None,
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


def _replay_baseline(args: argparse.Namespace) -> int:
    if args.method not in baselines.METHODS:
        print(
            f"unknown method '{args.method}'; choose from {', '.join(baselines.METHODS)}",
            file=sys.stderr,
        )
        return 2
    try:
        result = baselines.run_baseline(
            args.method, Path(args.take), block=args.block, feed=args.feed
        )
    except (replay_mod.MissingFeedError, synth.SynthError, OSError, ValueError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "method": result.method,
                    "take": result.take,
                    "score": result.score.as_dict(),
                    "output_hash": result.output_hash,
                    "estimates": [e.as_dict() for e in result.estimates],
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    print(f"{result.method} on {result.take}: {len(result.estimates)} estimates")
    for key, value in result.score.as_dict().items():
        if key != "method":
            print(f"  {key}: {value}")
    print(f"output hash {result.output_hash}")
    return 0


def _replay_follow(args: argparse.Namespace) -> int:
    feeds = tuple(f for f in (args.feeds or "").split(",") if f) or follower.DEFAULT_FEEDS
    config = follower.FollowerConfig(feeds=feeds, temperature=args.temperature)
    try:
        reference = follower.build_reference(
            [Path(p) for p in args.reference], feeds=feeds, block=args.block
        )
        result = follower.run_follower(Path(args.take), reference, config=config, block=args.block)
        proposal = (
            propose.propose_cues(result, [Path(p) for p in args.propose]) if args.propose else None
        )
    except (
        follower.FollowerError,
        replay_mod.MissingFeedError,
        synth.SynthError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    feeds_text = ", ".join(feeds)
    print(f"follower on {result.annotations.take}: {len(result.states)} states, feeds {feeds_text}")
    for key, value in result.score.as_dict().items():
        if key != "method":
            print(f"  {key}: {value}")
    print(f"output hash {result.output_hash}")
    if proposal is not None:
        print("proposals through the real gate (mock profile, unarmed):")
        for key, value in proposal.summary().items():
            print(f"  {key}: {value}")
    if args.out:
        Path(args.out).write_text(
            json.dumps(
                {
                    "take": result.annotations.take,
                    "config": config.as_dict(),
                    "score": result.score.as_dict(),
                    "output_hash": result.output_hash,
                    "states": [s.model_dump(mode="json") for s in result.states],
                    "proposals": proposal.summary() if proposal else None,
                },
                indent=1,
            )
            + "\n",
            encoding="utf-8",
        )
    return 0


def _benchmark(args: argparse.Namespace) -> int:
    try:
        protocol = benchmark_mod.load_protocol(Path(args.protocol))
        result = benchmark_mod.run_benchmark(
            protocol, repo=Path.cwd(), out_dir=Path(args.out) if args.out else None
        )
    except (
        benchmark_mod.BenchmarkError,
        synth.SynthError,
        follower.FollowerError,
        OSError,
        ValueError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    label = str(result.summary["label"]).upper()
    takes = len(result.summary["manifests"])
    print(f"{protocol.title} [{label}]: {len(result.rows)} result rows over {takes} takes")
    print(f"wrote {result.out_dir / 'report.md'}, results.csv, summary.json and plots")
    return 0


def _record_session(args: argparse.Namespace) -> int:
    """Capture from the real devices named in the session file. Input only: nothing is played
    unless the session's burst names an output device, and no MIDI is sent."""
    import time as _time

    try:
        config = capture.load_session(Path(args.session))
    except capture.CaptureError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    out = Path(args.out)
    if (out / "manifest.json").is_file():
        print(f"error: {out} already holds a take; choose another folder", file=sys.stderr)
        return 1
    sources = [
        capture.SoundDeviceSource(
            d.id, d.name, max(d.channels) + 1, config.sample_rate, config.block
        )
        for d in config.devices
    ]
    try:
        ports = (
            capture.midi_input_names() if config.midi_ports == "all" else list(config.midi_ports)
        )
    except Exception as exc:
        print(f"MIDI ports could not be listed ({exc}); no MIDI log for this take", file=sys.stderr)
        ports = []
    midi = capture.MidiInputLogger(ports, out / "midi.jsonl") if ports else None
    session = capture.Session(config, out, sources, midi=midi)
    try:
        session.start()
    except Exception as exc:
        print(f"error: capture could not start: {exc}", file=sys.stderr)
        return 1
    print(
        f"recording to {out}: {', '.join(f'{d.id} ({d.name})' for d in config.devices)}; "
        f"MIDI ports: {', '.join(ports) or 'none'}"
    )
    print("sync burst now (start)")
    session.mark_burst("start")
    try:
        if args.seconds:
            _time.sleep(args.seconds)
        else:
            print("press Ctrl-C to stop")
            while True:
                _time.sleep(1)
    except KeyboardInterrupt:
        pass
    print("sync burst now (end)")
    session.mark_burst("end")
    result = session.stop()
    print(
        f"wrote {out / 'manifest.json'}; frames {result.blocks_written}; "
        f"flagged blocks {result.overflow_blocks}; MIDI messages {result.midi_messages}"
    )
    if result.overflow_blocks:
        print(
            "warning: the driver flagged overflow or underflow; the validator marks those "
            "intervals",
            file=sys.stderr,
        )
    return 0


def _record_validate(args: argparse.Namespace) -> int:
    folder = Path(args.take)
    if not (folder / "manifest.json").is_file():
        print(f"error: no take at {folder}", file=sys.stderr)
        return 1
    try:
        info_path = folder / "capture.json"
        device_of: dict[str, str] = {}
        if info_path.is_file():
            info = json.loads(info_path.read_text(encoding="utf-8"))
            for dev in info.get("devices", []):
                for stream in dev.get("channels", {}).values():
                    device_of[str(stream)] = str(dev["id"])
        report = align.validate(
            folder, reference=args.reference, tolerance_ms=args.tolerance_ms, device_of=device_of
        )
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    verdict = "PASS" if report.passed else "FAIL"
    print(f"alignment against '{report.reference}', tolerance {report.tolerance_ms} ms: {verdict}")
    for s in report.streams:
        print(
            f"  {s.stream}: {s.markers} markers, offset {s.offset_ms} ms, drift {s.drift_ppm} ppm,"
            f" residual max {s.max_residual_ms} ms (raw max {s.max_raw_ms} ms), "
            f"invalid {len(s.invalid)} interval(s)"
        )
        for iv in s.invalid:
            print(f"      {iv.start_s:.3f}-{iv.end_s:.3f} s: {iv.reason}")
    print(f"invalid time {report.invalid_total_s} s; wrote {folder / 'alignment.json'}")
    return 0 if report.passed else 2


def _record_correct(args: argparse.Namespace) -> int:
    folder = Path(args.take)
    if not (folder / "alignment.json").is_file():
        print(f"error: run `roobotto record validate {folder}` first", file=sys.stderr)
        return 1
    try:
        out = align.correct(folder)
    except (ValueError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    print(f"wrote idealized, drift-corrected copies under {out} (never a reported result)")
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
    run.add_argument(
        "--replay", help="a take folder: the follower listens and drives the engine (T-14)"
    )
    run.add_argument("--reference", help="reference take for --replay (default: sibling nominal)")
    run.add_argument(
        "--milestone",
        type=int,
        default=1,
        help="engine milestone setting (default 1; 4+ lets tracker evidence schedule cues)",
    )
    run.add_argument("--feeds", help="follower feeds for --replay (default: chords,bass)")
    run.add_argument("--block", type=int, default=512, help="block size for --replay")
    run.add_argument("--log", help="write the JSONL decision log here")
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
    base = replay_sub.add_parser("baseline", help="run a baseline (B0, B1) on a take and score it")
    base.add_argument("take", help="take folder")
    base.add_argument(
        "--method", required=True, help="B0 (fixed timeline) or B1 (onset PLL bar counting)"
    )
    base.add_argument("--block", type=int, default=replay_mod.DEFAULT_BLOCK)
    base.add_argument(
        "--feed", default="drums", help="feed for the onset envelope (default: drums)"
    )
    base.add_argument("--out", help="write score and estimates as JSON")
    base.set_defaults(func=_replay_baseline)
    follow = replay_sub.add_parser(
        "follow", help="run the bar-grid follower (B3/B4) on a take and score it"
    )
    follow.add_argument("take", help="take folder")
    follow.add_argument(
        "--reference", action="append", required=True, help="reference take folder(s); repeatable"
    )
    follow.add_argument("--feeds", help="listening feeds, comma-separated (default: chords,bass)")
    follow.add_argument("--temperature", type=float, default=follower.FollowerConfig().temperature)
    follow.add_argument("--block", type=int, default=replay_mod.DEFAULT_BLOCK)
    follow.add_argument(
        "--propose",
        action="append",
        default=[],
        help="config paths; route the states through the real engine in the mock profile",
    )
    follow.add_argument("--out", help="write score, states and proposals as JSON")
    follow.set_defaults(func=_replay_follow)

    bench_cmd = sub.add_parser(
        "benchmark", help="run a protocol over the corpus; write CSV, report and plots (T-13)"
    )
    bench_cmd.add_argument("protocol", help="protocol YAML (see experiments/protocols/)")
    bench_cmd.add_argument("--out", help="output folder (default: the protocol's `out`)")
    bench_cmd.set_defaults(func=_benchmark)

    record = sub.add_parser("record", help="capture takes and validate their alignment (T-07)")
    record_sub = record.add_subparsers(dest="record_command", required=True)
    rec_session = record_sub.add_parser(
        "session", help="capture from the devices in a session file; input only"
    )
    rec_session.add_argument("session", help="session YAML")
    rec_session.add_argument(
        "--out", required=True, help="take folder to create (under recordings/)"
    )
    rec_session.add_argument(
        "--seconds", type=float, default=0.0, help="stop after this long (default: Ctrl-C)"
    )
    rec_session.set_defaults(func=_record_session)
    rec_validate = record_sub.add_parser(
        "validate", help="marker alignment, drift, invalid intervals"
    )
    rec_validate.add_argument("take", help="take folder")
    rec_validate.add_argument("--reference", help="reference stream (default: the first)")
    rec_validate.add_argument("--tolerance-ms", dest="tolerance_ms", type=float, default=1.0)
    rec_validate.set_defaults(func=_record_validate)
    rec_correct = record_sub.add_parser(
        "correct", help="write idealized drift-corrected copies under corrected/"
    )
    rec_correct.add_argument("take", help="take folder")
    rec_correct.set_defaults(func=_record_correct)

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
