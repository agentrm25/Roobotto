"""T-06: ``local/ports.yaml`` maps rig port ids to the operating system's port names."""

from pathlib import Path

import yaml

from roobotto.config import schemas
from roobotto.config.loader import load_config
from roobotto.config.ports import Ports
from roobotto.config.rig import Rig
from roobotto.config.validate import validate_files

EXAMPLES = Path(__file__).parents[2] / "examples"


def _write(tmp_path: Path, name: str, data: object) -> Path:
    path = tmp_path / name
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


def test_template_loads_and_names_every_rig_port() -> None:
    model, issues = load_config(EXAMPLES / "ports.template.yaml")
    assert not issues
    assert isinstance(model, Ports)
    rig, _ = load_config(EXAMPLES / "rig.template.yaml")
    assert rig is not None
    assert isinstance(rig, Rig)
    assert set(model.ports) == {p.id for p in rig.ports}
    assert model.rig == rig.id


def test_schema_is_exported(tmp_path: Path) -> None:
    assert "ports" in schemas.SCHEMAS
    written = schemas.export(tmp_path)
    assert tmp_path / "ports.schema.json" in written


def test_unknown_rig_port_is_rejected(tmp_path: Path) -> None:
    rig = yaml.safe_load((EXAMPLES / "rig.template.yaml").read_text(encoding="utf-8"))
    _write(tmp_path, "rig.yaml", rig)
    _write(
        tmp_path,
        "ports.yaml",
        {
            "kind": "ports",
            "schema_version": 0,
            "id": "p",
            "rig": "rig_template",
            "ports": {"nope": "IAC Bus 1"},
        },
    )
    report = validate_files([tmp_path])
    assert any("nope" in str(i) and "not a port of rig" in str(i) for i in report.issues)


def test_physical_port_without_a_name_is_rejected(tmp_path: Path) -> None:
    rig = yaml.safe_load((EXAMPLES / "rig.template.yaml").read_text(encoding="utf-8"))
    rig["profile"] = "physical"
    rig["ports"] = [
        {"id": "usb_out", "direction": "out", "transport": "usb"},
        {"id": "mock_in_switches", "direction": "in", "transport": "mock"},
    ]
    rig["devices"] = []
    rig["inputs"] = [
        {"id": "footswitches", "paths": [{"port": "mock_in_switches", "authoritative": True}]}
    ]
    _write(tmp_path, "rig.yaml", rig)
    _write(
        tmp_path,
        "ports.yaml",
        {
            "kind": "ports",
            "schema_version": 0,
            "id": "p",
            "rig": "rig_template",
            "ports": {"usb_out": None},
        },
    )
    report = validate_files([tmp_path])
    assert any("usb_out" in str(i) and "physical" in str(i) for i in report.issues)
    # A mock port never needs a name, and a named physical port is fine.
    _write(
        tmp_path,
        "ports.yaml",
        {
            "kind": "ports",
            "schema_version": 0,
            "id": "p",
            "rig": "rig_template",
            "ports": {"usb_out": "H4 Port 1"},
        },
    )
    assert validate_files([tmp_path]).ok


def test_ports_for_another_rig_is_ignored_but_still_checked_alone(tmp_path: Path) -> None:
    rig = yaml.safe_load((EXAMPLES / "rig.template.yaml").read_text(encoding="utf-8"))
    _write(tmp_path, "rig.yaml", rig)
    _write(
        tmp_path,
        "ports.yaml",
        {
            "kind": "ports",
            "schema_version": 0,
            "id": "p",
            "rig": "other_rig",
            "ports": {"nope": "x"},
        },
    )
    report = validate_files([tmp_path])
    assert any("other_rig" in str(i) for i in report.issues)


def test_arming_keys_are_rejected(tmp_path: Path) -> None:
    path = _write(
        tmp_path,
        "ports.yaml",
        {
            "kind": "ports",
            "schema_version": 0,
            "id": "p",
            "rig": "r",
            "ports": {},
            "armed": True,
        },
    )
    _, issues = load_config(path)
    assert issues
    assert "armed" in str(issues[0]).lower() or "extra" in str(issues[0]).lower()
