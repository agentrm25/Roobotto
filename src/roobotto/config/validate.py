"""Validate a set of config files, including the checks that span files."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

from roobotto.config.loader import Config, Issue, load_config
from roobotto.config.rig import Rig
from roobotto.config.setlist import Setlist
from roobotto.config.song import Song


@dataclass
class Report:
    issues: list[Issue] = field(default_factory=lambda: [])
    configs: dict[Path, Config] = field(default_factory=lambda: {})

    @property
    def ok(self) -> bool:
        return not self.issues


def expand(paths: Iterable[Path]) -> list[Path]:
    """Files as given; directories expand to their ``*.yaml``/``*.yml`` files, sorted."""
    out: list[Path] = []
    for path in paths:
        if path.is_dir():
            out.extend(sorted(p for p in path.rglob("*") if p.suffix in {".yaml", ".yml"}))
        else:
            out.append(path)
    return out


def _in_local(path: Path) -> bool:
    resolved = path.resolve()
    try:
        return resolved.relative_to(Path.cwd().resolve()).parts[:1] == ("local",)
    except ValueError:
        return "local" in resolved.parts


def _check_duplicates(report: Report) -> None:
    seen: dict[tuple[str, str], Path] = {}
    for path, cfg in report.configs.items():
        key = (cfg.kind, cfg.id)
        if key in seen:
            report.issues.append(
                Issue(
                    str(path), "id", f"{cfg.kind} id '{cfg.id}' is already defined in {seen[key]}"
                )
            )
        else:
            seen[key] = path


def _check_lyric_anchors(report: Report) -> None:
    """ADR-008: lyric anchors live only in local/."""
    for path, cfg in report.configs.items():
        if isinstance(cfg, Song) and not _in_local(path):
            for index, sec in enumerate(cfg.sections):
                if sec.lyric_anchor is not None:
                    report.issues.append(
                        Issue(
                            str(path),
                            f"sections[{index}].lyric_anchor",
                            "lyric anchors are private show data and may only appear under "
                            "local/ (ADR-008)",
                        )
                    )


def _check_setlists(report: Report) -> None:
    songs = {c.id for c in report.configs.values() if isinstance(c, Song)}
    if not songs:
        return
    for path, cfg in report.configs.items():
        if isinstance(cfg, Setlist):
            for index, entry in enumerate(cfg.entries):
                if entry.song not in songs:
                    report.issues.append(
                        Issue(str(path), f"entries[{index}].song", f"unknown song '{entry.song}'")
                    )


def _check_cues_against_rig(report: Report) -> None:
    rigs = [(p, c) for p, c in report.configs.items() if isinstance(c, Rig)]
    songs = [(p, c) for p, c in report.configs.items() if isinstance(c, Song)]
    if len(rigs) > 1:
        report.issues.append(
            Issue(
                str(rigs[1][0]), "", "more than one rig was given; cues are checked against one rig"
            )
        )
        return
    if not rigs:
        return
    rig = rigs[0][1]
    for path, song in songs:
        for ci, cue in enumerate(song.cues):
            for key, value in cue.state.items():
                where = f"cues[{ci}].state.{key}"
                device_id, intent = key.split(".")
                device = rig.device(device_id)
                if device is None:
                    report.issues.append(Issue(str(path), where, f"unknown device '{device_id}'"))
                elif device.owner != "roobotto":
                    report.issues.append(
                        Issue(
                            str(path),
                            where,
                            f"device '{device_id}' is owned by the performer; "
                            "Roobotto cannot cue it",
                        )
                    )
                elif intent not in device.commands:
                    report.issues.append(
                        Issue(str(path), where, f"device '{device_id}' has no command '{intent}'")
                    )
                elif value not in device.commands[intent].values:
                    known = ", ".join(sorted(device.commands[intent].values))
                    report.issues.append(
                        Issue(
                            str(path), where, f"'{value}' is not a value of {key} (known: {known})"
                        )
                    )


def validate_files(paths: Iterable[Path]) -> Report:
    report = Report()
    for path in expand(paths):
        cfg, issues = load_config(path)
        report.issues.extend(issues)
        if cfg is not None:
            report.configs[path] = cfg
    _check_duplicates(report)
    _check_lyric_anchors(report)
    _check_setlists(report)
    _check_cues_against_rig(report)
    return report
