"""``roobotto doctor``: is this machine and this config ready? Each check names a fix.

The checks read files and port names; nothing is opened, sent, or changed.
"""

import re
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Literal

from roobotto.config.ports import Ports
from roobotto.config.rig import Rig
from roobotto.config.validate import Report, validate_files
from roobotto.devices.probe import System

Status = Literal["ok", "warn", "fail", "skip"]


@dataclass(frozen=True)
class Check:
    id: str
    title: str
    status: Status
    detail: str
    fix: str = ""

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _python(system: System) -> Check:
    v = system.python_version()
    text = ".".join(map(str, v))
    if v[:2] >= (3, 12):
        return Check("python", "Python", "ok", text)
    return Check(
        "python", "Python", "fail", text, "Roobotto needs Python 3.12 (ADR-002): run `uv sync`."
    )


def _midi(system: System) -> Check:
    mido = system.module_version("mido")
    rtmidi = system.module_version("rtmidi")
    if mido and rtmidi:
        return Check("midi", "MIDI library", "ok", f"mido {mido}, python-rtmidi {rtmidi}")
    missing = ", ".join(n for n, v in (("mido", mido), ("python-rtmidi", rtmidi)) if not v)
    return Check(
        "midi",
        "MIDI library",
        "fail",
        f"missing: {missing}",
        "Run `uv sync` in the Roobotto folder.",
    )


def _audio(system: System) -> Check:
    version = system.module_version("sounddevice")
    if version:
        return Check("audio", "Audio library", "ok", f"sounddevice {version}")
    return Check(
        "audio",
        "Audio library",
        "warn",
        "sounddevice cannot load (usually the PortAudio library is missing)",
        "On macOS, `uv sync` is enough: the wheel bundles PortAudio. On Linux install the "
        "PortAudio library (Debian/Ubuntu: `sudo apt install libportaudio2`). Audio capture "
        "tools stay off until this passes; MIDI tools still work.",
    )


def _configs(report: Report, paths: Sequence[Path]) -> Check:
    where = ", ".join(str(p) for p in paths)
    if not report.configs and report.ok:
        return Check(
            "configs",
            "Config files",
            "fail",
            f"no YAML files found in {where}",
            "Point doctor at your config folder: `roobotto doctor local` (or `examples`).",
        )
    if report.issues:
        detail = "\n".join(str(i) for i in report.issues[:10])
        return Check(
            "configs",
            "Config files",
            "fail",
            detail,
            f"Fix the lines above, then re-run `roobotto validate-config {where}`.",
        )
    kinds = sorted({c.kind for c in report.configs.values()})
    return Check(
        "configs",
        "Config files",
        "ok",
        f"{len(report.configs)} valid ({', '.join(kinds)}) in {where}",
    )


def _profile(rig: Rig | None) -> Check:
    if rig is None:
        return Check("profile", "Rig profile", "skip", "no valid rig")
    if rig.profile == "mock":
        return Check(
            "profile",
            "Rig profile",
            "ok",
            f"mock profile '{rig.id}': every send goes to the mock adapter; nothing reaches "
            "hardware",
        )
    verified = sum(
        1 for d in rig.devices for c in d.commands.values() if c.status == "bench_verified"
    )
    return Check(
        "profile",
        "Rig profile",
        "ok",
        f"physical profile '{rig.id}' with {verified} bench_verified command(s); outputs still "
        "need explicit arming for each run (I-2)",
    )


def _ports(rig: Rig | None, ports: Ports | None, system: System) -> Check:
    if rig is None:
        return Check("ports", "MIDI port names", "skip", "no valid rig")
    physical = [p for p in rig.ports if p.transport != "mock"]
    if not physical:
        return Check(
            "ports", "MIDI port names", "ok", "all rig ports are mock ports; no system names needed"
        )
    if ports is None:
        return Check(
            "ports",
            "MIDI port names",
            "fail",
            f"{len(physical)} physical port(s) in rig '{rig.id}' but no ports file was given",
            "Run `roobotto devices ports-template <rig file>` and save the result as "
            "local/ports.yaml, then fill in a system name for each physical port.",
        )
    try:
        seen_in, seen_out = set(system.midi_inputs()), set(system.midi_outputs())
    except Exception as exc:
        return Check(
            "ports",
            "MIDI port names",
            "fail",
            f"MIDI ports could not be listed: {exc}",
            "Run `uv sync`; see the MIDI library check.",
        )
    problems: list[str] = []
    for port in physical:
        name = ports.ports.get(port.id)
        if not name:
            problems.append(f"{port.id}: no system name")
            continue
        wanted = {"in": seen_in, "out": seen_out, "both": seen_in | seen_out}[port.direction]
        if name not in wanted:
            problems.append(
                f"{port.id}: '{name}' is not among this machine's {port.direction}put ports"
            )
    if problems:
        return Check(
            "ports",
            "MIDI port names",
            "fail",
            "\n".join(problems),
            "Run `roobotto devices list`, copy the exact port name into local/ports.yaml, and "
            "check the device is plugged in and powered.",
        )
    return Check(
        "ports", "MIDI port names", "ok", f"{len(physical)} physical port(s) named and present"
    )


