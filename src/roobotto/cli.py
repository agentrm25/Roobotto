"""Command-line entry point: ``roobotto validate-config`` and ``roobotto schemas export``."""

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from roobotto.config import schemas
from roobotto.config.validate import validate_files


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
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
