"""Invariants I-1 to I-16, I-18 and I-19 under Hypothesis stateful testing (plan Sections 5.6, 12).

A ``RuleBasedStateMachine`` drives the control spine with random presses on both paths,
snapshot changes (mapped, unmapped, automated), preset loads, echoes of Roobotto's own sends,
tracker packets (old sessions, lost and reordered sequence numbers, stale and lost modes),
operator actions, port disconnects and reconnects, clock jumps and restarts. After every step
each invariant below is checked by its own named method. I-17 (gestures) lands with T-05.

Occurrence ids (I-6) must be unique across restarts: song instances are persisted in the fired
ledger (ADR-016). Rate limits (I-15) are checked within one process, because the supervisor's
rate-limit memory does not survive a restart (listed in docs/safety-model.md).
"""

from collections import Counter, defaultdict
from collections.abc import Callable
from typing import Any

from hypothesis import HealthCheck, event, settings
from hypothesis import strategies as st
from hypothesis.stateful import RuleBasedStateMachine, initialize, invariant, rule

from roobotto.adapters.midi import MidiAdapter
from roobotto.adapters.midi_wire import decode
from roobotto.engine.inputs import MidiIn, TrackerIn
from roobotto.engine.spine import Record, replay
from roobotto.messages import FiredLedgerRecord, MusicalState, OccurrenceId
from spine_support import MS, SWITCH_NOTE, RecordingBackend, World, show

SESSIONS = ["aaaaaaaa", "bbbbbbbb", "cccccccc"]
SECTIONS = ["intro", "verse", "chorus", "solo", "outro"]
OPERATOR_ACTIONS = [
    "arm",
    "disarm",
    "resume",
    "confirm_preset_b",
    "enable_tracker",
    "set_mode",
    "select_song",
    "next_song",
    "resync",
]
AUTHORIZING_DETAILS = {"switch_press", "snapshot_mapped"}


def occ_key(record: Record) -> str:
    o = record["occurrence"]
    return f"{o['performance']}/{o['song_instance']}/{o['section_visit']}/{o['cue_id']}"


