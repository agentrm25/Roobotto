"""The control spine: engine core plus output supervisor, the process wrapper, and replay.

``Spine`` joins the deterministic core to the supervisor. Given the same consumed input trace,
configuration, seed and starting ledger, it produces the same records and the same normalized
decision hash (I-19).

``Runner`` is the process wrapper and event bus. It owns the injected clock, stamps every input
with the time it became available, records the consumed input trace, steps the spine, writes
JSONL logs and hands each record to subscribers (recorder, HUD).

``replay`` drives a fresh spine from a recorded trace, with mock outputs only and the future-read
guard on.
"""

from collections.abc import Callable
from typing import Any

from roobotto.adapters.midi import MidiAdapter, NullBackend
from roobotto.adapters.mock import MockAdapter
from roobotto.cues.ledger import FiredLedger
from roobotto.cues.supervisor import Supervisor
from roobotto.engine.clock import Clock, SimClock
from roobotto.engine.core import Engine, InstanceStart, Note
from roobotto.engine.guard import FutureReadGuard
from roobotto.engine.hashing import decision_hash
from roobotto.engine.inputs import MidiIn, OperatorAction, OperatorIn, PortStatus, Tick, TrackerIn
from roobotto.engine.show import ShowConfig
from roobotto.engine.trace import InputT, Trace, TraceHeader, TraceWriter
from roobotto.messages import MusicalState, OutputAttempt, SongInstanceRecord
from roobotto.record.jsonl import JsonlLog

Record = dict[str, Any]


class Spine:
    def __init__(
        self,
        show: ShowConfig,
        *,
        ledger: FiredLedger | None = None,
        midi: MidiAdapter | None = None,
    ) -> None:
        """Always starts unarmed, with nothing pending; the ledger blocks resends (I-2, I-5)."""
        self.show = show
        self.config_hash = show.config_hash
        self.ledger = ledger if ledger is not None else FiredLedger()
        self.initial_ledger = list(self.ledger.records())
        self.mock = MockAdapter()
        self.midi = midi if midi is not None else MidiAdapter()
        self.engine = Engine(
            show, next_song_instance=self.ledger.next_song_instance(show.settings.performance)
        )
        self.supervisor = Supervisor(
            show.rig, self.ledger, mock=self.mock, midi=self.midi, config_hash=self.config_hash
        )
        self.records: list[Record] = []
        self.attempts: list[OutputAttempt] = []
        self.origin_ns: int | None = None

    def step(self, inp: InputT) -> list[Record]:
        if self.origin_ns is None:
            self.origin_ns = inp.t_ns
        if isinstance(inp, PortStatus):
            self.supervisor.set_port(inp.port, inp.connected)
        new: list[Record] = []
        for effect in self.engine.step(inp):
            if isinstance(effect, Note):
                new.append(effect.as_record())
                continue
            now = self.engine.now_ns
            if isinstance(effect, InstanceStart):
                self.ledger.append(
                    SongInstanceRecord(
                        performance=effect.performance,
                        song_instance=effect.song_instance,
                        song=effect.song,
                        started_ns=now,
                    )
                )
                continue
            new.append(effect.as_record(now))
            attempts, refusals = self.supervisor.submit(
                effect,
                now_ns=now,
                generation=self.engine.scheduler.generation,
                stopped=self.engine.stopped,
                armed=self.engine.armed,
            )
            self.engine.on_commit_result(effect, attempts)
            self.attempts.extend(attempts)
            new.extend(
                {"type": "output_attempt", "t_ns": now, **a.model_dump(mode="json")}
                for a in attempts
            )
            new.extend(r.as_record() for r in refusals)
        self.records.extend(new)
        return new

    def header(self) -> TraceHeader:
        return TraceHeader(
            config_hash=self.config_hash,
            seed=self.show.settings.seed,
            performance=self.show.settings.performance,
            midi_enabled=self.midi.enabled,
            initial_ledger=self.initial_ledger,
        )

    def decision_hash(self) -> str:
        return decision_hash(self.records, self.origin_ns or 0)


class Runner:
    def __init__(self, spine: Spine, clock: Clock, *, trace: TraceWriter, log: JsonlLog) -> None:
        self.spine = spine
        self.clock = clock
        self.trace = trace
        self.log = log
        self._subscribers: list[Callable[[Record], None]] = []

    @classmethod
    def in_memory(cls, spine: Spine, clock: Clock, run_id: str = "run") -> "Runner":
        return cls(
            spine,
            clock,
            trace=TraceWriter(None, spine.header()),
            log=JsonlLog(None, run_id=run_id, config_hash=spine.config_hash),
        )

    def subscribe(self, callback: Callable[[Record], None]) -> None:
        self._subscribers.append(callback)

    def submit(self, inp: InputT) -> list[Record]:
        self.trace.write(inp)
        records = self.spine.step(inp)
        for record in records:
            self.log.write(record)
            for callback in self._subscribers:
                callback(record)
        return records

    def tick(self) -> list[Record]:
        return self.submit(Tick(t_ns=self.clock.now_ns()))

    def run_until(self, t_ns: int) -> list[Record]:
        """Simulated time only: tick at every engine wake-up up to ``t_ns``, then at ``t_ns``.
        A live wrapper does the same with real timers."""
        clock = self.clock
        if not isinstance(clock, SimClock):
            raise TypeError("run_until needs a SimClock; a live wrapper uses timers")
        out: list[Record] = []
        while (wake := self.spine.engine.next_wakeup_ns()) is not None and wake <= t_ns:
            if wake > clock.now_ns():
                clock.jump_to(wake)
            out += self.tick()
        if t_ns > clock.now_ns():
            clock.jump_to(t_ns)
        out += self.tick()
        return out

    def midi(
        self,
        port: str,
        kind: str,
        channel: int,
        data1: int,
        data2: int | None = None,
        *,
        automated: bool = False,
    ) -> list[Record]:
        return self.submit(
            MidiIn.model_validate(
                {
                    "t_ns": self.clock.now_ns(),
                    "port": port,
                    "kind": kind,
                    "channel": channel,
                    "data1": data1,
                    "data2": data2,
                    "automated": automated,
                }
            )
        )

    def tracker(self, state: MusicalState) -> list[Record]:
        return self.submit(TrackerIn(t_ns=self.clock.now_ns(), state=state))

    def operator(self, action: OperatorAction, value: str | None = None) -> list[Record]:
        return self.submit(OperatorIn(t_ns=self.clock.now_ns(), action=action, value=value))

    def port(self, port: str, connected: bool) -> list[Record]:
        return self.submit(PortStatus(t_ns=self.clock.now_ns(), port=port, connected=connected))


class ReplayMismatchError(ValueError):
    pass


def replay(show: ShowConfig, trace: Trace) -> Spine:
    """Drive a fresh spine from ``trace``. Nothing reaches hardware: the MIDI adapter, if the
    recorded run had one, is replaced by one that discards every message."""
    if trace.header.config_hash != show.config_hash:
        raise ReplayMismatchError(
            "the trace was recorded with a different configuration "
            f"({trace.header.config_hash[:12]} vs {show.config_hash[:12]})"
        )
    ledger = FiredLedger()
    ledger.preload(trace.header.initial_ledger)
    midi = MidiAdapter(NullBackend()) if trace.header.midi_enabled else MidiAdapter()
    spine = Spine(show, ledger=ledger, midi=midi)
    guard = FutureReadGuard()
    for inp in trace.inputs:
        guard.advance(inp.t_ns)
        spine.step(inp)
    return spine
