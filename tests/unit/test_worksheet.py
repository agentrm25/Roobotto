"""T-16, bench-worksheet slice: Section 10.2's tests as data, and recording a result exactly as a
hand edit of docs/hardware-verification.md and the rig file would."""

import re
import shutil
from pathlib import Path

import pytest

from roobotto.config.loader import load_config
from roobotto.config.rig import Rig
from roobotto.ui import worksheet
from roobotto.ui.worksheet import (
    TESTS,
    CapabilityChange,
    Result,
    WireValue,
    WorksheetError,
    record,
    result_from_dict,
)

ROOT = Path(__file__).parents[2]
PLAN = ROOT / "docs" / "implementation-plan.md"
DOC = ROOT / "docs" / "hardware-verification.md"
RIG = ROOT / "examples" / "rig.template.yaml"


@pytest.fixture
def copies(tmp_path: Path) -> tuple[Path, Path]:
    doc = tmp_path / "hardware-verification.md"
    rig = tmp_path / "rig.yaml"
    shutil.copy(DOC, doc)
    shutil.copy(RIG, rig)
    return doc, rig


def _plan_table() -> list[tuple[str, str, str]]:
    rows: list[tuple[str, str, str]] = []
    for line in PLAN.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\| ([HMAKRPL]-\d) \| (.+?) \| (.+?) \| (.+?) \|$", line)
        if m:
            rows.append((m.group(1), m.group(2), m.group(4)))
    return rows


def test_tests_match_the_plan_table() -> None:
    plan = _plan_table()
    assert len(plan) == 14
    assert [(t.id, t.title, t.passes) for t in TESTS] == plan


def test_every_test_has_a_method_and_a_gate() -> None:
    for t in TESTS:
        assert t.method and t.passes and t.gates, t.id


def _h1(**overrides: object) -> Result:
    base: dict[str, object] = {
        "test_id": "H-1",
        "date": "2026-11-02",
        "outcome": "pass",
        "notes": "CC69 0-7 on channel 1 via USB; no echo seen",
        "bench_log": "captures/2026-11-02-helix/h1.md",
        "capability": CapabilityChange(
            device="guitar",
            kind="report",
            name="snapshot",
            status="bench_verified",
            path_checked="USB (authoritative)",
        ),
    }
    base.update(overrides)
    return Result(**base)  # pyright: ignore[reportArgumentType]


