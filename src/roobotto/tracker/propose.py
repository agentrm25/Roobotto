"""Route a follower's ``MusicalState`` stream through the real engine so its proposals meet the
real decision gate and output supervisor, without anything firing (T-12, WP6).

The engine runs in the mock profile, unarmed, in memory, at milestone 4 in ASSISTED mode with
tracker authority enabled: the only configuration in which tracker evidence may schedule
anything at all. The song is selected and one GO is pressed at the annotated song start, which
is how the plan anchors the first downbeat (Section 8). From then on the tracker moves the
engine. Decorative cues may reach the mock adapter; standard cues are refused until M5 and
critical cues are refused on tracker evidence whatever the posterior (I-14). Nothing is armed,
so nothing can reach hardware (I-1, I-2).
"""

from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from roobotto.engine.clock import SimClock
from roobotto.engine.dry_run import go_press
from roobotto.engine.inputs import OperatorIn
from roobotto.engine.show import EngineSettings, ShowConfig, load_show
from roobotto.engine.spine import Runner, Spine
from roobotto.engine.trace import TraceWriter
from roobotto.record.jsonl import JsonlLog
from roobotto.tracker.follower import FollowerResult

NS = 1_000_000_000
PRESS_NS = 50_000_000


@dataclass(frozen=True)
class Proposal:
    records: list[dict[str, Any]]
    decision_hash: str
    entered_sections: list[str]
    attempts: int
    """Output attempts, all to the mock adapter."""
    decisions: dict[str, int]
    """Gate decisions by kind (fire, abstain, refuse ...), plus scheduler expiries."""
    fired_physical: int
    """Always 0: the run is unarmed in the mock profile. Reported so a reader can check."""

    def summary(self) -> dict[str, Any]:
        return {
            "entered_sections": self.entered_sections,
            "attempts_to_mock": self.attempts,
            "decisions": self.decisions,
            "fired_physical": self.fired_physical,
            "decision_hash": self.decision_hash,
        }


def drive(
    result: FollowerResult,
    show: ShowConfig,
    *,
    run_id: str = "follower",
    log: JsonlLog | None = None,
) -> Spine:
    """Select the song, press GO once at the annotated song start, switch to ASSISTED, enable
    tracker authority at the first state, then feed every ``MusicalState`` at its time. The
    spine is unarmed in whatever profile the show has; a mock profile means nothing can reach
    hardware."""
    if result.song.id not in show.songs:
        raise ValueError(f"song '{result.song.id}' is not in the show config")
    spine = Spine(show)
    clock = SimClock(0)
    if log is None:
        runner = Runner.in_memory(spine, clock, run_id=run_id)
    else:
        runner = Runner(spine, clock, trace=TraceWriter(None, spine.header()), log=log)
    runner.submit(OperatorIn(t_ns=0, action="select_song", value=result.song.id))
    runner.submit(OperatorIn(t_ns=0, action="set_mode", value="assisted"))
    start = result.song_start_ns
    runner.run_until(start)
    runner.submit(go_press(show, start, True))
    runner.run_until(start + PRESS_NS)
    runner.submit(go_press(show, start + PRESS_NS, False))
    enabled = False
    for state in result.states:
        runner.run_until(state.t_ns)
        runner.tracker(state)
        if not enabled:
            runner.submit(OperatorIn(t_ns=state.t_ns, action="enable_tracker"))
            enabled = True
    last = result.states[-1].t_ns if result.states else start
    runner.run_until(last + 2 * NS)
    return spine


def propose_cues(
    result: FollowerResult, config_paths: Sequence[Path], *, milestone: int = 4
) -> Proposal:
    show = load_show(config_paths, EngineSettings(performance="follower", milestone=milestone))
    spine = drive(result, show)
    records = list(spine.records)
    entered = [str(r["section"]) for r in records if r["type"] == "visit"]
    attempts = [r for r in records if r["type"] == "output_attempt"]
    physical = sum(1 for r in attempts if r.get("adapter") != "mock")
    decisions: dict[str, int] = {}
    for r in records:
        if r["type"] == "decision":
            kind = str(r.get("kind", "expired"))
            decisions[kind] = decisions.get(kind, 0) + 1
        elif r["type"] == "refusal":
            decisions["supervisor_refusal"] = decisions.get("supervisor_refusal", 0) + 1
    return Proposal(
        records=records,
        decision_hash=spine.decision_hash(),
        entered_sections=entered,
        attempts=len(attempts),
        decisions=decisions,
        fired_physical=physical,
    )
