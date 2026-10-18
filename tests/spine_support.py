"""Shared fixtures for the control-spine tests (T-04).

Everything here is synthetic. ``fixture_synth`` and its "bench_verified" command exist only in
tests: the bench log path is a placeholder that no bench run produced, and its sends go to an
in-memory recording backend, never to a port. No real device mapping is invented.
"""

from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from roobotto.adapters.midi import MidiAdapter
from roobotto.config.rig import Rig
from roobotto.config.setlist import Setlist
from roobotto.config.song import Song
from roobotto.cues.ledger import FiredLedger
from roobotto.engine.clock import SimClock
from roobotto.engine.inputs import OperatorAction
from roobotto.engine.show import EngineSettings, ShowConfig
from roobotto.engine.spine import Record, Runner, Spine
from roobotto.messages import MusicalState, OutputAttempt

MS = 1_000_000
BEAT_MS = 500  # 120 BPM
BAR_MS = 2000  # 4/4

SONG_DOC: dict[str, Any] = {
    "kind": "song",
    "schema_version": 0,
    "id": "spine_test",
    "title": "Spine Test",
    "tempo": {"nominal": 120, "range": [100, 140]},
    "meter": "4/4",
    "sections": [
        {"id": "intro", "bars": 2},
        {"id": "verse", "bars": 2, "advance_on": {"snapshot": 1}},
        {"id": "chorus", "bars": 2, "advance_on": {"snapshot": 2}},
        {"id": "solo", "bars": 2, "advance_on": {"snapshot": 3}},
        {"id": "outro", "bars": 2, "ends": True, "advance_on": {"snapshot": 4}},
    ],
    "cues": [
        {
            "id": "verse_look",
            "at": {"section": "verse", "bar": 2},
            "class": "decorative",
            "state": {"mvp.scene": "verse_b"},
            "on_miss": "skip",
        },
        {
            "id": "chorus_scene",
            "at": {"section": "chorus"},
            "class": "standard",
            "state": {"mvp.scene": "chorus", "aum.layer": "pad"},
            "on_miss": {"fire_late_within_beats": 1},
        },
        {
            "id": "chorus_synth",
            "at": {"section": "chorus"},
            "class": "standard",
            "state": {"synth.patch": "bright"},
        },
        {
            "id": "solo_lead",
            "at": {"section": "solo"},
            "class": "critical",
            "state": {"guitar.snapshot": "lead"},
            "requires": {"corroborate_by": ["footswitch", "mapped_snapshot"], "within_bars": 1},
            "on_uncertain": "ask",
        },
        {
            "id": "solo_pad",
            "at": {"section": "solo", "beat": 3},
            "class": "critical",
            "state": {"aum.layer": "pad"},
            "requires": {"corroborate_by": ["footswitch"], "within_bars": 1},
            "on_uncertain": "ask",
        },
        {
            "id": "outro_scene",
            "at": {"section": "outro"},
            "class": "standard",
            "state": {"mvp.scene": "chorus"},
        },
    ],
}


