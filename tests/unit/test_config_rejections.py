"""T-02 acceptance: at least 25 configs that must be rejected, each with a clear message."""

from collections.abc import Callable
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
import yaml

from roobotto.config.validate import validate_files

EXAMPLES = Path(__file__).parents[2] / "examples"
Doc = dict[str, Any]
Docs = dict[str, Doc]


def _load(relative: str) -> Doc:
    data: Doc = yaml.safe_load((EXAMPLES / relative).read_text(encoding="utf-8"))
    return data


BASE: Docs = {
    "song": _load("songs/synthetic_anthem.yaml"),
    "rig": _load("rig.template.yaml"),
    "setlist": _load("setlist.yaml"),
}


def _by_id(items: list[Doc], item_id: str, key: str = "id") -> Doc:
    return next(i for i in items if i[key] == item_id)


def section(d: Docs, section_id: str) -> Doc:
    return _by_id(d["song"]["sections"], section_id)


def cue(d: Docs, cue_id: str) -> Doc:
    return _by_id(d["song"]["cues"], cue_id)


def device(d: Docs, device_id: str) -> Doc:
    return _by_id(d["rig"]["devices"], device_id)


def messages(tmp_path: Path, docs: Docs, song_in_local: bool = False) -> list[str]:
    folder = tmp_path / "local" if song_in_local else tmp_path
    folder.mkdir(exist_ok=True)
    paths = {
        "song": folder / "song.yaml",
        "rig": tmp_path / "rig.yaml",
        "setlist": tmp_path / "setlist.yaml",
    }
    for name, path in paths.items():
        path.write_text(yaml.safe_dump(docs[name], sort_keys=False), encoding="utf-8")
    return [str(i) for i in validate_files(paths.values()).issues]


def test_base_documents_are_valid(tmp_path: Path) -> None:
    assert messages(tmp_path, deepcopy(BASE)) == []


Mutate = Callable[[Docs], None]


def _set(path: list[str | int], value: object) -> Mutate:
    def apply(d: Docs) -> None:
        target: Any = d
        for part in path[:-1]:
            target = target[part]
        target[path[-1]] = value

    return apply


def _all(*mutations: Mutate) -> Mutate:
    def apply(d: Docs) -> None:
        for mutation in mutations:
            mutation(d)

    return apply


def _remove_behavior(d: Docs) -> None:
    del device(d, "aum")["receives"][0]["behavior"]


def _two_devices_one_channel(behavior: str) -> Mutate:
    """Put the MVP and AUM on one port and channel; AUM declares the given behavior."""

    def apply(d: Docs) -> None:
        mvp, aum = device(d, "mvp"), device(d, "aum")
        mvp["receives"] = [{"port": "mock_out_mvp", "channel": 3, "behavior": "intended"}]
        aum["receives"] = [
            {"port": "mock_out_aum", "channel": 15, "behavior": "intended"},
            {"port": "mock_out_mvp", "channel": 3, "behavior": behavior},
        ]

    return apply


def _mix_go_with_back(d: Docs) -> None:
    d["rig"]["controls"].append({"switch": "go_switch", "gesture": "tap", "action": "back"})
    d["rig"]["controls"] = [
        c
        for c in d["rig"]["controls"]
        if not (c["switch"] == "back_switch" and c["action"] == "back")
    ]


def _drop_stop(d: Docs) -> None:
    d["rig"]["controls"] = [c for c in d["rig"]["controls"] if c["action"] != "stop"]


def _second_authoritative_path(d: Docs) -> None:
    d["rig"]["ports"].append({"id": "mock_in_extra", "direction": "in", "transport": "mock"})
    _by_id(d["rig"]["inputs"], "helix")["paths"].append(
        {"port": "mock_in_extra", "authoritative": True}
    )


def _physical_unverified(d: Docs) -> None:
    d["rig"]["profile"] = "physical"


def _physical_port(d: Docs) -> None:
    d["rig"]["ports"][0]["transport"] = "usb"


def _verified(log: str | None) -> Mutate:
    def apply(d: Docs) -> None:
        cmd = device(d, "mvp")["commands"]["scene"]
        cmd["status"] = "bench_verified"
        cmd["values"] = {
            "chorus": {"kind": "note_on", "data1": 60, "data2": 100},
            "solo_build": {"kind": "note_on", "data1": 61, "data2": 100},
        }
        if log is not None:
            cmd["evidence"] = {"bench_log": log}

    return apply


def _documented_without_source(d: Docs) -> None:
    cmd = device(d, "mvp")["commands"]["scene"]
    cmd["status"] = "documented"
    cmd["values"] = {
        "chorus": {"kind": "note_on", "data1": 60, "data2": 100},
        "solo_build": {"kind": "note_on", "data1": 61, "data2": 100},
    }


