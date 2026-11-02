"""The bench worksheet (plan Section 10.2) as data, plus recording a result.

A result is written the way the owner would write it by hand: a row in
``docs/hardware-verification.md`` and, when a test verifies a capability record, a text edit of
that record in the rig file that changes only its ``status``, ``evidence`` and (for commands) the
wire messages in ``values``. Comments and layout in the rig file are kept. The rig is re-validated
before anything is written. This module guides and records; it never sends MIDI (ADR-018).
"""

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml

from roobotto.config.loader import load_config_text
from roobotto.config.rig import Evidence, Rig, evidence_errors


class WorksheetError(ValueError):
    """A result that cannot be recorded, with the reason."""


@dataclass(frozen=True)
class BenchTest:
    id: str
    title: str
    method: str
    passes: str
    gates: str
    """Which use this test gates (Section 10.2, "Which tests gate what")."""


Kind = Literal["command", "report"]
Status = Literal["documented", "bench_verified"]
Outcome = Literal["pass", "fail", "partial"]


@dataclass(frozen=True)
class WireValue:
    kind: Literal["note_on", "note_off", "control_change", "program_change"]
    data1: int
    data2: int | None = None

    def yaml(self) -> str:
        if self.data2 is None:
            return f"{{ kind: {self.kind}, data1: {self.data1} }}"
        return f"{{ kind: {self.kind}, data1: {self.data1}, data2: {self.data2} }}"

    def summary(self) -> str:
        return f"{self.kind} {self.data1}" + ("" if self.data2 is None else f" {self.data2}")


@dataclass(frozen=True)
class CapabilityChange:
    """Move one capability record (a device's command or report) to a new status."""

    device: str
    kind: Kind
    name: str
    status: Status
    source: str | None = None
    """Evidence-register id, needed for ``documented``."""
    bench_log: str | None = None
    """Bench log under captures/, needed for ``bench_verified``; defaults to the result's."""
    values: Mapping[str, WireValue] | None = None
    """Commands only: the wire message for each cue value the bench test established."""
    other_recipients: str = ""
    """Commands table: other recipients checked."""
    side_effects: str = ""
    """Commands table: side effects."""
    path_checked: str = ""
    """Reports table: the input path checked."""

    @property
    def label(self) -> str:
        return (
            f"{self.device}.{self.name}"
            if self.kind == "command"
            else f"{self.device}.reports.{self.name}"
        )


@dataclass(frozen=True)
class Result:
    test_id: str
    date: str
    outcome: Outcome
    notes: str
    bench_log: str | None = None
    capability: CapabilityChange | None = field(default=None)


# --- The tests, copied from plan Section 10.2. test_worksheet checks them against the plan. ---

_GATES = {
    "physical": "Physical MVP output through the show routing",
    "bench": "A physically isolated bench setup (the MVP wired straight to the Mac)",
    "m1": "M1 use",
    "enable": "When each output is enabled",
}

