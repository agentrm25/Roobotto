"""T-06 (P0): ``devices list`` and ``doctor`` work against fake devices in CI and open nothing."""

import json
import re
import shutil
from pathlib import Path

import pytest
import yaml

from roobotto.cli import main
from roobotto.devices import doctor as doctor_mod
from roobotto.devices import probe
from roobotto.devices.doctor import Check, run_doctor
from roobotto.devices.probe import AudioDevice, FakeSystem, list_devices, ports_template, render

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"


def _fake(**overrides: object) -> FakeSystem:
    base: dict[str, object] = {
        "midi_inputs": ["H4 Port 1", "Helix Stadium", "KeyLab mk3"],
        "midi_outputs": ["H4 Port 1", "H4 Port 2", "Helix Stadium"],
        "audio": [
            AudioDevice(0, "MacBook Pro Microphone", 1, 0, 48000.0, "Core Audio"),
            AudioDevice(1, "Helix Stadium", 8, 8, 48000.0, "Core Audio"),
        ],
        "modules": {"mido": "1.3.3", "rtmidi": "1.5.8", "sounddevice": "0.5.6"},
        "python": (3, 12, 7),
    }
    base.update(overrides)
    return FakeSystem(**base)  # pyright: ignore[reportArgumentType]


def test_list_devices_renders_everything() -> None:
    report = list_devices(_fake())
    text = "\n".join(render(report))
    assert "MIDI inputs" in text and "KeyLab mk3" in text
    assert "MIDI outputs" in text and "H4 Port 2" in text
    assert "Audio devices" in text and "Helix Stadium" in text and "8 in / 8 out" in text
    assert not report.errors


def test_list_devices_reports_a_missing_audio_library_instead_of_crashing() -> None:
    report = list_devices(_fake(modules={"mido": "1.3.3", "rtmidi": "1.5.8", "sounddevice": None}))
    assert report.audio == []
    assert any("sounddevice" in e for e in report.errors)
    assert report.midi_inputs


def test_ports_template_lists_rig_ports_with_system_names_as_hints() -> None:
    text = ports_template(EXAMPLES / "rig.template.yaml", list_devices(_fake()))
    data = yaml.safe_load(text)
    assert data["kind"] == "ports" and data["rig"] == "rig_template"
    assert set(data["ports"]) == {
        "mock_out_mvp",
        "mock_out_aum",
        "mock_out_helix",
        "mock_in_helix",
        "mock_in_switches",
    }
    assert all(v is None for v in data["ports"].values())
    assert "# outputs seen: H4 Port 1, H4 Port 2, Helix Stadium" in text
    assert "# inputs seen: H4 Port 1, Helix Stadium, KeyLab mk3" in text


def _by_id(checks: list[Check]) -> dict[str, Check]:
    return {c.id: c for c in checks}


def test_doctor_on_the_examples_is_healthy() -> None:
    checks = _by_id(run_doctor([EXAMPLES], _fake(), cwd=ROOT))  # no local/ or captures/ here
    assert checks["python"].status == "ok"
    assert checks["midi"].status == "ok"
    assert checks["audio"].status == "ok"
    assert checks["configs"].status == "ok"
    assert checks["profile"].status == "ok" and "mock" in checks["profile"].detail
    assert checks["ports"].status == "ok"  # mock ports need no system names
    assert checks["dirs"].status == "warn" and "mkdir" in checks["dirs"].fix
    assert checks["bench_logs"].status == "ok"
    assert checks["verification_doc"].status == "ok"
    assert all(c.fix for c in checks.values() if c.status != "ok")


def test_doctor_flags_missing_modules_and_old_python(tmp_path: Path) -> None:
    system = _fake(modules={"mido": None, "rtmidi": None, "sounddevice": None}, python=(3, 11, 0))
    checks = _by_id(run_doctor([EXAMPLES], system, cwd=tmp_path))
    assert checks["python"].status == "fail" and "3.12" in checks["python"].fix
    assert checks["midi"].status == "fail" and "uv sync" in checks["midi"].fix
    assert checks["audio"].status == "warn" and "PortAudio" in checks["audio"].fix