def _dirs(cwd: Path) -> Check:
    missing = [d for d in ("local", "captures") if not (cwd / d).is_dir()]
    if not missing:
        return Check("dirs", "Private folders", "ok", "local/ and captures/ exist")
    return Check(
        "dirs",
        "Private folders",
        "warn",
        f"missing: {', '.join(missing)}",
        f"Run `mkdir -p {' '.join(missing)}` in the Roobotto folder. Both are ignored by git: "
        "local/ holds private show data, captures/ holds bench logs.",
    )


def _bench_logs(rig: Rig | None, cwd: Path) -> Check:
    if rig is None:
        return Check("bench_logs", "Bench logs", "skip", "no valid rig")
    cited: list[tuple[str, str]] = []
    for dev in rig.devices:
        for name, cmd in dev.commands.items():
            if cmd.evidence.bench_log:
                cited.append((f"{dev.id}.{name}", cmd.evidence.bench_log))
        for name, rep in dev.reports.items():
            if rep.evidence.bench_log:
                cited.append((f"{dev.id}.reports.{name}", rep.evidence.bench_log))
    missing = [f"{label}: {log}" for label, log in cited if not (cwd / log).is_file()]
    if missing:
        return Check(
            "bench_logs",
            "Bench logs",
            "fail",
            "\n".join(missing),
            "Every bench_verified record cites a log under captures/. Put the log where the record "
            "says, or correct the path in the rig file.",
        )
    return Check("bench_logs", "Bench logs", "ok", f"{len(cited)} cited log(s) present")


def _verification_doc(cwd: Path) -> Check:
    path = cwd / "docs" / "hardware-verification.md"
    if not path.is_file():
        return Check(
            "verification_doc",
            "Verification doc",
            "warn",
            f"{path} not found",
            "Run doctor from the Roobotto folder, or restore docs/hardware-verification.md.",
        )
    text = path.read_text(encoding="utf-8")
    headings = ("## Verified commands", "## Verified input reports", "## Bench test log")
    missing = [h for h in headings if h not in text]
    if missing:
        return Check(
            "verification_doc",
            "Verification doc",
            "warn",
            f"missing section(s): {', '.join(missing)}",
            "Restore the section headings; the workbench appends rows under them.",
        )
    rows = len(re.findall(r"^\| [A-Z]-\d+ \| \d{4}-\d{2}-\d{2} \|", text, flags=re.M))
    return Check("verification_doc", "Verification doc", "ok", f"{rows} bench result(s) logged")


def run_doctor(paths: Sequence[Path], system: System, cwd: Path) -> list[Check]:
    report = validate_files(paths)
    rigs = [c for c in report.configs.values() if isinstance(c, Rig)]
    rig = rigs[0] if len(rigs) == 1 and report.ok else None
    ports = next((c for c in report.configs.values() if isinstance(c, Ports)), None)
    if rig is not None and ports is not None and ports.rig != rig.id:
        ports = None
    return [
        _python(system),
        _midi(system),
        _audio(system),
        _configs(report, paths),
        _profile(rig),
        _ports(rig, ports, system),
        _dirs(cwd),
        _bench_logs(rig, cwd),
        _verification_doc(cwd),
    ]


def render(checks: Sequence[Check]) -> list[str]:
    lines: list[str] = []
    for c in checks:
        lines.append(f"[{c.status:4}] {c.title}: {c.detail.splitlines()[0] if c.detail else ''}")
        for extra in c.detail.splitlines()[1:]:
            lines.append(f"       {extra}")
        if c.fix:
            lines.append(f"       fix: {c.fix}")
    worst = (
        "fail"
        if any(c.status == "fail" for c in checks)
        else "warn"
        if any(c.status == "warn" for c in checks)
        else "ok"
    )
    lines.append(
        {
            "ok": "All checks passed.",
            "warn": "Ready, with warnings.",
            "fail": "Not ready: fix the failures above.",
        }[worst]
    )
    return lines


def failed(checks: Sequence[Check]) -> bool:
    return any(c.status == "fail" for c in checks)
