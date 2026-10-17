from pathlib import Path

import pytest

from roobotto import cli
from roobotto.config import schemas
from roobotto.config.loader import load_config
from roobotto.config.validate import validate_files

ROOT = Path(__file__).parents[2]
EXAMPLES = ROOT / "examples"


def test_examples_validate() -> None:
    report = validate_files([EXAMPLES])
    assert report.issues == []
    assert {type(c).__name__ for c in report.configs.values()} == {"Song", "Rig", "Setlist"}


def test_committed_schemas_match_the_models() -> None:
    assert schemas.stale(ROOT / "schemas") == []


def test_every_schema_is_exported(tmp_path: Path) -> None:
    written = schemas.export(tmp_path)
    assert {p.name for p in written} == {f"{n}.schema.json" for n in schemas.SCHEMAS}
    assert schemas.stale(tmp_path) == []


def test_cli_validate_config_ok(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main(["validate-config", str(EXAMPLES)]) == 0
    assert "OK: 3 file(s) valid" in capsys.readouterr().out


def test_cli_validate_config_reports_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: song\nschema_version: 0\n", encoding="utf-8")
    assert cli.main(["validate-config", str(bad)]) == 1
    err = capsys.readouterr().err
    assert "bad.yaml: id: Field required" in err
    assert "problem(s) found" in err


def test_cli_schemas_export_and_check(tmp_path: Path) -> None:
    assert cli.main(["schemas", "export", "--out", str(tmp_path)]) == 0
    assert cli.main(["schemas", "export", "--check", "--out", str(tmp_path)]) == 0
    (tmp_path / "song.schema.json").write_text("{}\n", encoding="utf-8")
    assert cli.main(["schemas", "export", "--check", "--out", str(tmp_path)]) == 1


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("kind: song\nkind: song\n", "duplicate key 'kind'"),
        ("kind: toast\n", "must be one of rig, setlist, song"),
        ("- a\n- b\n", "must contain a YAML mapping"),
        ("kind: [unclosed\n", "invalid YAML"),
        ("", "must contain a YAML mapping"),
    ],
)
def test_loader_rejects_bad_files(tmp_path: Path, text: str, expected: str) -> None:
    path = tmp_path / "x.yaml"
    path.write_text(text, encoding="utf-8")
    cfg, issues = load_config(path)
    assert cfg is None
    assert any(expected in str(i) for i in issues), issues


def test_loader_reports_a_missing_file(tmp_path: Path) -> None:
    cfg, issues = load_config(tmp_path / "absent.yaml")
    assert cfg is None
    assert "cannot read file" in str(issues[0])


def test_valid_configs_round_trip() -> None:
    report = validate_files([EXAMPLES])
    for cfg in report.configs.values():
        dumped = cfg.model_dump(by_alias=True, mode="json")
        assert type(cfg).model_validate(dumped) == cfg
