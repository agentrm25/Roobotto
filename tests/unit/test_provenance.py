"""Provenance classification (ADR-011, Section 5.4): table-driven, fail closed."""

from typing import Any

import pytest

from roobotto.engine.inputs import MidiIn
from roobotto.engine.provenance import ProvenanceContext, SentWire, classify
from spine_support import MS, rig

NOW = 10_000 * MS
RIG = rig()


def ctx(**over: Any) -> ProvenanceContext:
    base: dict[str, Any] = {
        "rig": RIG,
        "now_ns": NOW,
        "init_window_ns": 500 * MS,
        "echo_window_ns": 250 * MS,
        "last_program_ns": {},
        "recent_sends": (),
        "mapped_targets": {2: "chorus"},
    }
    return ProvenanceContext(**(base | over))


def msg(
    port: str, kind: str, channel: int, data1: int, data2: int | None = None, **over: Any
) -> MidiIn:
    return MidiIn.model_validate(
        {
            "t_ns": NOW,
            "port": port,
            "kind": kind,
            "channel": channel,
            "data1": data1,
            "data2": data2,
        }
        | over
    )


GO_PRESS = ("mock_in_switches", "note_on", 16, 60, 127)
SNAP2 = ("mock_in_helix", "control_change", 1, 69, 1)

CASES: list[tuple[str, tuple[Any, ...], dict[str, Any], dict[str, Any], str, str | None]] = [
    ("GO press", GO_PRESS, {}, {}, "performer_intent", "go"),
    (
        "STOP press",
        ("mock_in_switches", "note_on", 16, 62, 127),
        {},
        {},
        "performer_intent",
        "stop",
    ),
    ("GO release", ("mock_in_switches", "note_off", 16, 60, 0), {}, {}, "performer_intent", None),
    (
        "note_on velocity 0 is a release",
        ("mock_in_switches", "note_on", 16, 60, 0),
        {},
        {},
        "performer_intent",
        None,
    ),
    (
        "BACK tap switch press waits for T-05",
        ("mock_in_switches", "note_on", 16, 61, 127),
        {},
        {},
        "performer_intent",
        None,
    ),
    (
        "GO on the duplicate path",
        ("mock_in_switches_h4", "note_on", 16, 60, 127),
        {},
        {},
        "state_observation",
        None,
    ),
    ("unknown port", ("mock_out_mvp", "note_on", 16, 60, 127), {}, {}, "unknown", None),
    (
        "unmapped note on the switch port",
        ("mock_in_switches", "note_on", 16, 99, 127),
        {},
        {},
        "unknown",
        None,
    ),
    ("mapped snapshot", SNAP2, {}, {}, "performer_intent", "go"),
    (
        "unmapped snapshot",
        ("mock_in_helix", "control_change", 1, 69, 3),
        {},
        {},
        "state_observation",
        None,
    ),
    (
        "snapshot on the duplicate path",
        ("mock_in_helix_h4", "control_change", 1, 69, 1),
        {},
        {},
        "state_observation",
        None,
    ),
    (
        "preset-load recall",
        SNAP2,
        {},
        {"last_program_ns": {"helix": NOW - 400 * MS}},
        "state_observation",
        None,
    ),
    (
        "snapshot after the init window",
        SNAP2,
        {},
        {"last_program_ns": {"helix": NOW - 600 * MS}},
        "performer_intent",
        "go",
    ),
    ("automated recall", SNAP2, {"automated": True}, {}, "state_observation", None),
    (
        "echo",
        SNAP2,
        {},
        {"recent_sends": (SentWire(NOW - 100 * MS, "guitar", "control_change", 1, 69, 1),)},
        "output_echo",
        None,
    ),
    (
        "same bytes sent to a device that does not report here is no echo",
        SNAP2,
        {},
        {"recent_sends": (SentWire(NOW - 100 * MS, "mvp", "control_change", 1, 69, 1),)},
        "performer_intent",
        "go",
    ),
    (
        "old send is no echo",
        SNAP2,
        {},
        {"recent_sends": (SentWire(NOW - 300 * MS, "guitar", "control_change", 1, 69, 1),)},
        "performer_intent",
        "go",
    ),
    (
        "snapshot CC value 8 (next) is no report",
        ("mock_in_helix", "control_change", 1, 69, 8),
        {},
        {},
        "unknown",
        None,
    ),
    (
        "program change",
        ("mock_in_helix", "program_change", 1, 0),
        {},
        {},
        "state_observation",
        None,
    ),
    (
        "Helix CC with no report record",
        ("mock_in_helix", "control_change", 1, 1, 0),
        {},
        {},
        "unknown",
        None,
    ),
    (
        "snapshot CC on another channel",
        ("mock_in_helix", "control_change", 2, 69, 1),
        {},
        {},
        "unknown",
        None,
    ),
]