TESTS: tuple[BenchTest, ...] = (
    BenchTest(
        "H-1",
        "Helix snapshot reports and echoes",
        "Turn on *Send Snapshot CC*. Watch USB and the H4 path with an external monitor while "
        "you press each snapshot. Then send CC69 to the Helix and see whether it echoes a CC69 "
        "back.",
        "CC69 values 0–7 arrive on the expected channel. The authoritative path is chosen. Echo "  # noqa: RUF001
        "behavior is documented and sets the echo window. No other device reacts.",
        _GATES["m1"],
    ),
    BenchTest(
        "H-2",
        "Helix song changes and preset load",
        "Change presets from the footswitches, and by selecting a song in Showcase. Note any "
        "CC69 the preset sends as it loads.",
        "A Program Change is seen for both, or the silent path is documented. Load-time snapshot "
        "messages are documented and set the initialization window.",
        _GATES["m1"],
    ),
    BenchTest(
        "H-3",
        "Snapshot recall latency",
        "Copy a live preset and set its Input block to USB In 3/4. Play a test signal from the "
        "Mac, send CC69, and detect the change on USB 1/2. Repeat 100 times.",
        "Latency distribution recorded, plus any gap, click, or cut-off tail.",
        _GATES["enable"] + " (Helix opt-in)",
    ),
    BenchTest(
        "H-4",
        "Document today's cue architecture",
        "For every song preset, list the Instant commands, the values that change per snapshot, "
        "and the destinations.",
        "A complete table in `local/` (private).",
        _GATES["physical"] + " (if preset B is used)",
    ),
    BenchTest(
        "H-5",
        "Channel collision audit",
        "List every device listening on channels 1, 2, 3, 4, 5, and 15, and on each H4 port. "
        "Read the Helix's Bypass/Control and Matrix MIDI channel settings. In a safe test preset "
        "at low volume, send a harmless CC on channels 2 and 3 and confirm the Helix doesn't "
        "react.",
        "No unintended recipient, or H4 filters added to prevent one.",
        _GATES["physical"],
    ),
    BenchTest(
        "H-6",
        "USB audio channel map",
        "Run `roobotto devices list`, then record USB 1/2, 7, and 8 while playing and singing.",
        "48 kHz. USB 7 is dry guitar and USB 8 is dry vocal, with no effects audible.",
        "Capture (T-07)",
    ),
    BenchTest(
        "M-1",
        "MVP mapping and behavior",
        "First-time procedure: play each candidate note on channel 3 from the KeyLab or "
        "SendMIDI. Repeat a note, overlap notes, and send note-offs.",
        "The note map is documented, retrigger behavior is known, and event-or-state handling "
        "is decided. Mapping marked bench_verified.",
        _GATES["physical"] + "; " + _GATES["bench"],
    ),
    BenchTest(
        "M-2",
        "MVP latency",
        "Send the verified note 100 times with `measure-latency`, using one of three timing "
        "references: Art-Net frames on the same Mac clock (if broadcast); a light sensor on a "
        "spare audio input, recorded on the same clock as the send; or 240 fps video that also "
        "shows a reference triggered by the same send. At 240 fps, frames are about 4.17 ms "
        "apart, but that alone doesn't establish end-to-end accuracy.",
        "Distribution recorded, along with the reference method and its own error. Fixture "
        "response measured separately.",
        _GATES["bench"] + "; day-14 item 7",
    ),
    BenchTest(
        "A-1",
        "AUM session recall on channel 15",
        "Trigger a recall while audio plays, and record the iPad's output.",
        "Load time and any audio gap are documented. Use mid-song only if it's glitch-free.",
        _GATES["enable"] + " (AUM)",
    ),
    BenchTest(
        "K-1",
        "MicroFreak program change on channel 2",
        "Send a Program Change with and without a note held.",
        "Behavior is documented, and changes are scheduled only where they're safe.",
        _GATES["enable"] + " (MicroFreak)",
    ),
    BenchTest(
        "R-1",
        "STOP, RESTORE, and RESUME",
        "Set preset A to today's routing and preset B to Roobotto's. Starting in B: (1) press "
        "STOP and confirm Roobotto's cues stop; (2) RESTORE with the H4 button while the Mac is "
        "unplugged, and confirm today's performer cues work; (3) switch back to B, RESUME from "
        "the HUD, and confirm nothing stale fires and each device has one owner.",
        "Each action does what Section 5.2 says. Switch time and side effects (hung notes) are "
        "documented. The H4 button can be reached on stage.",
        _GATES["m1"] + "; " + _GATES["physical"] + " (if preset B is used)",
    ),
    BenchTest(
        "R-2",
        "The Mac's MIDI path into the rig",
        "Choose one: the H4's USB-C port (if the iPad doesn't need it), a USB-MIDI interface "
        "into H4 DIN, or WIDI (never for critical paths).",
        "One path documented and tested.",
        _GATES["physical"],
    ),
    BenchTest(
        "P-1",
        "Footswitches",
        "Assign GO, BACK/MANUAL, and STOP (EXTEND later) to free switches, sending raw press and "
        "release events on a dedicated channel.",
        "Messages and latency logged. The map matches ADR-010.",
        _GATES["m1"],
    ),
    BenchTest(
        "L-1",
        "Feedback loops",
        "With preset B active, send a test message from the Mac and watch for echoes.",
        "Nothing returns to the Mac's input.",
        _GATES["physical"] + "; " + _GATES["bench"],
    ),
)
TESTS_BY_ID: Mapping[str, BenchTest] = {t.id: t for t in TESTS}