def _rig_doc(profile: str) -> dict[str, Any]:
    synth_port = "usb_out" if profile == "physical" else "mock_out_synth"
    ports = [
        {"id": "mock_out_mvp", "direction": "out", "transport": "mock"},
        {"id": "mock_out_aum", "direction": "out", "transport": "mock"},
        {"id": "mock_out_helix", "direction": "out", "transport": "mock"},
        {"id": "mock_in_helix", "direction": "in", "transport": "mock"},
        {"id": "mock_in_helix_h4", "direction": "in", "transport": "mock"},
        {"id": "mock_in_switches", "direction": "in", "transport": "mock"},
        {"id": "mock_in_switches_h4", "direction": "in", "transport": "mock"},
    ]
    if profile == "physical":
        ports.append({"id": "usb_out", "direction": "out", "transport": "usb"})
    else:
        ports.append({"id": "mock_out_synth", "direction": "out", "transport": "mock"})
    return {
        "kind": "rig",
        "schema_version": 0,
        "id": f"spine_{profile}",
        "profile": profile,
        "ports": ports,
        "devices": [
            {
                "id": "mvp",
                "kind": "mvp",
                "owner": "roobotto",
                "receives": [{"port": "mock_out_mvp", "channel": 3, "behavior": "intended"}],
                "commands": {
                    "scene": {
                        "type": "one_shot",
                        "port": "mock_out_mvp",
                        "channel": 3,
                        "values": {
                            "verse_b": {"kind": "note_on", "data1": 60, "data2": 127},
                            "chorus": {"kind": "note_on", "data1": 61, "data2": 127},
                        },
                        "status": "documented",
                        "evidence": {"source": "E-07"},
                        "min_interval_ms": 100,
                    }
                },
            },
            {
                "id": "aum",
                "kind": "aum",
                "owner": "roobotto",
                "receives": [{"port": "mock_out_aum", "channel": 15, "behavior": "intended"}],
                "commands": {
                    "layer": {
                        "type": "state",
                        "port": "mock_out_aum",
                        "channel": 15,
                        "values": {"pad": None},
                        "status": "unknown",
                        "min_interval_ms": 250,
                    }
                },
            },
            {
                "id": "guitar",
                "kind": "helix",
                "owner": "roobotto",
                "receives": [{"port": "mock_out_helix", "channel": 1, "behavior": "intended"}],
                "commands": {
                    "snapshot": {
                        "type": "state",
                        "port": "mock_out_helix",
                        "channel": 1,
                        # Snapshot 4: from the solo it would advance to the outro, so an echo of
                        # it must never count as a press (ADR-011).
                        "values": {"lead": {"kind": "control_change", "data1": 69, "data2": 3}},
                        "status": "documented",
                        "evidence": {"source": "E-01"},
                        "min_interval_ms": 250,
                    }
                },
                "reports": {
                    "snapshot": {
                        "meaning": "snapshot",
                        "source": "helix",
                        "channel": 1,
                        "kind": "control_change",
                        "number": 69,
                        "values": [0, 1, 2, 3, 4, 5, 6, 7],
                        "status": "documented",
                        "evidence": {"source": "E-01"},
                    },
                    "preset": {
                        "meaning": "preset",
                        "source": "helix",
                        "channel": 1,
                        "kind": "program_change",
                        "status": "documented",
                        "evidence": {"source": "E-01"},
                    },
                },
            },
            {
                "id": "synth",
                "kind": "fixture_synth",
                "owner": "roobotto",
                "receives": [{"port": synth_port, "channel": 2, "behavior": "intended"}],
                "commands": {
                    "patch": {
                        "type": "state",
                        "port": synth_port,
                        "channel": 2,
                        "values": {"bright": {"kind": "program_change", "data1": 5}},
                        "status": "bench_verified",
                        "evidence": {"bench_log": "captures/test-fixture/not-a-real-bench-log.txt"},
                        "min_interval_ms": 250,
                    }
                },
            },
        ],
        "inputs": [
            {
                "id": "helix",
                "paths": [
                    {"port": "mock_in_helix", "authoritative": True},
                    {"port": "mock_in_helix_h4", "authoritative": False},
                ],
            },
            {
                "id": "footswitches",
                "paths": [
                    {"port": "mock_in_switches", "authoritative": True},
                    {"port": "mock_in_switches_h4", "authoritative": False},
                ],
            },
        ],
        "switches": [
            {
                "id": "go_sw",
                "port": "mock_in_switches",
                "channel": 16,
                "kind": "note",
                "number": 60,
            },
            {
                "id": "back_sw",
                "port": "mock_in_switches",
                "channel": 16,
                "kind": "note",
                "number": 61,
            },
            {
                "id": "stop_sw",
                "port": "mock_in_switches",
                "channel": 16,
                "kind": "note",
                "number": 62,
            },
        ],
        "controls": [
            {"switch": "go_sw", "gesture": "press", "action": "go"},
            {"switch": "back_sw", "gesture": "tap", "action": "back"},
            {"switch": "back_sw", "gesture": "hold", "action": "manual"},
            {"switch": "stop_sw", "gesture": "press", "action": "stop"},
        ],
    }


SETLIST_DOC: dict[str, Any] = {
    "kind": "setlist",
    "schema_version": 0,
    "id": "spine_set",
    "title": "Spine Set",
    "entries": [{"song": "spine_test", "program_change": 0}],
}

SWITCH_NOTE = {"go": 60, "back": 61, "stop": 62}


def song() -> Song:
    return Song.model_validate(deepcopy(SONG_DOC))


def rig(profile: str = "mock") -> Rig:
    return Rig.model_validate(_rig_doc(profile))