def _physical_rig(tmp_path: Path, port_name: str | None) -> Path:
    cfg = tmp_path / "cfg"
    cfg.mkdir(parents=True)
    rig = yaml.safe_load((EXAMPLES / "rig.template.yaml").read_text(encoding="utf-8"))
    rig["profile"] = "physical"
    rig["ports"] = [
        {"id": "h4_out_1", "direction": "out", "transport": "usb"},
        {"id": "mock_in_switches", "direction": "in", "transport": "mock"},
    ]
    rig["devices"] = [
        {
            "id": "mvp",
            "kind": "mvp",
            "owner": "roobotto",
            "receives": [{"port": "h4_out_1", "channel": 3, "behavior": "intended"}],
            "commands": {
                "scene": {
                    "type": "one_shot",
                    "port": "h4_out_1",
                    "channel": 3,
                    "values": {"chorus": {"kind": "note_on", "data1": 60, "data2": 100}},
                    "status": "bench_verified",
                    "evidence": {"bench_log": "captures/2026-11-02-mvp/m1.md"},
                    "min_interval_ms": 100,
                }
            },
        }
    ]
    rig["inputs"] = [
        {"id": "footswitches", "paths": [{"port": "mock_in_switches", "authoritative": True}]}
    ]
    (cfg / "rig.yaml").write_text(yaml.safe_dump(rig, sort_keys=False), encoding="utf-8")
    (cfg / "ports.yaml").write_text(
        yaml.safe_dump(
            {
                "kind": "ports",
                "schema_version": 0,
                "id": "p",
                "rig": "rig_template",
                "ports": {"h4_out_1": port_name},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    return cfg


def test_doctor_checks_physical_ports_and_bench_logs(tmp_path: Path) -> None:
    cfg = _physical_rig(tmp_path, "H4 Port 9")
    checks = _by_id(run_doctor([cfg], _fake(), cwd=tmp_path))
    assert checks["profile"].status == "ok" and "physical" in checks["profile"].detail
    assert checks["ports"].status == "fail"
    assert "H4 Port 9" in checks["ports"].detail and "devices list" in checks["ports"].fix
    assert checks["bench_logs"].status == "fail"
    assert "captures/2026-11-02-mvp/m1.md" in checks["bench_logs"].detail

    (tmp_path / "captures" / "2026-11-02-mvp").mkdir(parents=True)
    (tmp_path / "captures" / "2026-11-02-mvp" / "m1.md").write_text("log", encoding="utf-8")
    (tmp_path / "local").mkdir()
    cfg = _physical_rig(tmp_path / "second", "H4 Port 1")
    checks = _by_id(run_doctor([cfg], _fake(), cwd=tmp_path))
    assert checks["ports"].status == "ok"
    assert checks["bench_logs"].status == "ok"
    assert checks["dirs"].status == "ok"


def test_doctor_without_a_ports_file_tells_you_to_make_one(tmp_path: Path) -> None:
    cfg = _physical_rig(tmp_path, "H4 Port 1")
    (cfg / "ports.yaml").unlink()
    checks = _by_id(run_doctor([cfg], _fake(), cwd=tmp_path))
    assert checks["ports"].status == "fail" and "ports-template" in checks["ports"].fix


def test_doctor_reports_invalid_configs(tmp_path: Path) -> None:
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    (cfg / "rig.yaml").write_text("kind: rig\nschema_version: 0\n", encoding="utf-8")
    checks = _by_id(run_doctor([cfg], _fake(), cwd=tmp_path))
    assert checks["configs"].status == "fail" and "validate-config" in checks["configs"].fix
    assert checks["profile"].status == "skip"


def test_cli_devices_and_doctor(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(probe, "live_system", _fake)
    monkeypatch.chdir(tmp_path)
    shutil.copytree(EXAMPLES, tmp_path / "examples")
    assert main(["devices", "list"]) == 0
    assert "KeyLab mk3" in capsys.readouterr().out
    assert main(["devices", "list", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["midi_outputs"] == ["H4 Port 1", "H4 Port 2", "Helix Stadium"]
    assert main(["devices", "ports-template", "examples/rig.template.yaml"]) == 0
    assert "mock_out_mvp:" in capsys.readouterr().out

    assert main(["doctor"]) == 0  # warnings only
    out = capsys.readouterr().out
    assert "ok" in out and "warn" in out and "mkdir" in out
    assert main(["doctor", "--json"]) == 0
    checks = json.loads(capsys.readouterr().out)
    assert {c["id"] for c in checks} >= {"python", "midi", "audio", "configs", "ports"}

    monkeypatch.setattr(probe, "live_system", lambda: _fake(python=(3, 11, 2)))
    assert main(["doctor"]) == 1


def test_live_module_versions_are_strings() -> None:
    """mido 1.3 exposes a packaging ``Version`` object, not a tuple: read the distribution."""
    system = probe.live_system()
    for name in ("mido", "rtmidi"):
        version = system.module_version(name)
        assert version is not None and re.match(r"^\d+\.\d+", version), (name, version)
    assert system.module_version("no_such_module_xyz") is None
    assert system.python_version()[:2] >= (3, 12)


def test_live_probe_opens_nothing_and_degrades_gracefully() -> None:
    """The live probe lists names only. What it can see depends on the machine, so it must
    report rather than raise when ALSA, CoreAudio or PortAudio are missing."""
    report = list_devices(probe.live_system())
    assert isinstance(report.midi_inputs, list) and isinstance(report.audio, list)
    for error in report.errors:
        assert "could not be listed" in error
    for path in (ROOT / "src" / "roobotto" / "devices").glob("*.py"):
        text = path.read_text(encoding="utf-8")
        for forbidden in (
            "open_input",
            "open_output",
            "InputStream",
            "OutputStream",
            "rec(",
            "play(",
        ):
            assert forbidden not in text, (path.name, forbidden)
    assert doctor_mod.__doc__ and probe.__doc__