def test_h1_result_matches_a_hand_edit(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    before = rig.read_text(encoding="utf-8")
    written = record(_h1(), doc, rig)
    assert written == [doc, rig]

    # The rig change is exactly what a person would type: one status word and one evidence line.
    expected = before.replace(
        "        values: [0, 1, 2, 3, 4, 5, 6, 7]\n        status: unknown\n",
        "        values: [0, 1, 2, 3, 4, 5, 6, 7]\n        status: bench_verified\n"
        "        evidence: { bench_log: captures/2026-11-02-helix/h1.md }\n",
    )
    assert expected != before
    assert rig.read_text(encoding="utf-8") == expected
    model, issues = load_config(rig)
    assert not issues
    assert isinstance(model, Rig)

    text = doc.read_text(encoding="utf-8")
    assert "_none yet_" in text  # the commands table is untouched
    assert (
        "| guitar.reports.snapshot | snapshot: control_change 69 on channel 1, values 0, 1, 2, 3, 4, 5, 6, 7 "
        "| 2026-11-02 | captures/2026-11-02-helix/h1.md | USB (authoritative) | CC69 0-7 on channel 1 via USB; no echo seen |"
    ) in text
    assert (
        "| H-1 | 2026-11-02 | pass | captures/2026-11-02-helix/h1.md | CC69 0-7 on channel 1 via USB; no echo seen |"
    ) in text
    reports = text[text.index("## Verified input reports") :]
    assert "_none yet_" not in reports.split("## ")[1]


def test_two_results_append_in_order(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    record(_h1(capability=None), doc, rig)
    record(_h1(test_id="L-1", notes="nothing returned", capability=None), doc, rig)
    text = doc.read_text(encoding="utf-8")
    assert text.index("| H-1 | 2026-11-02") < text.index("| L-1 | 2026-11-02")
    assert rig.read_text(encoding="utf-8") == RIG.read_text(encoding="utf-8")


def test_bench_verified_without_a_bench_log_is_refused(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    before = (doc.read_text(encoding="utf-8"), rig.read_text(encoding="utf-8"))
    with pytest.raises(WorksheetError, match="bench_log"):
        record(_h1(bench_log=None), doc, rig)
    with pytest.raises(WorksheetError, match="captures/"):
        record(_h1(bench_log="logs/h1.md"), doc, rig)
    with pytest.raises(WorksheetError, match="captures/"):
        record(_h1(bench_log="captures/../secret.md"), doc, rig)
    assert (doc.read_text(encoding="utf-8"), rig.read_text(encoding="utf-8")) == before


def test_documented_needs_an_evidence_id(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    change = CapabilityChange(device="guitar", kind="report", name="snapshot", status="documented")
    with pytest.raises(WorksheetError, match="evidence"):
        record(_h1(bench_log=None, capability=change), doc, rig)
    ok = CapabilityChange(
        device="guitar", kind="report", name="snapshot", status="documented", source="E-25"
    )
    record(_h1(bench_log=None, capability=ok), doc, rig)
    text = rig.read_text(encoding="utf-8")
    assert "        status: documented\n        evidence: { source: E-25 }\n" in text
    # Only bench_verified records enter the verified tables.
    assert "| guitar.reports.snapshot |" not in doc.read_text(encoding="utf-8")


def test_flow_style_record_is_edited_in_place(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    change = CapabilityChange(
        device="guitar", kind="report", name="preset", status="bench_verified", path_checked="USB"
    )
    record(_h1(test_id="H-2", capability=change, bench_log="captures/h2.md"), doc, rig)
    text = rig.read_text(encoding="utf-8")
    assert (
        "      preset: { meaning: preset, source: helix, channel: 1, kind: program_change, "
        "status: bench_verified, evidence: { bench_log: captures/h2.md } }\n"
    ) in text
    assert not load_config(rig)[1]


def test_m1_command_gets_its_note_map(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    change = CapabilityChange(
        device="mvp",
        kind="command",
        name="scene",
        status="bench_verified",
        values={
            "chorus": WireValue("note_on", 60, 100),
            "solo_build": WireValue("note_on", 62, 100),
        },
        other_recipients="aum, guitar: no reaction",
        side_effects="none",
    )
    record(_h1(test_id="M-1", capability=change, bench_log="captures/m1.md"), doc, rig)
    text = rig.read_text(encoding="utf-8")
    assert (
        "        values: { chorus: { kind: note_on, data1: 60, data2: 100 }, "
        "solo_build: { kind: note_on, data1: 62, data2: 100 } }\n"
        "        status: bench_verified\n"
        "        evidence: { bench_log: captures/m1.md }\n"
    ) in text
    model, issues = load_config(rig)
    assert not issues and isinstance(model, Rig)
    assert model.devices[0].commands["scene"].status == "bench_verified"
    assert (
        "| mvp.scene | one_shot on mock_out_mvp channel 3: chorus = note_on 60 100, "
        "solo_build = note_on 62 100 | 2026-11-02 | captures/m1.md | aum, guitar: no reaction | none |"
    ) in doc.read_text(encoding="utf-8")


def test_command_without_its_wire_messages_is_refused(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    before = rig.read_text(encoding="utf-8")
    change = CapabilityChange(device="mvp", kind="command", name="scene", status="bench_verified")
    with pytest.raises(WorksheetError, match="wire message"):
        record(_h1(test_id="M-1", capability=change), doc, rig)
    assert rig.read_text(encoding="utf-8") == before


def test_unknown_record_value_or_test_is_refused(copies: tuple[Path, Path]) -> None:
    doc, rig = copies
    with pytest.raises(WorksheetError, match="no device 'nope'"):
        record(
            _h1(capability=CapabilityChange("nope", "report", "snapshot", "documented", "E-25")),
            doc,
            rig,
        )
    with pytest.raises(WorksheetError, match="no report 'x'"):
        record(
            _h1(capability=CapabilityChange("guitar", "report", "x", "documented", "E-25")),
            doc,
            rig,
        )
    with pytest.raises(WorksheetError, match="not a cue value"):
        bad = CapabilityChange(
            "mvp",
            "command",
            "scene",
            "documented",
            "E-07",
            values={"verse": WireValue("note_on", 1, 1)},
        )
        record(_h1(capability=bad), doc, rig)
    with pytest.raises(WorksheetError, match="unknown test"):
        record(_h1(test_id="Z-9", capability=None), doc, rig)
    with pytest.raises(WorksheetError, match="date"):
        record(_h1(date="2 Nov", capability=None), doc, rig)
    with pytest.raises(WorksheetError, match="pass"):
        record(_h1(outcome="fail"), doc, rig)


def test_result_from_dict_rejects_bad_shapes() -> None:
    good = result_from_dict(
        {
            "test_id": "H-1",
            "date": "2026-11-02",
            "outcome": "pass",
            "notes": "ok",
            "bench_log": "captures/h1.md",
            "capability": {
                "device": "guitar",
                "kind": "report",
                "name": "snapshot",
                "status": "bench_verified",
                "path_checked": "USB",
            },
        }
    )
    assert good.capability is not None and good.capability.name == "snapshot"
    with pytest.raises(WorksheetError, match="outcome"):
        result_from_dict({"test_id": "H-1", "date": "2026-11-02", "outcome": "maybe", "notes": ""})
    with pytest.raises(WorksheetError, match="values"):
        result_from_dict(
            {
                "test_id": "M-1",
                "date": "2026-11-02",
                "outcome": "pass",
                "notes": "",
                "capability": {
                    "device": "mvp",
                    "kind": "command",
                    "name": "scene",
                    "status": "documented",
                    "values": 3,
                },
            }
        )
    with pytest.raises(WorksheetError, match="data1"):
        result_from_dict(
            {
                "test_id": "M-1",
                "date": "2026-11-02",
                "outcome": "pass",
                "notes": "",
                "capability": {
                    "device": "mvp",
                    "kind": "command",
                    "name": "scene",
                    "status": "documented",
                    "values": {"chorus": {"kind": "note_on", "data1": "sixty"}},
                },
            }
        )


def test_the_worksheet_never_touches_midi() -> None:
    """ADR-018: the bench screen guides and records; it never sends MIDI."""
    for path in (ROOT / "src" / "roobotto" / "ui").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in ("mido", "rtmidi", "roobotto.adapters", "midi_wire"):
            assert forbidden not in text, (path.name, forbidden)
    assert worksheet.__doc__