def rig_doc(profile: str = "mock") -> dict[str, Any]:
    return _rig_doc(profile)


def show(profile: str = "mock", **settings: Any) -> ShowConfig:
    return ShowConfig(
        rig=rig(profile),
        songs={"spine_test": song()},
        setlist=Setlist.model_validate(deepcopy(SETLIST_DOC)),
        settings=EngineSettings(**settings),
    )


class RecordingBackend:
    """An in-memory stand-in for a MIDI port backend. Test-only; it opens nothing."""

    def __init__(self) -> None:
        self.sent: list[tuple[str, bytes]] = []

    def send(self, port: str, data: bytes) -> None:
        self.sent.append((port, data))


def tracker_state(
    *,
    session: str = "aaaaaaaa",
    seq: int,
    t_ns: int,
    section: str = "verse",
    bar: int = 1,
    p: float = 0.95,
    mode: str = "following",
    downbeat_in_ms: int | None = 100,
    song_id: str = "spine_test",
) -> MusicalState:
    data: dict[str, Any] = {
        "tracker_session": session,
        "seq": seq,
        "t_ns": t_ns,
        "song": {"id": song_id, "p": 0.99},
        "mode": mode,
        "hypotheses": [{"section": section, "visit": 1, "bar": bar, "p": p}],
        "beat": {"phase": 0.5, "tempo_bpm": 120.0, "tempo_sd": 1.0},
    }
    if downbeat_in_ms is not None:
        data["next_downbeat"] = {"t_ns": t_ns + downbeat_in_ms * MS, "sd_ms": 10.0}
    return MusicalState.model_validate(data)


@dataclass
class World:
    """A spine driven through the process wrapper on a simulated clock."""

    show: ShowConfig
    ledger: FiredLedger = field(default_factory=FiredLedger)
    midi: MidiAdapter | None = None
    clock: SimClock = field(default_factory=lambda: SimClock(1_000 * MS))
    spine: Spine = field(init=False)
    runner: Runner = field(init=False)
    seq: int = 0

    def __post_init__(self) -> None:
        self.spine = Spine(self.show, ledger=self.ledger, midi=self.midi)
        self.runner = Runner.in_memory(self.spine, self.clock)

    @classmethod
    def make(cls, profile: str = "mock", **settings: Any) -> "World":
        return cls(show(profile, **settings))

    def restart(self) -> "World":
        """A new process: same config and ledger, nothing else carried over."""
        return World(self.show, ledger=self.ledger, midi=self.midi, clock=self.clock)

    # time
    def wait(self, ms: int) -> list[Record]:
        """Let ``ms`` pass, ticking at every engine wake-up as a live timer would."""
        return self.runner.run_until(self.clock.now_ns() + ms * MS)

    # inputs
    def press(self, action: str = "go", *, port: str = "mock_in_switches") -> list[Record]:
        return self.runner.midi(port, "note_on", 16, SWITCH_NOTE[action], 127)

    def release(self, action: str = "go", *, port: str = "mock_in_switches") -> list[Record]:
        return self.runner.midi(port, "note_off", 16, SWITCH_NOTE[action], 0)

    def snapshot(
        self, number: int, *, port: str = "mock_in_helix", automated: bool = False
    ) -> list[Record]:
        return self.runner.midi(port, "control_change", 1, 69, number - 1, automated=automated)

    def program(self, number: int, *, port: str = "mock_in_helix") -> list[Record]:
        return self.runner.midi(port, "program_change", 1, number)

    def op(self, action: OperatorAction, value: str | None = None) -> list[Record]:
        return self.runner.operator(action, value)

    def track(self, **kwargs: Any) -> list[Record]:
        self.seq = kwargs.pop("seq", self.seq + 1)
        state = tracker_state(seq=self.seq, t_ns=kwargs.pop("t_ns", self.clock.now_ns()), **kwargs)
        return self.runner.tracker(state)

    # views
    @property
    def attempts(self) -> list[OutputAttempt]:
        return self.spine.attempts

    def sent(self, command: str | None = None) -> list[OutputAttempt]:
        return [a for a in self.attempts if command is None or a.command == command]

    def records(self, kind: str) -> list[Record]:
        return [r for r in self.spine.records if r["type"] == kind]

    def select(self) -> list[Record]:
        return self.op("select_song", "spine_test")
