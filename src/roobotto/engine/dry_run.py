"""``roobotto run --dry-run``: print the outputs the engine would plan, sending nothing.

Without a trace, the walkthrough selects the song, presses the GO switch once at the start of
each section at the song's nominal tempo, and ticks every beat. With a trace, it replays the
recorded inputs. With ``--replay <take>`` (T-14), the bar-grid follower listens to the take and
its ``MusicalState`` stream drives the engine, so cue intents land on the downbeats the follower
predicts and meet the real decision gate and output supervisor. Every way, the run is unarmed,
the MIDI adapter is disabled and the fired ledger is in memory, so every send goes to the mock
adapter and nothing persists. Every decision is printed, and logged as JSONL when asked.
"""

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from roobotto.engine.clock import SimClock
from roobotto.engine.inputs import MidiIn, OperatorIn
from roobotto.engine.show import EngineSettings, ShowConfig, ShowConfigError, load_show
from roobotto.engine.spine import ReplayMismatchError, Runner, Spine, replay
from roobotto.engine.trace import TraceError, load_trace

NS_PER_S = 1_000_000_000
NS_PER_MINUTE = 60 * NS_PER_S
START_NS = NS_PER_S


class DryRunError(ValueError):
    pass


def go_press(show: ShowConfig, t_ns: int, press: bool) -> MidiIn:
    """The GO switch pressed or released, as the rig's control map defines it."""
    control = next(c for c in show.rig.controls if c.action == "go")
    switch = next(s for s in show.rig.switches if s.id == control.switch)
    if switch.kind == "note":
        kind = "note_on" if press else "note_off"
        data2 = 127 if press else 0
    else:
        kind = "control_change"
        data2 = 127 if press else 0
    return MidiIn.model_validate(
        {
            "t_ns": t_ns,
            "port": switch.port,
            "kind": kind,
            "channel": switch.channel,
            "data1": switch.number,
            "data2": data2,
        }
    )


def walkthrough(runner: Runner, show: ShowConfig, song_id: str) -> None:
    """Select the song, then press GO at the start of each section at the nominal tempo."""
    song = show.songs[song_id]
    beat_ns = round(NS_PER_MINUTE / song.tempo.nominal)
    runner.submit(OperatorIn(t_ns=runner.clock.now_ns(), action="select_song", value=song_id))
    t = START_NS
    for section in song.sections:
        runner.run_until(t)
        runner.submit(go_press(show, t, True))
        runner.run_until(t + 50_000_000)
        runner.submit(go_press(show, t + 50_000_000, False))
        t += section.bars * song.beats_per_bar * beat_ns
    runner.run_until(t)


def _describe(record: dict[str, Any], origin: int) -> str | None:
    seconds = (record["t_ns"] - origin) / NS_PER_S
    occurrence: dict[str, Any] = record.get("occurrence") or {}
    where = (
        f"{occurrence.get('cue_id')} [song {occurrence.get('song_instance')} "
        f"visit {occurrence.get('section_visit')}]"
    )
    if record["type"] == "output_attempt":
        return (
            f"{seconds:9.3f} s  SEND    {record['command']}={record['value']}  {where}  "
            f"-> {record['adapter']}  ({record['reason']})"
        )
    if record["type"] == "deferred":
        later = (record["send_at_ns"] - record["t_ns"]) / 1_000_000
        return f"{seconds:9.3f} s  DEFER   {record['intent']}  {where}  (+{later:g} ms, ADR-017)"
    if record["type"] == "refusal":
        return f"{seconds:9.3f} s  REFUSE  {record['intent'] or '*'}  {where}  ({record['reason']})"
    if record["type"] == "decision" and record.get("kind") in ("ask", "abstain", "hold"):
        return f"{seconds:9.3f} s  {record['kind'].upper():7} {where}  ({record['reason']})"
    if record["type"] == "go":
        return (
            f"{seconds:9.3f} s  GO      class {record['timing_class']} into "
            f"{record['section']} visit {record['visit']}"
        )
    if record["type"] == "back":
        return f"{seconds:9.3f} s  BACK    {record['event']}"
    if record["type"] == "visit":
        return (
            f"{seconds:9.3f} s  ENTER   {record['section']} visit {record['visit']} "
            f"(via {record.get('via')})"
        )
    if record["type"] == "decision" and record.get("kind") == "fire":
        return f"{seconds:9.3f} s  FIRE    {where}  ({record['reason']})"
    if record["type"] == "planned":
        due = (record["due_ns"] - origin) / NS_PER_S
        return f"{seconds:9.3f} s  PLAN    {where}  due {due:.3f} s"
    if record["type"] == "tracker" and record.get("event") in (
        "authority_enabled",
        "authority_suspended",
        "ignored",
    ):
        return f"{seconds:9.3f} s  TRACKER {record['event']}  ({record.get('reason', '')})"
    return None