@settings(
    max_examples=300,
    stateful_step_count=60,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
class SpineMachine(RuleBasedStateMachine):
    def __init__(self) -> None:
        super().__init__()
        self.backend = RecordingBackend()
        self.world: World | None = None
        self.segment = 0
        self.violations: dict[str, list[str]] = defaultdict(list)
        self.step_records: list[Record] = []
        self.step_op: str | None = None
        self.step_new_session = False
        self.sessions_seen: list[str] = []
        self.seq: dict[str, int] = {}
        self.events: dict[tuple[int, str], Record] = {}
        self.planned: set[str] = set()
        self.restart_instance_floor = 0
        self.commits: dict[str, Record] = {}
        self.sent: Counter[tuple[str, str]] = Counter()
        self.overrides: dict[tuple[int, str, int], set[str]] = defaultdict(set)
        self.performer_devices_this_step: set[str] = set()
        self.last_send: dict[str, tuple[int, int]] = {}
        self.last_preset_ns: int | None = None
        self.pre_restart_ledger: set[str] = set()
        self.stopped = False
        self.confirmed = False
        self.awaiting_go = False
        self.just_restarted = False

    # ------------------------------------------------------------------ driving

    @property
    def w(self) -> World:
        assert self.world is not None
        return self.world

    @initialize(
        milestone=st.integers(1, 8),
        physical=st.booleans(),
        start=st.integers(0, 4),
        armed=st.booleans(),
        assisted=st.booleans(),
    )
    def setup(
        self, milestone: int, physical: bool, start: int, armed: bool, assisted: bool
    ) -> None:
        """Start somewhere interesting: ``start`` GOs into the song, maybe armed and ASSISTED."""
        self.world = World(
            show("physical" if physical else "mock", milestone=milestone),
            midi=MidiAdapter(self.backend),
        )
        self._do(lambda: self.w.select())
        if armed:
            self.arm()
        for _ in range(start):
            self.go(False)
            self.advance(300)
        if assisted:
            self.assisted_with_tracker()

    def _do(
        self, action: Callable[[], object], op: str | None = None, new_session: bool = False
    ) -> None:
        engine = self.w.spine.engine
        authority_before = engine.tracker.authority
        start = len(self.w.spine.records)
        action()
        self.step_records = self.w.spine.records[start:]
        self.step_op = op
        self.step_new_session = new_session
        if not authority_before and engine.tracker.authority and op != "enable_tracker":
            self.violations["I-8"].append("tracker authority rose without the operator")
        self.just_restarted = False
        self._absorb(self.step_records)

    def _midi(
        self,
        port: str,
        kind: str,
        channel: int,
        data1: int,
        data2: int | None,
        automated: bool = False,
    ) -> None:
        self._do(lambda: self.w.runner.midi(port, kind, channel, data1, data2, automated=automated))

    @rule(ms=st.integers(0, 1500))
    def advance(self, ms: int) -> None:
        self._do(lambda: self.w.wait(ms))

    @rule(duplicate=st.booleans())
    def go(self, duplicate: bool) -> None:
        self.w.press  # noqa: B018
        port = "mock_in_switches_h4" if duplicate else "mock_in_switches"
        self._midi(port, "note_on", 16, SWITCH_NOTE["go"], 127)

    @rule(
        action=st.sampled_from(["go", "back", "stop"]), press=st.booleans(), duplicate=st.booleans()
    )
    def switch(self, action: str, press: bool, duplicate: bool) -> None:
        port = "mock_in_switches_h4" if duplicate else "mock_in_switches"
        self._midi(port, "note_on", 16, SWITCH_NOTE[action], 127 if press else 0)
        if action == "stop" and press and not duplicate and not self.w.spine.engine.stopped:
            self.violations["I-18"].append("a STOP press on the authoritative path did not stop")

    @rule(value=st.integers(0, 9), duplicate=st.booleans(), automated=st.booleans())
    def snapshot(self, value: int, duplicate: bool, automated: bool) -> None:
        port = "mock_in_helix_h4" if duplicate else "mock_in_helix"
        self._midi(port, "control_change", 1, 69, value, automated)

    @rule(program=st.integers(0, 3), automated=st.booleans(), duplicate=st.booleans())
    def preset_change(self, program: int, automated: bool, duplicate: bool) -> None:
        port = "mock_in_helix_h4" if duplicate else "mock_in_helix"
        self._midi(port, "program_change", 1, program, None, automated)
        self.last_preset_ns = self.w.spine.engine.now_ns

    @rule(data=st.data())
    def echo(self, data: st.DataObject) -> None:
        sent = [a for a in self.w.attempts if a.bytes_sent]
        if not sent:
            return
        attempt = data.draw(st.sampled_from(sent[-3:]))
        kind, channel, data1, data2 = decode(bytes(attempt.bytes_sent))
        self._midi("mock_in_helix", kind, channel, data1, data2)

    @rule(
        session=st.sampled_from(SESSIONS),
        seq_delta=st.integers(-2, 3),
        section=st.sampled_from(SECTIONS),
        bar=st.integers(1, 2),
        p=st.floats(0.5, 1.0),
        mode=st.sampled_from(["following", "following", "lost"]),
        lag_ms=st.sampled_from([0, 0, 10, 300]),
        downbeat_in_ms=st.integers(0, 2500),
    )
    def tracker(
        self,
        session: str,
        seq_delta: int,
        section: str,
        bar: int,
        p: float,
        mode: str,
        lag_ms: int,
        downbeat_in_ms: int,
    ) -> None:
        seq = max(0, self.seq.get(session, 0) + seq_delta)
        self.seq[session] = max(self.seq.get(session, 0), seq)
        now = self.w.clock.now_ns()
        t_ns = max(0, now - lag_ms * MS)
        state = MusicalState.model_validate(
            {
                "tracker_session": session,
                "seq": seq,
                "t_ns": t_ns,
                "song": {"id": "spine_test", "p": 0.99},
                "mode": mode,
                "hypotheses": [{"section": section, "visit": 1, "bar": bar, "p": p}],
                "beat": {"phase": 0.25, "tempo_bpm": 120.0, "tempo_sd": 1.0},
                "next_downbeat": {"t_ns": t_ns + downbeat_in_ms * MS, "sd_ms": 10.0},
            }
        )
        new_session = bool(self.sessions_seen) and session not in self.sessions_seen
        if session not in self.sessions_seen:
            self.sessions_seen.append(session)
        self._do(
            lambda: self.w.runner.submit(TrackerIn(t_ns=now, state=state)), new_session=new_session
        )

    @rule()
    def confirmed_resume(self) -> None:
        self._do(lambda: self.w.op("confirm_preset_b"), op="confirm_preset_b")
        self._do(lambda: self.w.op("resume"), op="resume")

    @rule(ahead=st.booleans())
    def stop_resume_then_tracker(self, ahead: bool) -> None:
        """STOP, RESUME, re-enable the tracker, then let it report the next section."""
        self.switch("stop", True, False)
        self.confirmed_resume()
        self._do(lambda: self.w.op("set_mode", "assisted"), op="set_mode")
        self.tracker_follows(False, 1, 1500)
        self._do(lambda: self.w.op("enable_tracker"), op="enable_tracker")
        for _ in range(3):
            self.tracker_follows(ahead, 1, 1900)
            self.advance(30)

    @rule()
    def arm(self) -> None:
        self._do(lambda: self.w.op("arm"), op="arm")

    @rule(duplicate=st.booleans())
    def mapped_snapshot(self, duplicate: bool) -> None:
        """The snapshot that the song maps from the current section, if there is one."""
        engine = self.w.spine.engine
        song = self.w.show.songs["spine_test"]
        if engine.visit is None:
            return
        for target in song.successors()[engine.visit.section]:
            section = song.section(target)
            if section is not None and section.advance_on is not None:
                self.snapshot(section.advance_on.snapshot - 1, duplicate, False)
                return

    @rule(ahead=st.booleans(), bar=st.integers(1, 2), downbeat_in_ms=st.integers(50, 1950))
    def tracker_follows(self, ahead: bool, bar: int, downbeat_in_ms: int) -> None:
        """A fresh, confident packet from the current session: the current section, or the next."""
        engine = self.w.spine.engine
        song = self.w.show.songs["spine_test"]
        section = engine.visit.section if engine.visit else "intro"
        if ahead and engine.visit is not None and song.successors()[section]:
            section = song.successors()[section][0]
        session = self.sessions_seen[-1] if self.sessions_seen else SESSIONS[0]
        self.tracker(session, 1, section, bar, 0.97, "following", 0, downbeat_in_ms)

    @rule()
    def assisted_with_tracker(self) -> None:
        """A shortcut to the interesting states: ASSISTED, a fresh packet, authority enabled."""
        self._do(lambda: self.w.op("set_mode", "assisted"), op="set_mode")
        session = self.sessions_seen[-1] if self.sessions_seen else SESSIONS[0]
        self.tracker(session, 1, "verse", 1, 0.95, "following", 0, 100)
        self._do(lambda: self.w.op("enable_tracker"), op="enable_tracker")

    @rule(
        action=st.sampled_from(OPERATOR_ACTIONS),
        mode=st.sampled_from(["manual", "assisted", "auto", "bogus"]),
        song=st.sampled_from(["spine_test", "nope"]),
    )
    def operator(self, action: str, mode: str, song: str) -> None:
        value = mode if action == "set_mode" else song if action == "select_song" else None
        self._do(lambda: self.w.op(action, value), op=action)  # type: ignore[arg-type]

    @rule(
        port=st.sampled_from(["mock_out_mvp", "mock_out_synth", "usb_out", "mock_in_helix"]),
        connected=st.booleans(),
    )
    def port(self, port: str, connected: bool) -> None:
        if port not in {p.id for p in self.w.show.rig.ports}:
            return
        self._do(lambda: self.w.runner.port(port, connected))

    @rule(ms=st.integers(1, 800))
    def clock_jump_back(self, ms: int) -> None:
        clock = self.w.clock
        clock.jump_to(max(0, clock.now_ns() - ms * MS))
        self._do(lambda: self.w.runner.tick())

    @rule(stamp_back_ms=st.integers(1, 300))
    def late_press(self, stamp_back_ms: int) -> None:
        """A press stamped before engine time: delivered late."""
        t_ns = max(0, self.w.clock.now_ns() - stamp_back_ms * MS)
        msg = MidiIn.model_validate(
            {
                "t_ns": t_ns,
                "port": "mock_in_switches",
                "kind": "note_on",
                "channel": 16,
                "data1": 60,
                "data2": 127,
            }
        )
        self._do(lambda: self.w.runner.submit(msg))

    @rule()
    def restart(self) -> None:
        self._check_replay()
        self.pre_restart_ledger |= {
            r.occurrence.key for r in self.w.ledger.records() if isinstance(r, FiredLedgerRecord)
        }
        self.restart_instance_floor = self.w.ledger.next_song_instance("rehearsal")
        self.world = self.w.restart()
        self.segment += 1
        self.last_send.clear()
        self.last_preset_ns = None
        self.stopped = False
        self.awaiting_go = False
        self.confirmed = False
        self.sessions_seen.clear()
        self.seq.clear()
        self.step_records = []
        self.step_op = None
        self.just_restarted = True

    def teardown(self) -> None:
        if self.world is not None:
            self._check_replay()

    def _check_replay(self) -> None:
        w = self.w
        replayed = replay(w.show, w.runner.trace.trace())
        assert replayed.decision_hash() == w.spine.decision_hash(), "I-19: replay diverged"

    # ------------------------------------------------------------------ bookkeeping

    def _absorb(self, records: list[Record]) -> None:
        rig = self.w.show.rig
        song = self.w.show.songs["spine_test"]
        cue_section = {c.id: c.at.section for c in song.cues}
        commit: Record | None = None
        self.performer_devices_this_step = set()
        for r in records:
            kind = r["type"]
            labels: dict[str, str] = {
                "event": f"event:{r.get('detail')}",
                "visit": f"visit:{r.get('via')}",
                "proposal": f"proposal:{r.get('event')}",
                "output_attempt": f"send:{r.get('adapter')}",
                "tracker": f"tracker:{r.get('event')}",
            }
            event(labels.get(str(kind), str(kind)))
            if kind == "event":
                self.events[(self.segment, r["id"])] = r
                if r["provenance"] != "performer_intent" and r["mapped_action"] is not None:
                    self.violations["I-16"].append(
                        f"non-performer event {r['id']} maps to an action"
                    )
                window = self.w.show.rig.timing.init_window_ms * MS
                if (
                    r["detail"] == "snapshot_mapped"
                    and self.last_preset_ns is not None
                    and r["t_ns"] - self.last_preset_ns <= window
                ):
                    self.violations["I-16"].append(
                        f"snapshot {r['id']} inside a preset-load window authorized a GO"
                    )
            elif kind == "planned":
                key = occ_key(r)
                if key in self.planned:
                    self.violations["I-6"].append(f"occurrence {key} planned twice")
                if r["occurrence"]["song_instance"] < self.restart_instance_floor:
                    self.violations["I-6"].append(f"occurrence {key} reuses a pre-restart instance")
                self.planned.add(key)
            elif kind == "device" and r["set_by"] == "performer":
                self.performer_devices_this_step.add(r["device"])
                if r["visit"] is not None:
                    self.overrides[(r["song_instance"], r["section"], r["visit"])].add(r["device"])
            elif kind == "visit" and r["via"] == "go":
                self._check_authorization(r["authorized_by"], f"visit {r['section']}")
                self.awaiting_go = False
            elif kind == "proposal" and r["event"] == "authorized":
                self._check_authorization(r["event_id"], "proposal")
            elif kind == "commit":
                commit = r
                if not r["resync"]:
                    self.commits[occ_key(r)] = r
                if r["class"] == "critical":
                    auth = r["authorized_by"]
                    if auth is None:
                        self.violations["I-14"].append(
                            f"critical commit {occ_key(r)} without authorization"
                        )
                    else:
                        self._check_authorization(
                            auth["event_id"], f"critical {occ_key(r)}", invariant="I-14"
                        )
            elif kind == "stop":
                self.stopped = True
                self.confirmed = False
                self.awaiting_go = False
            elif kind == "operator" and r["action"] == "confirm_preset_b" and self.stopped:
                self.confirmed = True
            elif kind == "resume":
                if not (self.stopped and self.confirmed and self.step_op == "resume"):
                    self.violations["I-18"].append(
                        "RESUME without STOP, confirmation and a deliberate action"
                    )
                self.stopped = False
                self.confirmed = False
                self.awaiting_go = True
            elif kind == "output_attempt":
                # A deferred send (ADR-017) goes out on a later step than its commit.
                owner = commit if commit and occ_key(commit) == occ_key(r) else None
                owner = owner or self.commits.get(occ_key(r))
                assert owner is not None, "every attempt belongs to a commit"
                self._check_attempt(r, owner, rig, cue_section)
            elif kind == "deferred":
                deferred_window = self.commits[occ_key(r)]["window_end_ns"]
                if r["send_at_ns"] > deferred_window:
                    self.violations["I-4"].append(f"{occ_key(r)} deferred past its window")

    def _check_authorization(
        self, event_id: str | None, what: str, invariant: str = "I-16"
    ) -> None:
        event = self.events.get((self.segment, event_id or ""))
        if (
            event is None
            or event["provenance"] != "performer_intent"
            or event["mapped_action"] != "go"
            or event["detail"] not in AUTHORIZING_DETAILS
        ):
            self.violations[invariant].append(
                f"{what} authorized by a non-performer event {event_id}"
            )

    def _check_attempt(
        self, r: Record, commit: Record, rig: Any, cue_section: dict[str, str]
    ) -> None:
        key = occ_key(r)
        command_key: str = r["command"]
        device_id, name = command_key.split(".")
        device = rig.device(device_id)
        command = device.commands[name]
        port = next(p for p in rig.ports if p.id == command.port)
        resync = commit["resync"]
        t = r["send_time_ns"]

        if r["adapter"] != "mock":
            ok = (
                rig.profile == "physical"
                and port.transport != "mock"
                and command.status == "bench_verified"
                and command.values[r["value"]] is not None
            )
            if not ok:
                self.violations["I-1"].append(f"{command_key} reached {r['adapter']} unverified")
            if not r["armed"]:
                self.violations["I-2"].append(f"{command_key} reached {r['adapter']} unarmed")
        if self.stopped or self.awaiting_go:
            self.violations["I-18"].append(
                f"{key} sent while stopped or before the GO after RESUME"
            )
        if not resync:
            if t > commit["window_end_ns"]:
                self.violations["I-4"].append(f"{key} sent after its on_miss window")
            if key in self.pre_restart_ledger:
                self.violations["I-5"].append(f"{key} resent after a restart")
            self.sent[(key, command_key)] += 1
            if self.sent[(key, command_key)] > 1:
                self.violations["I-7"].append(f"{key} {command_key} sent twice")
            o = r["occurrence"]
            visit_key = (o["song_instance"], cue_section[o["cue_id"]], o["section_visit"])
            if device_id in self.overrides.get(visit_key, set()):
                self.violations["I-12"].append(f"{key} fought the performer on {device_id}")
        elif command.type != "state":
            self.violations["I-15"].append(f"one-shot {command_key} resent")
        if r["bytes_sent"]:
            _, channel, _, _ = decode(bytes(r["bytes_sent"]))
            if channel != command.channel:
                self.violations["I-10"].append(f"{command_key} on wire channel {channel}")
        for other in rig.devices:
            for listen in other.receives:
                if (listen.port, listen.channel) != (command.port, command.channel):
                    continue
                wanted = "intended" if other.id == device_id else "ignores"
                if listen.behavior != wanted:
                    self.violations["I-13"].append(f"{command_key} reaches {other.id}")
        if commit["class"] == "critical" and commit["authorized_by"] is None:
            self.violations["I-14"].append(f"critical {key} sent without authorization")
        interval = command.min_interval_ms * MS
        last = self.last_send.get(device_id)
        if last is not None and t - last[0] < max(interval, last[1]):
            self.violations["I-15"].append(f"device {device_id} sent to faster than its limit")
        self.last_send[device_id] = (t, interval)
        if (
            not r["reason"]
            or not r["config_hash"]
            or r["result"] not in ("attempted", "confirmed", "failed", "unknown")
        ):
            self.violations["I-11"].append(f"{key} logged incompletely")

    # ------------------------------------------------------------------ invariants

    def _clean(self, invariant: str) -> None:
        assert not self.violations[invariant], f"{invariant}: {self.violations[invariant][:3]}"

    @invariant()
    def i01_unverified_mapping_reaches_mock_only(self) -> None:
        self._clean("I-1")

    @invariant()
    def i02_no_physical_output_unarmed_and_arming_never_persists(self) -> None:
        self._clean("I-2")
        if self.just_restarted:
            assert not self.w.spine.engine.armed

    @invariant()
    def i03_manual_action_cancels_pending_automation(self) -> None:
        for device in self.performer_devices_this_step:
            for item in self.w.spine.engine.scheduler.pending():
                assert all(k.split(".")[0] != device for k in item.intents), (
                    f"I-3: {device} still pending"
                )

    @invariant()
    def i04_expired_cues_discarded(self) -> None:
        self._clean("I-4")

    @invariant()
    def i05_no_resend_after_restart_and_no_past_work(self) -> None:
        self._clean("I-5")
        if self.just_restarted:
            engine = self.w.spine.engine
            assert engine.scheduler.pending() == [] and engine.song is None

    @invariant()
    def i06_identity_is_the_occurrence(self) -> None:
        self._clean("I-6")
        assert "generation" not in OccurrenceId.model_fields

    @invariant()
    def i07_at_most_one_output_per_occurrence(self) -> None:
        self._clean("I-7")

    @invariant()
    def i08_lost_or_stale_input_never_raises_authority(self) -> None:
        self._clean("I-8")
        engine = self.w.spine.engine
        if self.just_restarted or self.step_new_session:
            assert not engine.tracker.authority
        if engine.tracker.authority:
            assert self.step_op == "enable_tracker" or engine._tracker_fresh()  # pyright: ignore[reportPrivateUsage]
            state = engine.tracker.state
            assert state is not None and state.mode == "following", (
                "I-8: authority survived a non-following tracker state"
            )

    @invariant()
    def i09_no_future_reads(self) -> None:
        engine = self.w.spine.engine
        assert engine.now_ns <= max(engine.now_ns, self.w.clock.now_ns())
        inputs = self.w.runner.trace.trace().inputs
        if inputs:
            assert engine.now_ns <= max(i.t_ns for i in inputs), (
                "I-9: engine time ran ahead of its inputs"
            )

    @invariant()
    def i10_channel_conversion_in_one_function(self) -> None:
        self._clean("I-10")

    @invariant()
    def i11_every_output_attempt_logged(self) -> None:
        self._clean("I-11")
        logged = [r for r in self.w.runner.log.records if r["type"] == "output_attempt"]
        assert len(logged) == len(self.w.spine.attempts)
        assert all(r["run_id"] and r["config_hash"] == self.w.spine.config_hash for r in logged)

    @invariant()
    def i12_performer_manual_changes_win(self) -> None:
        self._clean("I-12")

    @invariant()
    def i13_every_recipient_known_safe(self) -> None:
        self._clean("I-13")

    @invariant()
    def i14_no_critical_cue_on_tracker_evidence_alone(self) -> None:
        self._clean("I-14")

    @invariant()
    def i15_rate_limits_and_state_vs_one_shot(self) -> None:
        self._clean("I-15")

    @invariant()
    def i16_only_performer_intent_authorizes(self) -> None:
        self._clean("I-16")

    @invariant()
    def i18_stop_cancels_everything_resume_is_deliberate(self) -> None:
        self._clean("I-18")
        engine = self.w.spine.engine
        if engine.stopped:
            assert engine.scheduler.pending() == [] and engine.proposal is None
            assert not engine.tracker.authority
        if engine.awaiting_go:
            assert engine.scheduler.pending() == [], "I-18: work planned before the GO after RESUME"


TestSpineInvariants = SpineMachine.TestCase  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
