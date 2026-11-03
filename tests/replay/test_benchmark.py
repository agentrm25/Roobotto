"""T-13 acceptance: one command regenerates the report from a protocol file plus local
recordings, with the method x feed-subset matrix, fault injections, assistance accounting, a
reduced-scope section, the idealized label rule and the commit, config and manifest hashes."""

import csv
import json
from pathlib import Path

import pytest
import yaml

from roobotto.cli import main
from roobotto.config import schemas
from roobotto.eval.benchmark import Protocol, load_protocol, run_benchmark

ROOT = Path(__file__).parents[2]
RATE = 8000
BLOCK = 256


def _protocol(tmp_path: Path, **overrides: object) -> Path:
    data: dict[str, object] = {
        "kind": "protocol",
        "schema_version": 0,
        "id": "test_pilot",
        "title": "Test pilot",
        "label": "synthetic",
        "corpus": {
            "root": str(tmp_path / "recordings"),
            "synthesize": {"songs": str(ROOT / "examples" / "songs"), "sample_rate": RATE},
            "songs": ["synthetic_anthem"],
            "takes": ["nominal", "drift", "skip"],
        },
        "reference": "nominal",
        "block": BLOCK,
        "methods": ["B0", "B1", "B3"],
        "feed_subsets": {"B3": [["chords", "bass"], ["chords"]]},
        "fault_injections": [{"id": "drums_lost_20_30", "drop": {"drums": [[20.0, 30.0]]}}],
        "out": str(tmp_path / "report"),
    }
    data.update(overrides)
    path = tmp_path / "protocol.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
    return path


@pytest.fixture(scope="module")
def run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path]:
    tmp = tmp_path_factory.mktemp("bench")
    protocol = _protocol(tmp)
    result = run_benchmark(load_protocol(protocol), repo=ROOT)
    return protocol, result.out_dir


def test_protocol_loads_and_is_exported() -> None:
    assert "protocol" in schemas.SCHEMAS
    proto = load_protocol(ROOT / "experiments" / "protocols" / "synthetic-pilot.yaml")
    assert isinstance(proto, Protocol) and proto.methods == ["B0", "B1", "B3"]
    assert proto.label == "synthetic"


def test_one_command_regenerates_the_report(run: tuple[Path, Path]) -> None:
    _, out = run
    assert (out / "report.md").is_file() and (out / "results.csv").is_file()
    svgs = sorted(p.name for p in out.glob("*.svg"))
    assert "accuracy_by_method.svg" in svgs and "boundary_timing.svg" in svgs
    assert not list(out.rglob("*.flac")) and not list(out.rglob("*.wav"))


def test_results_cover_the_matrix_and_the_fault_injections(run: tuple[Path, Path]) -> None:
    _, out = run
    with (out / "results.csv").open(encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    takes = {"nominal", "drift", "skip"}
    # B0 and B1 once per take; B3 once per feed subset per take; plus the fault injection on
    # every (method, subset).
    expected_clean = len(takes) * (1 + 1 + 2)
    clean = [r for r in rows if r["fault"] == ""]
    faults = [r for r in rows if r["fault"] != ""]
    assert len(clean) == expected_clean
    assert len(faults) == expected_clean and {r["fault"] for r in faults} == {"drums_lost_20_30"}
    assert {r["method"] for r in rows} == {"B0", "B1", "B3"}
    assert {r["feeds"] for r in rows if r["method"] == "B3"} == {"chords+bass", "chords"}
    assert all(r["streams"] == "as-captured" and r["idealized"] == "false" for r in rows)
    for key in (
        "section_visit_accuracy",
        "bar_accuracy",
        "production_equivalent_accuracy",
        "boundary_error_median_s",
        "boundary_error_p95_s",
        "recovery_bars",
        "manifest_sha256",
    ):
        assert key in rows[0]
    b3 = next(
        r
        for r in clean
        if r["method"] == "B3" and r["take"].endswith("/skip") and r["feeds"] == "chords+bass"
    )
    assert float(b3["recovery_bars"]) <= 2
    b1 = next(r for r in clean if r["method"] == "B1" and r["take"].endswith("/skip"))
    assert b1["recovery_bars"] == ""


def test_report_has_every_required_section(run: tuple[Path, Path]) -> None:
    _, out = run
    text = (out / "report.md").read_text(encoding="utf-8")
    for heading in (
        "## Provenance",
        "## Results by method and feed subset",
        "## Fault injections",
        "## Assistance accounting",
        "## Failure examples",
        "## Reduced scope",
        "## Streams and the idealized label",
    ):
        assert heading in text, heading
    assert "SYNTHETIC" in text and "not evidence" in text.lower()
    assert "commit" in text.lower() and "config hash" in text.lower()
    assert "manifest" in text.lower()
    assert "B2" in text and "deferred" in text.lower()
    assert "told about" in text and "song start" in text
    assert "as-captured" in text and "idealized" in text
    assert "![" in text  # plots are linked


def test_report_is_reproducible(tmp_path: Path, run: tuple[Path, Path]) -> None:
    protocol, out = run
    again = run_benchmark(load_protocol(protocol), repo=ROOT, out_dir=tmp_path / "again")
    first = (out / "report.md").read_text(encoding="utf-8")
    second = (again.out_dir / "report.md").read_text(encoding="utf-8")
    assert first == second
    assert (out / "results.csv").read_text(encoding="utf-8") == (
        again.out_dir / "results.csv"
    ).read_text(encoding="utf-8")


def test_summary_json_lists_hashes(run: tuple[Path, Path]) -> None:
    _, out = run
    summary = json.loads((out / "summary.json").read_text(encoding="utf-8"))
    assert summary["label"] == "synthetic"
    assert set(summary["manifests"]) == {
        "synthetic_anthem/nominal",
        "synthetic_anthem/drift",
        "synthetic_anthem/skip",
    }
    assert len(summary["config_hash"]) == 64
    assert summary["commit"]
    assert summary["reduced_scope"]


def test_cli_benchmark(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    protocol = _protocol(
        tmp_path,
        corpus={
            "root": str(tmp_path / "rec"),
            "synthesize": {"songs": str(ROOT / "examples" / "songs"), "sample_rate": RATE},
            "songs": ["synthetic_waltz"],
            "takes": ["nominal"],
        },
        methods=["B0"],
        feed_subsets={},
        fault_injections=[],
    )
    assert main(["benchmark", str(protocol)]) == 0
    out = capsys.readouterr().out
    assert "report.md" in out and "SYNTHETIC" in out
    assert main(["benchmark", str(tmp_path / "missing.yaml")]) == 1
    bad = tmp_path / "bad.yaml"
    bad.write_text("kind: protocol\nschema_version: 0\n", encoding="utf-8")
    assert main(["benchmark", str(bad)]) == 1
    assert "error" in capsys.readouterr().err