def dry_run(
    paths: Sequence[Path],
    *,
    song: str | None = None,
    performance: str = "dry_run",
    trace: Path | None = None,
    replay_take: Path | None = None,
    reference: Path | None = None,
    milestone: int = 1,
    feeds: Sequence[str] | None = None,
    block: int = 512,
    log: Path | None = None,
) -> list[str]:
    try:
        show = load_show(paths, EngineSettings(performance=performance, milestone=milestone))
    except (ShowConfigError, ValueError) as exc:
        raise DryRunError(str(exc)) from exc

    if replay_take is not None:
        if trace is not None:
            raise DryRunError("--replay and --trace are different sources; give one")
        spine, title = _replay_take(show, replay_take, reference, feeds, block, log, performance)
    elif trace is not None:
        try:
            spine = replay(show, load_trace(trace))
        except (TraceError, ReplayMismatchError, OSError) as exc:
            raise DryRunError(str(exc)) from exc
        title = f"replay of {trace}"
    else:
        if song is None:
            song = show.setlist.entries[0].song if show.setlist else sorted(show.songs)[0]
        if song not in show.songs:
            raise DryRunError(f"unknown song '{song}'")
        spine = Spine(show)
        runner = Runner.in_memory(spine, SimClock(), run_id="dry_run")
        walkthrough(runner, show, song)
        title = f"walkthrough of '{song}', one GO per section at the nominal tempo"

    origin = spine.origin_ns or 0
    lines = [
        f"Dry run: {title}",
        f"Rig '{show.rig.id}' ({show.rig.profile} profile), performance '{performance}', "
        f"milestone {milestone}, config {show.config_hash[:12]}",
        "Outputs are not armed and the MIDI adapter is disabled: every send goes to the mock "
        "adapter.",
        "",
    ]
    lines += [d for r in spine.records if (d := _describe(r, origin)) is not None]
    sends = sum(1 for r in spine.records if r["type"] == "output_attempt")
    refusals = sum(1 for r in spine.records if r["type"] == "refusal")
    lines += [
        "",
        f"{sends} planned send(s), {refusals} refusal(s). Decision hash {spine.decision_hash()}",
    ]
    return lines


def _replay_take(
    show: ShowConfig,
    take: Path,
    reference: Path | None,
    feeds: Sequence[str] | None,
    block: int,
    log: Path | None,
    performance: str,
) -> tuple[Spine, str]:
    """The follower listens to the take; its states drive the engine (T-14)."""
    from roobotto.record.jsonl import JsonlLog
    from roobotto.tracker import follower, propose

    ref_dir = reference if reference is not None else take.parent / "nominal"
    if not (ref_dir / "manifest.json").is_file():
        raise DryRunError(f"no reference take at {ref_dir}; give --reference")
    if not (take / "manifest.json").is_file():
        raise DryRunError(f"no take at {take}")
    chosen = tuple(feeds) if feeds else follower.DEFAULT_FEEDS
    try:
        ref = follower.build_reference([ref_dir], feeds=chosen, block=block)
        result = follower.run_follower(
            take, ref, config=follower.FollowerConfig(feeds=chosen), block=block
        )
    except (follower.FollowerError, ValueError, OSError) as exc:
        raise DryRunError(str(exc)) from exc
    if result.song.id not in show.songs:
        raise DryRunError(f"the take's song '{result.song.id}' is not in the config")
    jsonl = JsonlLog(log, run_id=performance, config_hash=show.config_hash)
    spine = propose.drive(result, show, run_id=performance, log=jsonl)
    title = (
        f"replay of take {result.annotations.take} with the bar-grid follower on "
        f"{', '.join(chosen)} (reference {ref_dir.name}); follower accuracy "
        f"{result.score.section_visit_accuracy:.3f} section+visit per beat"
    )
    return spine, title
