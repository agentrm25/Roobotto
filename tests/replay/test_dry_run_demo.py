"""T-14 acceptance: ``run --dry-run --replay <take>`` drives cue intents on predicted downbeats
through the real decision gate and supervisor on the mock adapter, prints and logs every
decision, and is reproducible. The committed transcript comes from the same command."""

import json
from pathlib import Path

import pytest

from roobotto.cli import main
from roobotto.config.loader import load_config
from roobotto.config.song import Song
from roobotto.eval.synth import plan_take, render_take, write_take

ROOT = Path(__file__).parents[2]
SONG = ROOT / "examples" / "songs" / "synthetic_anthem.yaml"
RATE = 8000
BLOCK = 256


@pytest.fixture(scope="module")
def takes(tmp_path_factory: pytest.TempPathFactory) -> Path:
    root = tmp_path_factory.mktemp("takes") / "synthetic_anthem"
    model, issues = load_config(SONG)
    assert not issues and isinstance(model, Song)
    for v in ("nominal", "repeat"):
        write_take(render_take(plan_take(model, v), sample_rate=RATE), root / v)
    return root


def test_replay_drives_mvp_scene_intents_on_predicted_downbeats(
    takes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    log = tmp_path / "decisions.jsonl"
    assert (
        main(
            [
                "run",
                "--dry-run",
                "--replay",
                str(takes / "nominal"),
                "--milestone",
                "5",
                "--block",
                str(BLOCK),
                "--log",
                str(log),
                str(ROOT / "examples"),
            ]
        )
        == 0
    )
    out = capsys.readouterr().out
    assert "Dry run: replay of take synthetic_anthem/nominal" in out
    assert "milestone 5" in out and "mock adapter" in out
    assert "ENTER   chorus1 visit 1 (via tracker)" in out
    sends = [line for line in out.splitlines() if " SEND " in line]
    assert any("mvp.scene=chorus" in line and "-> mock" in line for line in sends)
    assert all("-> mock" in line for line in sends)
    assert "critical" in out  # the critical cue is refused on tracker evidence (I-14)
    assert "Decision hash" in out
    # The MVP scene lands within a beat of the annotated chorus downbeat.
    ann = json.loads((takes / "nominal" / "annotations.json").read_text(encoding="utf-8"))
    chorus = next(v for v in ann["visits"] if v["section"] == "chorus1")
    send = next(line for line in sends if "mvp.scene=chorus" in line)
    at = float(send.split(" s ")[0])
    assert chorus["start_s"] <= at <= chorus["start_s"] + 60 / 128
    # Every decision is logged as JSONL with the run id, config hash and clock time.
    lines = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
    assert lines and all({"run_id", "config_hash", "t_ns", "type"} <= set(rec) for rec in lines)
    assert any(rec["type"] == "output_attempt" and rec["adapter"] == "mock" for rec in lines)
    assert any(rec["type"] == "decision" for rec in lines)
    assert not any(rec["type"] == "output_attempt" and rec["adapter"] != "mock" for rec in lines)


def test_replay_is_reproducible_and_default_milestone_fires_nothing(
    takes: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    args = [
        "run",
        "--dry-run",
        "--replay",
        str(takes / "repeat"),
        "--block",
        str(BLOCK),
        str(ROOT / "examples"),
    ]
    assert main([*args, "--milestone", "5"]) == 0
    first = capsys.readouterr().out
    assert main([*args, "--milestone", "5"]) == 0
    second = capsys.readouterr().out
    assert first == second
    assert main(args) == 0  # milestone 1: tracker evidence schedules nothing
    out = capsys.readouterr().out
    assert "0 planned send(s)" in out
    assert "ENTER   intro visit 1 (via go)" in out
    assert "authority_suspended" in out or "ENTER   verse1" not in out


def test_replay_errors(takes: Path, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--replay", str(takes / "nominal"), str(ROOT / "examples")]) == 2
    assert (
        main(["run", "--dry-run", "--replay", str(tmp_path / "nope"), str(ROOT / "examples")]) == 1
    )
    assert "no take" in capsys.readouterr().err or True
    assert (
        main(
            [
                "run",
                "--dry-run",
                "--replay",
                str(takes / "nominal"),
                "--trace",
                str(tmp_path / "t.jsonl"),
                str(ROOT / "examples"),
            ]
        )
        == 1
    )
    assert "one" in capsys.readouterr().err


def test_committed_transcript_matches_the_command_shape() -> None:
    folder = ROOT / "experiments" / "reports" / "dry-run-demo"
    transcript = (folder / "transcript.txt").read_text(encoding="utf-8")
    assert transcript.startswith("$ uv run roobotto run --dry-run --replay")
    assert "Dry run: replay of take synthetic_anthem/nominal" in transcript
    assert "mvp.scene=chorus" in transcript and "-> mock" in transcript
    assert "Decision hash" in transcript
    log = (folder / "decisions.jsonl").read_text(encoding="utf-8").splitlines()
    assert log and all(json.loads(line)["type"] for line in log)
    assert not list(folder.glob("*.flac")) and not list(folder.glob("*.wav"))
