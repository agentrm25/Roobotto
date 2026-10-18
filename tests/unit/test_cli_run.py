"""``roobotto run --dry-run`` prints the planned outputs (T-04 acceptance)."""

import re
from pathlib import Path

import pytest

from roobotto.cli import main
from roobotto.engine.clock import SimClock
from roobotto.engine.dry_run import walkthrough
from roobotto.engine.show import EngineSettings, load_show
from roobotto.engine.spine import Runner, Spine
from roobotto.engine.trace import TraceWriter
from roobotto.record.jsonl import JsonlLog

EXAMPLES = Path(__file__).parents[2] / "examples"


def test_dry_run_prints_planned_outputs(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--dry-run", str(EXAMPLES)]) == 0
    out = capsys.readouterr().out
    assert "not armed" in out
    sends = [line for line in out.splitlines() if " SEND " in line]
    assert any("mvp.scene=chorus" in line for line in sends)
    assert all("-> mock" in line for line in sends)
    assert re.search(r"Decision hash [0-9a-f]{64}", out)


def test_live_run_is_refused(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", str(EXAMPLES)]) == 2
    assert "--dry-run" in capsys.readouterr().err


def test_dry_run_rejects_unknown_song(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["run", "--dry-run", str(EXAMPLES), "--song", "nope"]) == 1
    assert "unknown song" in capsys.readouterr().err


def test_dry_run_replays_a_trace(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    show = load_show([EXAMPLES], EngineSettings(performance="dry_run"))
    spine = Spine(show)
    path = tmp_path / "t.jsonl"
    writer = TraceWriter(path, spine.header())
    runner = Runner(
        spine,
        SimClock(),
        trace=writer,
        log=JsonlLog(None, run_id="x", config_hash=spine.config_hash),
    )
    walkthrough(runner, show, "synthetic_anthem")
    writer.close()
    assert main(["run", "--dry-run", str(EXAMPLES), "--trace", str(path)]) == 0
    assert spine.decision_hash() in capsys.readouterr().out
