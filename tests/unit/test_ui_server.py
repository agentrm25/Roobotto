"""T-16: ``roobotto ui`` serves the workbench on 127.0.0.1 only, and its buttons call the same
functions as the CLI."""

import json
import re
import shutil
import socket
import threading
from collections.abc import Iterator
from http.client import HTTPConnection
from pathlib import Path
from typing import Any

import pytest

from roobotto.cli import build_parser, main
from roobotto.devices.probe import AudioDevice, FakeSystem
from roobotto.ui.server import Workbench, make_server

ROOT = Path(__file__).parents[2]
DOC = ROOT / "docs" / "hardware-verification.md"
RIG = ROOT / "examples" / "rig.template.yaml"


@pytest.fixture
def paths(tmp_path: Path) -> tuple[Path, Path]:
    doc, rig = tmp_path / "hv.md", tmp_path / "rig.yaml"
    shutil.copy(DOC, doc)
    shutil.copy(RIG, rig)
    return doc, rig


@pytest.fixture
def server(paths: tuple[Path, Path]) -> Iterator[Workbench]:
    doc, rig = paths
    system = FakeSystem(
        midi_inputs=["KeyLab mk3"],
        midi_outputs=["H4 Port 1"],
        audio=[AudioDevice(0, "Helix Stadium", 8, 8, 48000.0, "Core Audio")],
        modules={"mido": "1.3.3", "rtmidi": "1.5.8", "sounddevice": "0.5.6"},
        python=(3, 12, 7),
    )
    srv = make_server(
        doc=doc, rig=rig, port=0, config_paths=[ROOT / "examples"], system=system, cwd=ROOT
    )
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    try:
        yield srv
    finally:
        srv.shutdown()
        srv.server_close()
        thread.join(timeout=5)


def _request(
    srv: Workbench, method: str, path: str, body: Any = None, host: str | None = None
) -> tuple[int, Any]:
    conn = HTTPConnection("127.0.0.1", srv.server_address[1], timeout=5)
    headers = {"Host": host or f"127.0.0.1:{srv.server_address[1]}"}
    data = None
    if body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    conn.request(method, path, body=data, headers=headers)
    resp = conn.getresponse()
    raw = resp.read()
    conn.close()
    ctype = resp.getheader("Content-Type", "")
    return resp.status, json.loads(raw) if ctype.startswith("application/json") else raw


def test_binds_to_loopback_only(server: Workbench) -> None:
    assert server.server_address[0] == "127.0.0.1"
    assert server.socket.family == socket.AF_INET
    assert server.socket.getsockname()[0] == "127.0.0.1"


def test_refuses_any_other_bind_address(paths: tuple[Path, Path]) -> None:
    doc, rig = paths
    for host in ("0.0.0.0", "", "192.168.1.5", "::"):
        with pytest.raises(ValueError, match=re.escape("127.0.0.1")):
            make_server(doc=doc, rig=rig, port=0, host=host)


def test_serves_the_page_and_the_worksheet(server: Workbench) -> None:
    status, page = _request(server, "GET", "/")
    assert status == 200
    assert isinstance(page, bytes)
    assert b"<!doctype html>" in page.lower()
    assert b"Bench worksheet" in page

    status, data = _request(server, "GET", "/api/worksheet")
    assert status == 200
    assert [t["id"] for t in data["tests"]][:3] == ["H-1", "H-2", "H-3"]
    assert data["records"]["mvp.scene"]["status"] == "unknown"
    assert data["records"]["guitar.reports.snapshot"]["kind"] == "report"
    assert data["records"]["mvp.scene"]["values"] == ["chorus", "solo_build"]

    assert _request(server, "GET", "/nope")[0] == 404
    assert _request(server, "GET", "/api/worksheet", host="evil.example:80")[0] == 403


def test_posting_a_result_records_it(server: Workbench, paths: tuple[Path, Path]) -> None:
    doc, rig = paths
    body = {
        "test_id": "H-1",
        "date": "2026-11-02",
        "outcome": "pass",
        "notes": "clean",
        "bench_log": "captures/h1.md",
        "capability": {
            "device": "guitar",
            "kind": "report",
            "name": "snapshot",
            "status": "bench_verified",
            "path_checked": "USB",
        },
    }
    status, data = _request(server, "POST", "/api/results", body)
    assert status == 200, data
    assert data["written"] == [str(doc), str(rig)]
    assert "status: bench_verified" in rig.read_text(encoding="utf-8")
    assert "| H-1 | 2026-11-02 | pass | captures/h1.md | clean |" in doc.read_text(encoding="utf-8")

    status, data = _request(server, "GET", "/api/worksheet")
    assert data["records"]["guitar.reports.snapshot"]["status"] == "bench_verified"
    assert data["results"] == [{"test_id": "H-1", "date": "2026-11-02", "outcome": "pass"}]