def _command_not_in_receives(d: Docs) -> None:
    device(d, "mvp")["commands"]["scene"]["channel"] = 4


def _meter(value: str) -> Mutate:
    return _set(["song", "meter"], value)


def _section_cycle(d: Docs) -> None:
    section(d, "verse1")["model_as"] = "verse2"


def _advance_ambiguity(d: Docs) -> None:
    section(d, "chorus3")["advance_on"] = {"snapshot": 4}


def _critical_without_performer(d: Docs) -> None:
    cue(d, "solo_guitar_lead")["requires"]["corroborate_by"] = ["drum_pattern"]


REJECTED: list[tuple[str, Mutate, str]] = [
    # Song structure
    (
        "cue at unknown section",
        _set(["song", "cues", 0, "at", "section"], "bridge"),
        "unknown section 'bridge'",
    ),
    (
        "cue past end of section",
        _set(["song", "cues", 0, "at", "bar"], 9),
        "past the end of section 'chorus1'",
    ),
    ("cue beat outside meter", _set(["song", "cues", 0, "at", "beat"], 5), "outside the 4/4 bar"),
    (
        "cue offset past end",
        _set(["song", "cues", 1, "at", "offset_beats"], 40),
        "past the end of section 'solo'",
    ),
    ("broken graph edge", _set(["song", "graph", 0, "to"], "nowhere"), "unknown section 'nowhere'"),
    (
        "edge from ending section",
        lambda d: d["song"]["graph"].append({"from": "outro", "to": "intro", "prob": 0.1}),
        "has no successors",
    ),
    (
        "self-loop without extend or repeat",
        lambda d: d["song"]["graph"].append({"from": "intro", "to": "intro", "prob": 0.1}),
        "self-loop needs",
    ),
    (
        "edge probabilities above one",
        lambda d: d["song"]["graph"].append({"from": "solo", "to": "chorus3", "prob": 0.8}),
        "summing to",
    ),
    (
        "duplicate edge",
        lambda d: d["song"]["graph"].append(dict(d["song"]["graph"][0])),
        "duplicate edge",
    ),
    (
        "duplicate section id",
        lambda d: d["song"]["sections"].insert(1, {"id": "intro", "bars": 2}),
        "duplicate section id 'intro'",
    ),
    (
        "model_as unknown section",
        _set(["song", "sections", 3, "model_as"], "bridge"),
        "unknown section 'bridge'",
    ),
    ("model_as cycle", _section_cycle, "model_as chain contains a cycle"),
    (
        "last section does not end",
        _set(["song", "sections", 7, "ends"], False),
        "must set ends: true",
    ),
    ("tempo nominal outside range", _set(["song", "tempo", "nominal"], 200), "outside range"),
    ("malformed meter", _meter("four-four"), "must look like 4/4"),
    ("ambiguous advance_on snapshot", _advance_ambiguity, "ambiguous after 'chorus3'"),
    (
        "advance_on on first section",
        _set(["song", "sections", 0, "advance_on"], {"snapshot": 1}),
        "first section is entered by song selection",
    ),
    (
        "lyric_anchor on non-vocal section",
        _set(["song", "sections", 0, "lyric_anchor"], "la la"),
        "lyric_anchor requires vocal",
    ),
    (
        "lyric anchor outside local/",
        _set(["song", "sections", 1, "lyric_anchor"], "la la"),
        "only appear under local/",
    ),
    (
        "duplicate cue id",
        _set(["song", "cues", 1, "id"], "chorus1_scene"),
        "duplicate cue id 'chorus1_scene'",
    ),
    (
        "critical cue without performer intent",
        _critical_without_performer,
        "tracker evidence alone never fires a critical cue",
    ),
    ("excluded cue class", _set(["song", "cues", 1, "class"], "excluded"), "never automated"),
    ("cue with empty state", _set(["song", "cues", 1, "state"], {}), "at least 1 item"),
    (
        "YAML 'on' read as a boolean",
        _set(["song", "cues", 1, "state"], {"mvp.scene": True}),
        "valid string",
    ),
    ("unknown key in song", _set(["song", "colour"], "red"), "Extra inputs are not permitted"),
    # Rig
    (
        "channel above 16",
        _set(["rig", "devices", 0, "commands", "scene", "channel"], 17),
        "less than or equal to 16",
    ),
    (
        "channel zero (0-15 is wire format only)",
        _set(["rig", "devices", 0, "receives", 0, "channel"], 0),
        "greater than or equal to 1",
    ),
    ("physical port in a mock profile", _physical_port, "may only use mock ports"),
    (
        "unverified command in a physical profile",
        _all(_physical_port, _physical_unverified),
        "only bench_verified commands",
    ),
    ("bench_verified without a bench log", _verified(None), "needs evidence.bench_log"),
    (
        "bench log outside captures/",
        _verified("notes/log.txt"),
        "must be a file path under captures/",
    ),
    (
        "bench log escaping captures/",
        _verified("captures/../x.txt"),
        "must be a file path under captures/",
    ),
    ("documented without a source", _documented_without_source, "needs evidence.source"),
    ("recipient with no declared behavior", _remove_behavior, "behavior"),
    (
        "recipient that acts on the channel (I-13)",
        _two_devices_one_channel("acts"),
        "would also reach 'aum'",
    ),
    (
        "recipient with unknown behavior (I-13)",
        _two_devices_one_channel("unknown"),
        "would also reach 'aum'",
    ),
    (
        "command port/channel not in receives",
        _command_not_in_receives,
        "must list this port/channel in receives",
    ),
    (
        "forward and backward action on one switch (I-17)",
        _mix_go_with_back,
        "no switch may combine a forward action",
    ),
    ("STOP as a long press", _set(["rig", "controls", 3, "gesture"], "hold"), "never a long press"),
    (
        "GO as a tap",
        _set(["rig", "controls", 0, "gesture"], "tap"),
        "never inferred from a multi-gesture switch",
    ),
    ("no STOP control", _drop_stop, "needs a 'stop' control"),
    (
        "source with two authoritative paths",
        _second_authoritative_path,
        "has 2 authoritative paths",
    ),
    (
        "source with no authoritative path",
        _set(["rig", "inputs", 0, "paths", 0, "authoritative"], False),
        "has 0 authoritative paths",
    ),
    ("arming in config (I-2)", _set(["rig", "armed"], True), "never configured"),
    ("override in config (I-1)", _set(["rig", "override"], True), "never configured"),
    (
        "unknown port",
        _set(["rig", "devices", 0, "receives", 0, "port"], "nope"),
        "unknown port 'nope'",
    ),
    (
        "duplicate device id",
        lambda d: d["rig"]["devices"].append(deepcopy(d["rig"]["devices"][0])),
        "duplicate device id 'mvp'",
    ),
    (
        "hold shorter than tap",
        _set(["rig", "timing"], {"tap_max_ms": 600, "hold_ms": 500}),
        "never both a tap and a hold",
    ),
    (
        "send-only port used for input",
        _set(["rig", "inputs", 0, "paths", 0, "port"], "mock_out_mvp"),
        "needs one of",
    ),
    # Setlist
    (
        "duplicate program change",
        lambda d: d["setlist"]["entries"].append({"song": "synthetic_anthem", "program_change": 0}),
        "selects more than one setlist entry",
    ),
    (
        "setlist with unknown song",
        _set(["setlist", "entries", 0, "song"], "missing_song"),
        "unknown song 'missing_song'",
    ),
    # Cues against the rig
    (
        "cue for an unknown device",
        _set(["song", "cues", 0, "state"], {"lights.scene": "chorus"}),
        "unknown device 'lights'",
    ),
    (
        "cue for a performer-owned device",
        _set(["rig", "devices", 0, "owner"], "performer"),
        "owned by the performer",
    ),
    (
        "cue for an unknown command",
        _set(["song", "cues", 0, "state"], {"mvp.strobe": "chorus"}),
        "has no command 'strobe'",
    ),
    (
        "cue with an unknown value",
        _set(["song", "cues", 0, "state"], {"mvp.scene": "bridge"}),
        "not a value of mvp.scene",
    ),
]


def test_at_least_25_rejections() -> None:
    assert len(REJECTED) >= 25


@pytest.mark.parametrize(("name", "mutate", "expected"), REJECTED, ids=[r[0] for r in REJECTED])
def test_rejected(tmp_path: Path, name: str, mutate: Mutate, expected: str) -> None:
    docs = deepcopy(BASE)
    mutate(docs)
    found = messages(tmp_path, docs)
    assert found, f"{name}: the config was accepted"
    assert any(expected in line for line in found), f"{name}: expected {expected!r} in {found}"


def test_lyric_anchor_is_allowed_under_local(tmp_path: Path) -> None:
    docs = deepcopy(BASE)
    section(docs, "verse1")["lyric_anchor"] = "la la"
    assert messages(tmp_path, docs, song_in_local=True) == []


def test_rejection_messages_name_the_file_and_location(tmp_path: Path) -> None:
    docs = deepcopy(BASE)
    docs["song"]["sections"][2]["bars"] = 0
    found = messages(tmp_path, docs)
    assert any("song.yaml: sections[2].bars" in line for line in found)