# --- Parsing a result from JSON (the page) or from CLI arguments. ---


def _text(data: Mapping[str, Any], key: str, required: bool = False) -> str | None:
    value = data.get(key)
    if value is None or value == "":
        if required:
            raise WorksheetError(f"{key} is required")
        return None
    if not isinstance(value, str):
        raise WorksheetError(f"{key} must be text")
    return value.strip()


def _wire_value(name: str, raw: Any) -> WireValue:
    if not isinstance(raw, Mapping):
        raise WorksheetError(f"value '{name}' must be a mapping with kind, data1 and data2")
    data: Mapping[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    kind = data.get("kind")
    if kind not in ("note_on", "note_off", "control_change", "program_change"):
        raise WorksheetError(
            f"value '{name}': kind must be note_on, note_off, control_change or program_change"
        )
    d1, d2 = data.get("data1"), data.get("data2")
    if not isinstance(d1, int) or isinstance(d1, bool):
        raise WorksheetError(f"value '{name}': data1 must be an integer 0-127")
    if d2 is not None and (not isinstance(d2, int) or isinstance(d2, bool)):
        raise WorksheetError(f"value '{name}': data2 must be an integer 0-127 or absent")
    return WireValue(kind, d1, d2)


def capability_from_dict(data: Mapping[str, Any]) -> CapabilityChange:
    kind = _text(data, "kind", required=True)
    if kind not in ("command", "report"):
        raise WorksheetError("kind must be command or report")
    status = _text(data, "status", required=True)
    if status not in ("documented", "bench_verified"):
        raise WorksheetError("status must be documented or bench_verified")
    values: dict[str, WireValue] | None = None
    raw_values = data.get("values")
    if raw_values is not None:
        if not isinstance(raw_values, Mapping):
            raise WorksheetError("values must be a mapping of cue value to wire message")
        items: Mapping[str, Any] = raw_values  # pyright: ignore[reportUnknownVariableType]
        values = {str(k): _wire_value(str(k), v) for k, v in items.items()}
    return CapabilityChange(
        device=_text(data, "device", required=True) or "",
        kind=kind,
        name=_text(data, "name", required=True) or "",
        status=status,
        source=_text(data, "source"),
        bench_log=_text(data, "bench_log"),
        values=values,
        other_recipients=_text(data, "other_recipients") or "",
        side_effects=_text(data, "side_effects") or "",
        path_checked=_text(data, "path_checked") or "",
    )


def result_from_dict(data: Mapping[str, Any]) -> Result:
    outcome = _text(data, "outcome", required=True)
    if outcome not in ("pass", "fail", "partial"):
        raise WorksheetError("outcome must be pass, fail or partial")
    raw_cap = data.get("capability")
    capability = None
    if raw_cap is not None:
        if not isinstance(raw_cap, Mapping):
            raise WorksheetError("capability must be a mapping")
        cap: Mapping[str, Any] = raw_cap  # pyright: ignore[reportUnknownVariableType]
        capability = capability_from_dict(cap)
    return Result(
        test_id=_text(data, "test_id", required=True) or "",
        date=_text(data, "date", required=True) or "",
        outcome=outcome,
        notes=_text(data, "notes") or "",
        bench_log=_text(data, "bench_log"),
        capability=capability,
    )


# --- Checking a result. ---


def _check_log(log: str | None, what: str) -> list[str]:
    if log is None:
        return []
    errors = evidence_errors("bench_verified", Evidence(bench_log=log))
    return [e.replace("status 'bench_verified' needs evidence.bench_log", what) for e in errors]


def check(result: Result) -> list[str]:
    """Everything that can be checked without the rig file."""
    errors: list[str] = []
    if result.test_id not in TESTS_BY_ID:
        errors.append(f"unknown test '{result.test_id}'")
    try:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", result.date):
            raise ValueError
        date.fromisoformat(result.date)
    except ValueError:
        errors.append(f"date '{result.date}' must be YYYY-MM-DD")
    errors += _check_log(result.bench_log, "bench log")
    cap = result.capability
    if cap is not None:
        if result.outcome != "pass":
            errors.append("only a pass can change a capability record's status")
        errors += evidence_errors(
            cap.status, Evidence(source=cap.source, bench_log=_cap_log(result))
        )
    return errors


def _cap_log(result: Result) -> str | None:
    cap = result.capability
    if cap is None:
        return None
    return cap.bench_log or result.bench_log


# --- Editing the rig file as text, so comments and layout survive. ---


def _mapping_get(node: Any, key: str) -> tuple[Any, Any] | None:
    if not isinstance(node, yaml.MappingNode):
        return None
    for key_node, value_node in node.value:  # pyright: ignore[reportUnknownVariableType]
        if isinstance(key_node, yaml.ScalarNode) and key_node.value == key:
            return key_node, value_node
    return None


def _span(node: Any) -> tuple[int, int]:
    return int(node.start_mark.index), int(node.end_mark.index)


def _flow_mapping(items: Mapping[str, str]) -> str:
    return "{ " + ", ".join(f"{k}: {v}" for k, v in items.items()) + " }"


@dataclass(frozen=True)
class _Record:
    node: Any
    key_node: Any
    data: dict[str, Any]
    """The record as plain data, for summaries."""


def _find_record(root: Any, cap: CapabilityChange) -> _Record:
    devices = _mapping_get(root, "devices")
    if devices is None or not isinstance(devices[1], yaml.SequenceNode):
        raise WorksheetError("the rig file has no devices list")
    for dev in devices[1].value:  # pyright: ignore[reportUnknownVariableType]
        ident = _mapping_get(dev, "id")
        if ident is not None and ident[1].value == cap.device:
            break
    else:
        raise WorksheetError(f"no device '{cap.device}' in the rig file")
    section = _mapping_get(dev, "commands" if cap.kind == "command" else "reports")
    if section is None:
        raise WorksheetError(f"device '{cap.device}' has no {cap.kind}s")
    found = _mapping_get(section[1], cap.name)
    if found is None:
        raise WorksheetError(f"device '{cap.device}' has no {cap.kind} '{cap.name}'")
    key_node, node = found
    loader = yaml.SafeLoader("")
    raw: Mapping[Any, Any] = loader.construct_mapping(node, deep=True)  # pyright: ignore[reportUnknownMemberType]
    data: dict[str, Any] = {str(k): v for k, v in raw.items()}
    return _Record(node, key_node, data)


def edit_rig_text(
    text: str, cap: CapabilityChange, bench_log: str | None
) -> tuple[str, dict[str, Any]]:
    """Return the rig text with the record changed, plus the record's data after the change."""
    root: Any = yaml.compose(text, Loader=yaml.SafeLoader)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    rec = _find_record(root, cap)
    edits: list[tuple[int, int, str]] = []

    status = _mapping_get(rec.node, "status")
    if status is None:
        raise WorksheetError(f"{cap.label} has no status field")
    status_key, status_value = status
    edits.append((*_span(status_value), cap.status))

    evidence: dict[str, str] = {}
    old = rec.data.get("evidence")
    if isinstance(old, dict):
        evidence.update({str(k): str(v) for k, v in old.items()})  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
    if cap.source:
        evidence["source"] = cap.source
    if bench_log:
        evidence["bench_log"] = bench_log
    evidence_text = _flow_mapping(evidence)
    existing = _mapping_get(rec.node, "evidence")
    if existing is not None:
        edits.append((*_span(existing[1]), evidence_text))
    elif rec.node.flow_style:
        end = _span(status_value)[1]
        edits.append((end, end, f", evidence: {evidence_text}"))
    else:
        line_end = text.index("\n", _span(status_value)[1])
        indent = " " * int(status_key.start_mark.column)
        edits.append((line_end, line_end, f"\n{indent}evidence: {evidence_text}"))

    after = dict(rec.data)
    after["status"] = cap.status
    after["evidence"] = evidence
    if cap.values:
        if cap.kind != "command":
            raise WorksheetError("values apply to commands only")
        values = _mapping_get(rec.node, "values")
        if values is None:
            raise WorksheetError(f"{cap.label} has no values")
        new_values: dict[str, Any] = dict(after.get("values") or {})  # pyright: ignore[reportUnknownArgumentType]
        for name, wire in cap.values.items():
            slot = _mapping_get(values[1], name)
            if slot is None:
                raise WorksheetError(f"'{name}' is not a cue value of {cap.label}")
            edits.append((*_span(slot[1]), wire.yaml()))
            new_values[name] = {"kind": wire.kind, "data1": wire.data1, "data2": wire.data2}
        after["values"] = new_values

    edits.sort(key=lambda e: e[0], reverse=True)
    for i in range(1, len(edits)):
        if edits[i][1] > edits[i - 1][0]:
            raise WorksheetError("internal: overlapping edits")
    out = text
    for start, end, replacement in edits:
        out = out[:start] + replacement + out[end:]
    return out, after


# --- The hardware-verification document. ---

_LOG_HEADING = "## Bench test log"
_COMMANDS_HEADING = "## Verified commands"
_REPORTS_HEADING = "## Verified input reports"


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def _append_row(doc: str, heading: str, row: str) -> str:
    lines = doc.split("\n")
    try:
        start = lines.index(heading)
    except ValueError as exc:
        raise WorksheetError(f"docs file has no '{heading}' section") from exc
    first = next((i for i in range(start + 1, len(lines)) if lines[i].startswith("|")), None)
    if first is None:
        raise WorksheetError(f"'{heading}' has no table")
    last = first
    while last + 1 < len(lines) and lines[last + 1].startswith("|"):
        last += 1
    if "_none yet_" in lines[last]:
        lines[last] = row
    else:
        lines.insert(last + 1, row)
    return "\n".join(lines)


def _wire_summary(raw: Any) -> str:
    if not isinstance(raw, dict):
        return "null"
    data: dict[str, Any] = raw  # pyright: ignore[reportUnknownVariableType]
    d2 = data.get("data2")
    return f"{data.get('kind')} {data.get('data1')}" + ("" if d2 is None else f" {d2}")


def _command_summary(data: Mapping[str, Any]) -> str:
    values: Mapping[str, Any] = data.get("values") or {}
    pairs = ", ".join(f"{k} = {_wire_summary(v)}" for k, v in values.items())
    return f"{data.get('type')} on {data.get('port')} channel {data.get('channel')}: {pairs}"


def _report_summary(name: str, data: Mapping[str, Any]) -> str:
    out = f"{name}: {data.get('kind')}"
    if data.get("number") is not None:
        out += f" {data['number']}"
    out += f" on channel {data.get('channel')}"
    values: list[Any] = data.get("values") or []
    if values:
        out += ", values " + ", ".join(str(v) for v in values)
    return out


def _doc_rows(result: Result, after: Mapping[str, Any] | None) -> list[tuple[str, str]]:
    rows = [
        (
            _LOG_HEADING,
            f"| {result.test_id} | {result.date} | {result.outcome} | "
            f"{_cell(result.bench_log or '')} | {_cell(result.notes)} |",
        )
    ]
    cap = result.capability
    if cap is not None and cap.status == "bench_verified" and after is not None:
        log = _cell(_cap_log(result) or "")
        if cap.kind == "command":
            rows.append(
                (
                    _COMMANDS_HEADING,
                    f"| {cap.label} | {_cell(_command_summary(after))} | {result.date} | {log} | "
                    f"{_cell(cap.other_recipients)} | {_cell(cap.side_effects)} |",
                )
            )
        else:
            rows.append(
                (
                    _REPORTS_HEADING,
                    f"| {cap.label} | {_cell(_report_summary(cap.name, after))} | {result.date} | "
                    f"{log} | {_cell(cap.path_checked)} | {_cell(result.notes)} |",
                )
            )
    return rows


def record(result: Result, doc: Path, rig: Path) -> list[Path]:
    """Record a result. Returns the files written; writes nothing when anything is wrong."""
    errors = check(result)
    if errors:
        raise WorksheetError("\n".join(errors))
    doc_text = doc.read_text(encoding="utf-8")
    after: dict[str, Any] | None = None
    rig_text: str | None = None
    if result.capability is not None:
        original = rig.read_text(encoding="utf-8")
        rig_text, after = edit_rig_text(original, result.capability, _cap_log(result))
        model, issues = load_config_text(rig_text, str(rig))
        if issues or not isinstance(model, Rig):
            raise WorksheetError(
                "the rig file would not validate after this change:\n"
                + "\n".join(str(i) for i in issues)
            )
    for heading, row in _doc_rows(result, after):
        doc_text = _append_row(doc_text, heading, row)
    written = [doc]
    doc.write_text(doc_text, encoding="utf-8")
    if rig_text is not None:
        rig.write_text(rig_text, encoding="utf-8")
        written.append(rig)
    return written


# --- What the page and the CLI show. ---


def records(rig: Path) -> dict[str, dict[str, Any]]:
    """Every capability record in the rig file, keyed by its label, with its status."""
    model, issues = load_config_text(rig.read_text(encoding="utf-8"), str(rig))
    if issues or not isinstance(model, Rig):
        raise WorksheetError("the rig file does not validate:\n" + "\n".join(map(str, issues)))
    out: dict[str, dict[str, Any]] = {}
    for dev in model.devices:
        for name, cmd in dev.commands.items():
            out[f"{dev.id}.{name}"] = {
                "device": dev.id,
                "kind": "command",
                "name": name,
                "status": cmd.status,
                "type": cmd.type,
                "port": cmd.port,
                "channel": cmd.channel,
                "values": list(cmd.values),
                "evidence": {k: v for k, v in cmd.evidence.model_dump().items() if v},
            }
        for name, rep in dev.reports.items():
            out[f"{dev.id}.reports.{name}"] = {
                "device": dev.id,
                "kind": "report",
                "name": name,
                "status": rep.status,
                "meaning": rep.meaning,
                "channel": rep.channel,
                "evidence": {k: v for k, v in rep.evidence.model_dump().items() if v},
            }
    return out


def logged_results(doc: Path) -> list[dict[str, str]]:
    """The rows already in the bench test log, oldest first."""
    text = doc.read_text(encoding="utf-8")
    if _LOG_HEADING not in text:
        return []
    section = text.split(_LOG_HEADING, 1)[1].split("\n## ", 1)[0]
    out: list[dict[str, str]] = []
    for line in section.splitlines():
        m = re.match(r"^\| ([A-Z]-\d+) \| (\d{4}-\d{2}-\d{2}) \| (pass|fail|partial) \|", line)
        if m:
            out.append({"test_id": m.group(1), "date": m.group(2), "outcome": m.group(3)})
    return out