def test_bad_posts_change_nothing(server: Workbench, paths: tuple[Path, Path]) -> None:
    doc, rig = paths
    before = (doc.read_bytes(), rig.read_bytes())
    body = {
        "test_id": "H-1",
        "date": "2026-11-02",
        "outcome": "pass",
        "notes": "",
        "capability": {
            "device": "guitar",
            "kind": "report",
            "name": "snapshot",
            "status": "bench_verified",
        },
    }
    status, data = _request(server, "POST", "/api/results", body)
    assert status == 400 and "bench_log" in data["error"]
    conn = HTTPConnection("127.0.0.1", server.server_address[1], timeout=5)
    conn.request(
        "POST", "/api/results", body=b"{not json", headers={"Content-Type": "application/json"}
    )
    assert conn.getresponse().status == 400
    conn.close()
    assert _request(server, "POST", "/api/results", body, host="evil.example")[0] == 403
    assert _request(server, "POST", "/api/other", body)[0] == 404
    assert (doc.read_bytes(), rig.read_bytes()) == before


def test_doctor_and_devices_endpoints(server: Workbench) -> None:
    status, page = _request(server, "GET", "/")
    assert status == 200 and isinstance(page, bytes) and b"Doctor" in page

    status, data = _request(server, "GET", "/api/devices")
    assert status == 200
    assert data["midi_inputs"] == ["KeyLab mk3"] and data["audio"][0]["name"] == "Helix Stadium"

    status, data = _request(server, "GET", "/api/doctor")
    assert status == 200
    by_id = {c["id"]: c for c in data["checks"]}
    assert by_id["configs"]["status"] == "ok" and by_id["ports"]["status"] == "ok"
    assert by_id["dirs"]["status"] == "warn" and "mkdir" in by_id["dirs"]["fix"]
    assert data["ready"] is True
    assert data["paths"] == [str(ROOT / "examples")]

    status, text = _request(server, "GET", "/api/ports-template")
    assert status == 200 and isinstance(text, bytes)
    assert b"rig: rig_template" in text and b"# outputs seen: H4 Port 1" in text
    assert _request(server, "GET", "/api/doctor", host="evil.example")[0] == 403


def test_cli_ui_defaults_and_flags() -> None:
    args = build_parser().parse_args(["ui", "--no-browser", "--port", "0"])
    assert args.port == 0 and args.no_browser is True
    assert args.func.__name__ == "_ui"


def test_cli_bench_commands(paths: tuple[Path, Path], capsys: pytest.CaptureFixture[str]) -> None:
    doc, rig = paths
    assert main(["bench", "list"]) == 0
    out = capsys.readouterr().out
    assert out.splitlines()[0].startswith("H-1 ")
    assert len([line for line in out.splitlines() if line]) == 14

    assert main(["bench", "show", "M-1"]) == 0
    assert "KeyLab" in capsys.readouterr().out
    assert main(["bench", "show", "Z-1"]) == 1

    assert (
        main(
            [
                "bench",
                "record",
                "M-1",
                "--date",
                "2026-11-02",
                "--outcome",
                "pass",
                "--notes",
                "note map found",
                "--log",
                "captures/m1.md",
                "--record",
                "mvp.scene",
                "--status",
                "bench_verified",
                "--value",
                "chorus=note_on:60:100",
                "--value",
                "solo_build=note_on:62:100",
                "--recipients",
                "none",
                "--side-effects",
                "none",
                "--doc",
                str(doc),
                "--rig",
                str(rig),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "wrote" in out
    assert "data1: 60" in rig.read_text(encoding="utf-8")

    assert (
        main(
            [
                "bench",
                "record",
                "H-1",
                "--outcome",
                "pass",
                "--notes",
                "x",
                "--record",
                "guitar.reports.snapshot",
                "--status",
                "bench_verified",
                "--doc",
                str(doc),
                "--rig",
                str(rig),
            ]
        )
        == 1
    )
    assert "bench_log" in capsys.readouterr().err
    # A note_on needs data2: the rig model says so, and the worksheet passes that message on.
    assert (
        main(
            [
                "bench",
                "record",
                "M-1",
                "--outcome",
                "pass",
                "--notes",
                "x",
                "--record",
                "mvp.scene",
                "--status",
                "documented",
                "--source",
                "E-07",
                "--value",
                "chorus=note_on:60",
                "--value",
                "solo_build=note_on:62:100",
                "--doc",
                str(doc),
                "--rig",
                str(rig),
            ]
        )
        == 1
    )
    assert "data2" in capsys.readouterr().err
    assert (
        main(
            [
                "bench",
                "record",
                "M-1",
                "--outcome",
                "pass",
                "--value",
                "chorus=bad",
                "--doc",
                str(doc),
                "--rig",
                str(rig),
            ]
        )
        == 1
    )
    assert "--record" in capsys.readouterr().err