@pytest.mark.parametrize(
    ("name", "raw", "over", "context", "provenance", "action"), CASES, ids=[c[0] for c in CASES]
)
def test_classify(
    name: str,
    raw: tuple[Any, ...],
    over: dict[str, Any],
    context: dict[str, Any],
    provenance: str,
    action: str | None,
) -> None:
    result = classify(msg(*raw, **over), "e1", ctx(**context))
    assert (result.event.provenance, result.event.mapped_action) == (provenance, action), (
        result.reason
    )
    if provenance != "performer_intent":
        assert result.event.mapped_action is None


def test_mapped_snapshot_names_its_target() -> None:
    result = classify(msg(*SNAP2), "e1", ctx())
    assert (result.snapshot, result.target, result.detail) == (2, "chorus", "snapshot_mapped")


def test_port_shared_by_two_sources_fails_closed() -> None:
    doc = RIG.model_dump(by_alias=True, mode="json")
    doc["inputs"][1]["paths"].append({"port": "mock_in_helix", "authoritative": False})
    from roobotto.config.rig import Rig

    shared = Rig.model_validate(doc)
    assert classify(msg(*SNAP2), "e1", ctx(rig=shared)).event.provenance == "unknown"


def _with_physical_helix_input(status: str):
    """The Helix input on a USB port, with the snapshot report at ``status``. Built without
    validation: only the classifier is under test."""
    from roobotto.config.rig import Evidence

    ports = [
        p.model_copy(update={"transport": "usb"}) if p.id == "mock_in_helix" else p
        for p in RIG.ports
    ]
    guitar = RIG.device("guitar")
    assert guitar is not None
    evidence = Evidence(bench_log="captures/test-fixture/not-a-real-bench-log.txt")
    report = guitar.reports["snapshot"].model_copy(update={"status": status, "evidence": evidence})
    devices = [
        d.model_copy(update={"reports": {**d.reports, "snapshot": report}})
        if d.id == "guitar"
        else d
        for d in RIG.devices
    ]
    return RIG.model_copy(update={"ports": ports, "devices": devices})


@pytest.mark.parametrize(
    ("status", "provenance", "detail"),
    [
        ("documented", "state_observation", "snapshot_unverified"),
        ("bench_verified", "performer_intent", "snapshot_mapped"),
    ],
)
def test_only_a_bench_verified_report_authorizes_on_a_physical_port(
    status: str, provenance: str, detail: str
) -> None:
    result = classify(msg(*SNAP2), "e1", ctx(rig=_with_physical_helix_input(status)))
    assert (result.event.provenance, result.detail) == (provenance, detail), result.reason


def test_two_snapshot_reports_make_mapping_ambiguous() -> None:
    guitar = RIG.device("guitar")
    assert guitar is not None
    second = guitar.reports["snapshot"].model_copy(update={"channel": 2})
    devices = [
        d.model_copy(update={"reports": {**d.reports, "snapshot_b": second}})
        if d.id == "guitar"
        else d
        for d in RIG.devices
    ]
    rig2 = RIG.model_copy(update={"devices": devices})
    result = classify(msg(*SNAP2), "e1", ctx(rig=rig2))
    assert result.detail == "snapshot_unmapped"


def test_report_records_name_the_device() -> None:
    assert classify(msg(*SNAP2), "e1", ctx()).device == "guitar"
    assert classify(msg("mock_in_helix", "program_change", 1, 0), "e1", ctx()).device == "guitar"
