"""Engine core: the deterministic step function (ADR-014, plan Section 4.1).

``Engine.step(input) -> effects``. The core reads time only from the input it is given (the
wrapper stamps every input from the injected clock), does no I/O and uses no randomness. Its
effects are notes (decisions, refusals and state changes, which are logged and hashed) and
commits, which the output supervisor executes. Live runs and replay drive the same code.

What the core does (T-04):

- **Song selection** by a preset report's Program Change mapped in the setlist, or by the HUD.
  Selecting a song starts a new song instance, persisted in the fired ledger before it is used
  (ADR-016), and authorizes nothing; the first GO enters the first section.
- **GO** from a press-gesture switch or a mapped snapshot, read through the device's report
  record (Section 5.4, ADR-015), enters the next section as a new visit. Its occurrences are
  authorized by that GO. A critical cue takes the authorization only if its
  ``requires.corroborate_by`` lists the kind of action (footswitch or mapped snapshot) and the
  cue lies within ``requires.within_bars``. A GO made by a device's own snapshot press leaves
  that device's cues to the performer (Section 8, stage 1).
- **GO timing** (Section 8, ``control/go_timing.py``): class F with no lock, L within the late
  window after a downbeat, otherwise Q or D per entry cue against the next downbeat. D follows
  ``on_miss`` and is never deferred a bar. Later cues are counted in beats from the anchor.
- **Gestures** (Section 5.3, ``control/gestures.py``): a tap on the BACK/MANUAL switch is BACK,
  recognized on release; a hold is MANUAL, recognized when it reaches ``hold_ms``. Neither can
  issue a GO (I-17). BACK before anything from the last GO was sent cancels that GO and restores
  the previous visit; after a send it re-enters the previous section as a new visit. MANUAL
  (from M3) turns tracker authority off; a second hold turns it back on only if the performer's
  own hold turned it off. Authority the system suspended needs the operator (ADR-006).
- **STOP** cancels all pending work by starting a new generation and suspends tracker authority.
  **RESUME** is a deliberate HUD action after the operator confirms H4 preset B; it releases
  nothing stale, treats device state as unknown, and holds an explicit "awaiting GO" state:
  until the next authorized GO the tracker cannot move the engine, resync is refused and
  nothing fires.
- **Tracker packets** are filtered by session and sequence (ADR-006). A new session, a stale
  stream, or a packet whose mode is not ``following`` suspends tracker authority and drops to
  MANUAL; only the operator re-enables it (I-8). Only a fresh, following packet about the
  selected song counts as evidence.
  In ASSISTED or AUTO, with authority, from M4, and only while the tracker reports the selected
  song, the tracker may relocalize within a visit (a new generation, no new occurrences) and
  enter a successor section as an unauthorized visit. A critical cue there is only proposed
  ("ask"); a later GO press authorizes the single armed proposal (Section 5.4). A hypothesis in
  the current section is treated as the current visit: tracker-detected repeats and extensions
  arrive with M4's EXTEND work.
- **Device state.** A performer change on a device (a snapshot or preset change) updates the
  copy, cancels pending automation for that device and wins for the rest of the visit (I-3,
  I-12). Echoes and automated recalls update the copy only.

EXTEND has no effect yet; M4 brings it.
"""

from dataclasses import dataclass, field
from typing import Any, Literal

from roobotto.config.song import Cue, Song
from roobotto.control.gestures import Gesture, Gestures
from roobotto.control.go_timing import (
    GoAnchor,
    GoClass,
    beat_ns_for,
    entry_timing,
    go_anchor,
    go_class,
    miss_window_end,
)
from roobotto.cues.gate import DeviceView, GateInput, Mode, decide
from roobotto.cues.scheduler import ScheduledCue, Scheduler
from roobotto.cues.supervisor import Commit, Intent
from roobotto.engine.inputs import EngineInput, MidiIn, OperatorIn, PortStatus, TrackerIn
from roobotto.engine.provenance import (
    Classified,
    ProvenanceContext,
    SentWire,
    classify,
    preset_load_source,
)
from roobotto.engine.show import ShowConfig
from roobotto.messages import Authorization, CueClass, MusicalState, OccurrenceId, OutputAttempt

NS_PER_MS = 1_000_000
NS_PER_MINUTE = 60_000_000_000
MODES: tuple[Mode, ...] = ("manual", "assisted", "auto")


@dataclass(frozen=True)
class Note:
    kind: str
    t_ns: int
    data: dict[str, Any]

    def as_record(self) -> dict[str, Any]:
        return {"type": self.kind, "t_ns": self.t_ns, **self.data}


@dataclass(frozen=True)
class InstanceStart:
    """A song instance is starting: the wrapper persists it before anything else (ADR-016)."""

    performance: str
    song_instance: int
    song: str


Effect = Note | Commit | InstanceStart


@dataclass
class Visit:
    section: str
    number: int
    anchor_ns: int
    """The predicted downbeat of the visit's first bar."""
    beat_ns: int
    authorization: Authorization | None
    corroboration: str | None
    go_kind: Literal["F", "L", "next"] | None
    """How a GO or BACK placed the first downbeat; ``None`` for a tracker-entered visit."""
    go_ns: int | None
    """When the GO or BACK that created the visit was authorized."""
    excluded: frozenset[str] = frozenset()
    """Devices whose cues stay with the performer in this visit (stage 1 snapshot GO)."""
    overridden: set[str] = field(default_factory=lambda: set[str]())
    """Devices the performer changed during this visit; they win until the next visit."""
    committed: bool = False
    """Something planned in this visit has been committed: a send is irreversible (5.3)."""


@dataclass
class LastGo:
    """What BACK needs to undo the most recent GO (Section 5.3)."""

    event_id: str
    visit: Visit
    previous: Visit | None
    suspended: list[ScheduledCue]
    """The previous visit's pending cues, cancelled when the GO entered."""


@dataclass
class TrackerView:
    session: str | None = None
    seq: int = -1
    retired: set[str] = field(default_factory=lambda: set[str]())
    state: MusicalState | None = None
    rx_ns: int | None = None
    authority: bool = False


@dataclass(frozen=True)
class Proposal:
    key: str
    armed_ns: int


class Engine:
    def __init__(self, show: ShowConfig, *, next_song_instance: int = 0) -> None:
        self.show = show
        self.settings = show.settings
        self.rig = show.rig
        timing = show.rig.timing
        self._echo_ns = timing.echo_window_ms * NS_PER_MS
        self._init_ns = timing.init_window_ms * NS_PER_MS
        self._margin_ns = round(timing.send_margin_ms * NS_PER_MS)
        self._stale_ns = self.settings.tracker_stale_ms * NS_PER_MS
        self.now_ns = 0
        self._started = False
        self._event_seq = 0
        self.mode: Mode = "manual"
        self.stopped = False
        self.armed = False
        self.preset_b_confirmed = False
        self.awaiting_go = False
        """Set by RESUME: nothing may be sent until the next authorized GO (I-18)."""
        self.tracker = TrackerView()
        self.song: Song | None = None
        self.song_instance: int | None = None
        self._next_instance = next_song_instance
        self.visit: Visit | None = None
        self._visit_counts: dict[str, int] = {}
        self.devices: dict[str, dict[str, str | None]] = {
            d.id: dict.fromkeys(d.commands) for d in self.rig.devices
        }
        self.scheduler = Scheduler()
        self.proposal: Proposal | None = None
        self._last_program_ns: dict[str, int] = {}
        self._recent_sends: list[SentWire] = []
        self._last_state: dict[str, tuple[OccurrenceId, str, CueClass]] = {}
        self._last_ignored: str | None = None
        self.gestures = Gestures(self.rig)
        self._history: list[Visit] = []
        self._last_go: LastGo | None = None
        self._authority_off_by_performer = False
        self._mode_before_manual: Mode = "manual"
        self._linked: dict[str, frozenset[str]] = {
            src.id: frozenset(d for d, r in self.rig.reports() if r.source == src.id)
            for src in self.rig.inputs
        }
        """Source id -> the devices that report through it (ADR-015)."""

    # ----------------------------------------------------------------------------- step

    def step(self, inp: EngineInput) -> list[Effect]:
        out: list[Effect] = []
        if self._started and inp.t_ns < self.now_ns:
            self._note(
                out,
                "clock",
                reason="input stamped earlier than engine time; engine time never moves back",
                input_t_ns=inp.t_ns,
            )
        else:
            self.now_ns = inp.t_ns
        self._started = True
        self._watchdog(out)
        for gesture in self.gestures.due(self.now_ns):
            self._on_gesture(gesture, out)
        if isinstance(inp, MidiIn):
            self._on_midi(inp, out)
        elif isinstance(inp, TrackerIn):
            self._on_tracker(inp.state, out)
        elif isinstance(inp, OperatorIn):
            self._on_operator(inp, out)
        elif isinstance(inp, PortStatus):
            self._on_port(inp, out)
        self._watchdog(out)  # a stale packet must not leave authority on for a moment longer
        self._run_due(out)
        return out

    def next_wakeup_ns(self) -> int | None:
        """When the wrapper must tick next: the earliest send deadline, the end of a held
        item's window, or the moment the tracker stream goes stale. ``None`` if nothing waits."""
        times: list[int] = []
        for item in self.scheduler.pending():
            if item.lifecycle != "scheduled" or item.generation != self.scheduler.generation:
                continue
            times.append(
                item.send_at_ns if item.send_at_ns > self.now_ns else item.window_end_ns + 1
            )
        if self.tracker.authority and self.tracker.rx_ns is not None:
            times.append(self.tracker.rx_ns + self._stale_ns + 1)
        hold = self.gestures.next_wakeup_ns()
        if hold is not None:
            times.append(hold)
        future = [t for t in times if t > self.now_ns]
        return min(future) if future else None

    def on_commit_result(self, commit: Commit, attempts: list[OutputAttempt]) -> None:
        """Feedback from the supervisor: what was attempted for a commit made this step."""
        if not commit.resync:
            self.scheduler.mark(commit.occurrence.key, "sent" if attempts else "cancelled")
        self.on_attempts(commit, attempts)

    def on_attempts(self, commit: Commit, attempts: list[OutputAttempt]) -> None:
        """Feedback for sends, including deferred ones that went out on a later step."""
        ports = {p.id: p for p in self.rig.ports}
        for attempt in attempts:
            device = self.rig.device(attempt.device)
            name = attempt.command.split(".")[1]
            if device is None or name not in device.commands:
                continue
            command = device.commands[name]
            port = ports.get(command.port)
            reached = attempt.adapter == "midi" or (port is not None and port.transport == "mock")
            if not reached:
                continue
            wire = command.values.get(attempt.value)
            if wire is not None:
                self._recent_sends.append(
                    SentWire(
                        self.now_ns, device.id, wire.kind, command.channel, wire.data1, wire.data2
                    )
                )
            self.devices[device.id][name] = attempt.value
            if command.type == "state" and not commit.resync and commit.cue_class != "critical":
                self._last_state[attempt.command] = (
                    commit.occurrence,
                    attempt.value,
                    commit.cue_class,
                )
        self._recent_sends = [
            s for s in self._recent_sends if self.now_ns - s.t_ns <= self._echo_ns
        ]

    # ------------------------------------------------------------------------- helpers

    def _note(self, out: list[Effect], kind: str, /, **data: Any) -> None:
        out.append(Note(kind, self.now_ns, data))

    def _refuse(
        self, out: list[Effect], action: str, reason: str, event_id: str | None = None
    ) -> None:
        self._note(out, "action_refused", action=action, event_id=event_id, reason=reason)

    def _tracker_fresh(self) -> bool:
        tr = self.tracker
        return (
            tr.state is not None
            and tr.rx_ns is not None
            and self.now_ns - tr.rx_ns <= self._stale_ns
            and self.now_ns - tr.state.t_ns <= self._stale_ns
        )

    def _tracker_usable(self) -> bool:
        """Fresh, following, and about the selected song: the only tracker state that may count
        as evidence for firing (I-8)."""
        state = self.tracker.state
        return (
            self._tracker_fresh()
            and state is not None
            and state.mode == "following"
            and self.song is not None
            and state.song.id == self.song.id
        )

    def _suspend_tracker(self, out: list[Effect], why: str) -> None:
        self.tracker.authority = False
        self._authority_off_by_performer = False
        self.mode = "manual"
        self._note(
            out,
            "tracker",
            event="authority_suspended",
            reason=f"{why}: tracker authority suspended and MANUAL until the operator "
            "re-enables it (ADR-006, I-8)",
        )

    def _watchdog(self, out: list[Effect]) -> None:
        if self.tracker.authority and not self._tracker_fresh():
            self._suspend_tracker(out, "stale tracker stream")

    def _tempo_beat_ns(self, song: Song, state: MusicalState | None) -> int:
        return beat_ns_for(song, state)

    # ---------------------------------------------------------------------------- MIDI

    def _mapped_targets(self) -> dict[int, str]:
        song = self.song
        if song is None:
            return {}
        if self.visit is None:
            successors = [song.sections[0].id]
        else:
            successors = song.successors()[self.visit.section]
        targets: dict[int, str] = {}
        for section_id in successors:
            section = song.section(section_id)
            if section is not None and section.advance_on is not None:
                targets[section.advance_on.snapshot] = section_id
        return targets

    def _on_midi(self, msg: MidiIn, out: list[Effect]) -> None:
        self._event_seq += 1
        ctx = ProvenanceContext(
            rig=self.rig,
            now_ns=self.now_ns,
            init_window_ns=self._init_ns,
            echo_window_ns=self._echo_ns,
            last_program_ns=dict(self._last_program_ns),
            recent_sends=tuple(self._recent_sends),
            mapped_targets=self._mapped_targets(),
        )
        c = classify(msg, f"e{self._event_seq}", ctx)
        ev = c.event
        preset_source = preset_load_source(self.rig, msg)
        if preset_source is not None:
            self._last_program_ns[preset_source] = self.now_ns
        self._note(
            out,
            "event",
            id=ev.id,
            source=ev.source,
            path=ev.path,
            kind=ev.kind,
            channel=ev.channel,
            data1=ev.data1,
            data2=ev.data2,
            available_ns=ev.available_ns,
            provenance=ev.provenance,
            mapped_action=ev.mapped_action,
            detail=c.detail,
            reason=c.reason,
        )
        linked = self._linked.get(ev.source, frozenset())
        reporter = frozenset({c.device}) if c.device else frozenset[str]()
        if c.detail == "program_change":
            self._performer_changed(reporter, None, "preset change", out)
            if c.program is not None:
                self._select_by_program(c.program, out)
        elif c.detail in (
            "snapshot_preset_load",
            "snapshot_unmapped",
            "snapshot_unverified",
            "snapshot_mapped",
        ):
            self._performer_changed(reporter, msg, "snapshot change", out)
        elif c.detail in ("echo", "automated"):
            self._observe(linked, msg)

        if (
            ev.provenance == "performer_intent"
            and ev.mapped_action is None
            and c.switch is not None
            and self.gestures.handles(c.switch)
        ):
            self._switch_edge(c, out)
            return
        if ev.provenance != "performer_intent" or ev.mapped_action is None:
            return
        if ev.mapped_action == "go":
            self._go(c, out)
        elif ev.mapped_action == "stop":
            self._stop(out, f"STOP press '{ev.id}'")
        else:
            self._refuse(out, ev.mapped_action, "no effect: EXTEND arrives with M4", ev.id)

    # ------------------------------------------------------------------- gestures

    def _switch_edge(self, c: Classified, out: list[Effect]) -> None:
        """A raw edge of a tap/hold switch (Section 5.3)."""
        ev = c.event
        assert c.switch is not None
        if c.detail == "switch_press":
            note = self.gestures.press(c.switch, ev.available_ns, ev.id)
            if note is not None:
                self._note(out, "gesture", event="restarted", switch=c.switch, reason=note)
            return
        gesture, reason = self.gestures.release(c.switch, ev.available_ns, ev.id)
        if gesture is None:
            self._note(out, "gesture", event="none", switch=c.switch, reason=reason)
            return
        self._on_gesture(gesture, out)

    def _on_gesture(self, g: Gesture, out: list[Effect]) -> None:
        self._note(
            out,
            "gesture",
            event="recognized",
            action=g.action,
            switch=g.switch,
            event_id=g.event_id,
            pressed_ns=g.pressed_ns,
            held_dur_ns=g.at_ns - g.pressed_ns,
        )
        if g.action == "back":
            self._back(g, out)
        elif g.action == "manual":
            self._manual(g, out)
        else:
            self._refuse(out, g.action, "not a tap or hold action", g.event_id)

    def _observe(self, devices: frozenset[str], msg: MidiIn | None) -> None:
        for device_id in sorted(devices):
            device = self.rig.device(device_id)
            if device is None:
                continue
            copy = self.devices[device_id]
            matched = False
            if msg is not None:
                for name, command in device.commands.items():
                    for value, wire in command.values.items():
                        if (
                            wire is not None
                            and command.channel == msg.channel
                            and (wire.kind, wire.data1, wire.data2)
                            == (msg.kind, msg.data1, msg.data2)
                        ):
                            copy[name] = value
                            matched = True
            if not matched:
                for name in copy:
                    copy[name] = None

    def _performer_changed(
        self, devices: frozenset[str], msg: MidiIn | None, what: str, out: list[Effect]
    ) -> None:
        self._observe(devices, msg)
        for device_id in sorted(devices):
            if self.visit is not None:
                self.visit.overridden.add(device_id)
            for item, removed in self.scheduler.cancel_device(device_id):
                self._note(
                    out,
                    "cancel",
                    occurrence=item.occurrence.model_dump(mode="json"),
                    intents=removed,
                    generation=self.scheduler.generation,
                    reason=f"performer {what} on '{device_id}': a manual action cancels pending "
                    "automation for that device (I-3, I-12)",
                )
            self._note(
                out,
                "device",
                device=device_id,
                state=dict(self.devices.get(device_id, {})),
                set_by="performer",
                song_instance=self.song_instance,
                section=self.visit.section if self.visit else None,
                visit=self.visit.number if self.visit else None,
            )

    # ----------------------------------------------------------------------- songs

    def _select_by_program(self, program: int, out: list[Effect]) -> None:
        setlist = self.show.setlist
        entry = (
            next((e for e in setlist.entries if e.program_change == program), None)
            if setlist
            else None
        )
        if entry is None:
            self._note(
                out,
                "song",
                event="ignored",
                reason=f"program change {program} selects no setlist entry",
            )
            return
        self._select_song(entry.song, f"program change {program}", out)

    def _select_song(self, song_id: str, via: str, out: list[Effect]) -> None:
        song = self.show.songs.get(song_id)
        if song is None:
            self._refuse(out, "select_song", f"unknown song '{song_id}'")
            return
        self._cancel_all("song changed", out)
        self.song = song
        self.song_instance = self._next_instance
        self._next_instance += 1
        out.append(InstanceStart(self.settings.performance, self.song_instance, song.id))
        self.visit = None
        self._visit_counts = {}
        self._history = []
        self._last_go = None
        self.proposal = None
        self._note(
            out, "song", event="selected", song=song.id, instance=self.song_instance, via=via
        )

    def _next_song(self, out: list[Effect]) -> None:
        setlist = self.show.setlist
        if setlist is None:
            self._refuse(out, "next_song", "no setlist was given")
            return
        ids = [e.song for e in setlist.entries]
        if self.song is None or self.song.id not in ids:
            target = ids[0]
        else:
            index = ids.index(self.song.id) + 1
            if index >= len(ids):
                self._refuse(out, "next_song", "the setlist has ended")
                return
            target = ids[index]
        self._select_song(target, "HUD next song", out)

    def _next_section(self) -> str | None:
        song = self.song
        assert song is not None
        if self.visit is None:
            return song.sections[0].id
        ids = [s.id for s in song.sections]
        index = ids.index(self.visit.section)
        if song.sections[index].ends or index + 1 >= len(ids):
            return None
        return ids[index + 1]

    # ------------------------------------------------------------------------- GO

    def _go(self, c: Classified, out: list[Effect]) -> None:
        ev = c.event
        if self.stopped:
            self._refuse(out, "go", "STOP is active; RESUME first (I-18)", ev.id)
            return
        if self.song is None:
            self._refuse(out, "go", "no song selected", ev.id)
            return
        if self.proposal is not None and self._authorize_proposal(c, out):
            return
        target = c.target or self._next_section()
        if target is None:
            self._refuse(out, "go", "the song has ended; nothing to advance to", ev.id)
            return
        corroboration = "mapped_snapshot" if c.detail == "snapshot_mapped" else "footswitch"
        excluded = (
            frozenset({c.device})
            if corroboration == "mapped_snapshot" and c.device
            else frozenset[str]()
        )
        song = self.song
        state = self.tracker.state if self._tracker_usable() else None
        anchor = go_anchor(ev.available_ns, state, song, self.rig.timing.late_window_bars)
        previous = self.visit
        suspended = self._enter(
            target,
            anchor=anchor,
            authorization=Authorization(event_id=ev.id, action="go"),
            corroboration=corroboration,
            excluded=excluded,
            via="go",
            out=out,
        )
        visit = self.visit
        assert visit is not None
        self._last_go = LastGo(ev.id, visit, previous, suspended)
        entries = [
            i.timing_class
            for i in self.scheduler.pending()
            if i.timing_class is not None and i.cue.at.section == visit.section
        ]
        self._note(
            out,
            "go",
            event_id=ev.id,
            press_ns=ev.available_ns,
            timing_class=go_class(entries, anchor),
            anchor_ns=anchor.anchor_ns,
            section=visit.section,
            visit=visit.number,
            reason="GO record: press time and class; send times are in the output attempts "
            "it authorized (Section 8)",
        )

    def _authorize_proposal(self, c: Classified, out: list[Effect]) -> bool:
        proposal = self.proposal
        assert proposal is not None
        item = self.scheduler.get(proposal.key)
        if item is None or item.lifecycle != "scheduled":
            self.proposal = None
            return False
        ev = c.event
        if ev.available_ns <= proposal.armed_ns:
            self._note(
                out,
                "proposal",
                event="not_authorized",
                occurrence=item.occurrence.model_dump(mode="json"),
                event_id=ev.id,
                reason="the press was made before the proposal was armed; it never authorizes "
                "it (Section 5.4)",
            )
            return False
        corroboration = "mapped_snapshot" if c.detail == "snapshot_mapped" else "footswitch"
        accepted = item.cue.requires.corroborate_by if item.cue.requires else []
        if corroboration not in accepted:
            self._note(
                out,
                "proposal",
                event="not_authorized",
                occurrence=item.occurrence.model_dump(mode="json"),
                event_id=ev.id,
                reason=f"the cue does not accept a {corroboration} action as authorization",
            )
            return False
        item.authorization = Authorization(event_id=ev.id, action="go")
        item.corroboration = corroboration
        self.proposal = None
        self._note(
            out,
            "proposal",
            event="authorized",
            occurrence=item.occurrence.model_dump(mode="json"),
            event_id=ev.id,
            reason="the single proposal armed when the press happened (Section 5.4)",
        )
        return True

    def _cancel_all(self, why: str, out: list[Effect]) -> list[ScheduledCue]:
        cancelled = self.scheduler.cancel_all()
        for item in cancelled:
            self._note(
                out,
                "cancel",
                occurrence=item.occurrence.model_dump(mode="json"),
                intents=sorted(item.intents),
                generation=self.scheduler.generation,
                reason=why,
            )
        return cancelled

    def _enter(
        self,
        section: str,
        *,
        anchor: GoAnchor | None = None,
        tracker_anchor: tuple[int, int] | None = None,
        authorization: Authorization | None,
        corroboration: str | None,
        excluded: frozenset[str],
        via: Literal["go", "tracker", "back"],
        out: list[Effect],
        remember: bool = True,
    ) -> list[ScheduledCue]:
        """Enter ``section`` as a new visit. Returns the pending cues it cancelled.

        A GO or BACK passes its ``anchor``; the tracker passes ``(anchor_ns, beat_ns)``.
        """
        song = self.song
        assert song is not None
        if via == "go":
            self.awaiting_go = False
        left = self._cancel_all("left the section: pending work cancelled", out)
        if remember and self.visit is not None:
            self._history = [*self._history[-31:], self.visit]
        self._last_go = None
        number = self._visit_counts.get(section, 0) + 1
        self._visit_counts[section] = number
        if anchor is not None:
            anchor_ns, beat_ns = anchor.anchor_ns, anchor.beat_ns
            go_kind, go_ns = anchor.kind, anchor.go_ns
        else:
            assert tracker_anchor is not None
            anchor_ns, beat_ns = tracker_anchor
            go_kind, go_ns = None, None
        self.visit = Visit(
            section,
            number,
            anchor_ns,
            beat_ns,
            authorization,
            corroboration,
            go_kind,
            go_ns,
            excluded,
        )
        self.proposal = None
        self._last_ignored = None
        self._note(
            out,
            "visit",
            section=section,
            visit=number,
            via=via,
            authorized_by=authorization.event_id if authorization else None,
            go_kind=go_kind,
            anchor_ns=anchor_ns,
            beat_dur_ns=beat_ns,
            generation=self.scheduler.generation,
        )
        for cue in song.cues:
            if cue.at.section == section:
                self._plan(cue, out)
        return left

    # ------------------------------------------------------------------- scheduling

    def _cue_beats(self, cue: Cue) -> float:
        song = self.song
        assert song is not None
        return (cue.at.bar - 1) * song.beats_per_bar + (cue.at.beat - 1) + cue.at.offset_beats

    def _authorization_for(self, cue: Cue, beats: float) -> Authorization | None:
        visit = self.visit
        song = self.song
        assert visit is not None and song is not None
        if visit.authorization is None:
            return None
        if cue.cue_class != "critical":
            return visit.authorization
        req = cue.requires
        if req is None or visit.corroboration not in req.corroborate_by:
            return None
        if req.within_bars is not None and beats >= req.within_bars * song.beats_per_bar:
            return None
        return visit.authorization

    def _plan(self, cue: Cue, out: list[Effect]) -> None:
        visit = self.visit
        assert visit is not None and self.song_instance is not None
        occurrence = OccurrenceId(
            performance=self.settings.performance,
            song_instance=self.song_instance,
            section_visit=visit.number,
            cue_id=cue.id,
        )
        intents = {k: v for k, v in cue.state.items() if k.split(".")[0] not in visit.excluded}
        if not intents:
            self._note(
                out,
                "decision",
                kind="abstain",
                occurrence=occurrence.model_dump(mode="json"),
                reason="every intent targets the device whose own snapshot press made this GO; "
                "its cues stay with the performer (Section 8, stage 1)",
            )
            return
        beats = self._cue_beats(cue)
        authorization = self._authorization_for(cue, beats)
        item = self.scheduler.prepare(
            occurrence,
            cue,
            intents,
            authorization,
            visit.corroboration if authorization else None,
        )
        if item is None:
            self._note(
                out,
                "decision",
                kind="abstain",
                occurrence=occurrence.model_dump(mode="json"),
                reason="occurrence already planned or finished: at most once (I-7)",
            )
            return
        self._time(item)
        self._note(
            out,
            "planned",
            occurrence=occurrence.model_dump(mode="json"),
            cue_class=cue.cue_class,
            intents=sorted(f"{k}={v}" for k, v in item.intents.items()),
            due_ns=item.due_ns,
            send_at_ns=item.send_at_ns,
            window_end_ns=item.window_end_ns,
            timing_class=item.timing_class,
            generation=item.generation,
            authorized_by=authorization.event_id if authorization else None,
        )

    def _lead_ns(self, intents: dict[str, str]) -> int:
        latency = 0
        for key in intents:
            device_id, name = key.split(".")
            device = self.rig.device(device_id)
            command = device.commands.get(name) if device else None
            if command is not None and command.latency_ms:
                latency = max(latency, round(command.latency_ms * NS_PER_MS))
        return latency + self._margin_ns

    def _time(self, item: ScheduledCue) -> None:
        visit = self.visit
        song = self.song
        assert visit is not None and song is not None
        beats = self._cue_beats(item.cue)
        lead = self._lead_ns(item.intents)
        due = visit.anchor_ns + round(beats * visit.beat_ns)
        send_at = due - lead
        timing_class: GoClass | None = None
        if beats == 0 and visit.go_kind is not None and visit.go_ns is not None:
            entry = entry_timing(
                GoAnchor(visit.go_kind, visit.go_ns, visit.anchor_ns, visit.beat_ns), lead
            )
            timing_class, due, send_at = entry.timing_class, entry.due_ns, entry.send_at_ns
        window_end = miss_window_end(
            item.cue.on_miss,
            due_ns=due,
            send_at_ns=send_at,
            beat_ns=visit.beat_ns,
            beats_per_bar=song.beats_per_bar,
            late_window_bars=self.rig.timing.late_window_bars,
            margin_ns=self._margin_ns,
        )
        self.scheduler.schedule(
            item,
            due_ns=due,
            send_at_ns=send_at,
            window_end_ns=window_end,
            timing_class=timing_class,
        )

    def _posterior(self, item: ScheduledCue) -> float | None:
        state = self.tracker.state
        if state is None or not self._tracker_usable():
            return None
        return min(1.0, sum(h.p for h in state.hypotheses if h.section == item.cue.at.section))

    def _device_views(self, item: ScheduledCue) -> dict[str, DeviceView]:
        views: dict[str, DeviceView] = {}
        for key in item.intents:
            device_id = key.split(".")[0]
            device = self.rig.device(device_id)
            overridden = self.visit is not None and device_id in self.visit.overridden
            views[device_id] = DeviceView(
                owner=device.owner if device else "missing", overridden=overridden
            )
        return views

    def _run_due(self, out: list[Effect]) -> None:
        for item in self.scheduler.due(self.now_ns):
            occurrence = item.occurrence.model_dump(mode="json")
            if self.now_ns > item.window_end_ns:
                self.scheduler.finish(item, "expired")
                self._note(
                    out,
                    "decision",
                    kind="abstain",
                    occurrence=occurrence,
                    reason="expired: past the on_miss window, discarded (I-4)",
                )
                continue
            if self.awaiting_go:
                self.scheduler.finish(item, "cancelled")
                self._note(
                    out,
                    "decision",
                    kind="abstain",
                    occurrence=occurrence,
                    reason="nothing is sent after RESUME until the next authorized GO (I-18)",
                )
                continue
            decision = decide(
                GateInput(
                    posterior=self._posterior(item),
                    cue=item.cue,
                    devices=self._device_views(item),
                    now_ns=self.now_ns,
                    occurrence=item.occurrence,
                    window_end_ns=item.window_end_ns,
                    authorization=item.authorization,
                    mode=self.mode,
                    milestone=self.settings.milestone,
                    tracker_authority=self.tracker.authority,
                    tracker_fresh=self._tracker_usable(),
                    stopped=self.stopped,
                    timing_class=item.timing_class,
                )
            )
            signature = f"{decision.kind}:{decision.reason}"
            if signature != item.last_decision:
                item.last_decision = signature
                self._note(out, "decision", **decision.model_dump(mode="json"))
            if decision.kind == "fire":
                self.scheduler.commit(item)
                visit = self.visit
                if (
                    visit is not None
                    and item.occurrence.song_instance == self.song_instance
                    and item.occurrence.section_visit == visit.number
                    and item.cue.at.section == visit.section
                ):
                    visit.committed = True
                out.append(
                    Commit(
                        occurrence=item.occurrence,
                        generation=self.scheduler.generation,
                        cue_class=item.cue.cue_class,
                        intents=tuple(
                            Intent(k.split(".")[0], k.split(".")[1], v)
                            for k, v in item.intents.items()
                        ),
                        reason=decision.reason,
                        window_end_ns=item.window_end_ns,
                        authorized_by=item.authorization,
                    )
                )
            elif decision.kind == "ask" and self.proposal is None:
                self.proposal = Proposal(item.key, self.now_ns)
                self._note(
                    out, "proposal", event="armed", occurrence=occurrence, armed_ns=self.now_ns
                )
            elif decision.kind == "abstain":
                self.scheduler.finish(item, "cancelled")
        if self.proposal is not None and self.scheduler.get(self.proposal.key) is None:
            self._note(out, "proposal", event="withdrawn", key=self.proposal.key)
            self.proposal = None

    # ---------------------------------------------------------------- BACK, MANUAL

    def _previous_section(self) -> str | None:
        song, visit = self.song, self.visit
        assert song is not None and visit is not None
        ids = [s.id for s in song.sections]
        index = ids.index(visit.section)
        return ids[index - 1] if index > 0 else None

    def _back(self, g: Gesture, out: list[Effect]) -> None:
        """BACK (Section 5.3). Never a GO: it only undoes one, or moves to an earlier section."""
        if self.stopped:
            self._refuse(out, "back", "STOP is active; RESUME first (I-18)", g.event_id)
            return
        if self.awaiting_go:
            self._refuse(
                out,
                "back",
                "after RESUME nothing is sent until the next authorized GO (I-18)",
                g.event_id,
            )
            return
        if self.song is None or self.visit is None:
            self._refuse(out, "back", "no section to go back from", g.event_id)
            return
        last = self._last_go
        if last is not None and last.visit is self.visit and not self.visit.committed:
            self._cancel_all("BACK before the GO's send deadline: the GO is cancelled", out)
            self.visit = last.previous
            if self._history and self._history[-1] is last.previous:
                self._history.pop()
            self._last_go = None
            self.proposal = None
            reinstated = [
                item.key
                for item in last.suspended
                if self.now_ns <= item.window_end_ns and self.scheduler.reinstate(item)
            ]
            self._note(
                out,
                "back",
                event="go_cancelled",
                event_id=g.event_id,
                cancelled_go=last.event_id,
                section=self.visit.section if self.visit else None,
                visit=self.visit.number if self.visit else None,
                reinstated=reinstated,
                generation=self.scheduler.generation,
                reason="nothing from the GO had been sent: it is cancelled with no output and the "
                "previous visit continues",
            )
            return
        target = self._history[-1].section if self._history else self._previous_section()
        if target is None:
            self._refuse(out, "back", "there is no earlier section to go back to", g.event_id)
            return
        if self._history:
            self._history.pop()
        state = self.tracker.state if self._tracker_usable() else None
        anchor = GoAnchor("F", self.now_ns, self.now_ns, self._tempo_beat_ns(self.song, state))
        self._enter(
            target,
            anchor=anchor,
            authorization=Authorization(event_id=g.event_id, action="back"),
            corroboration="footswitch",
            excluded=frozenset(),
            via="back",
            out=out,
            remember=False,
        )
        self._note(
            out,
            "back",
            event="reentered",
            event_id=g.event_id,
            section=target,
            reason="the GO's commands were already sent: BACK re-enters the previous section and "
            "sends its entry state as new occurrences (Section 5.3)",
        )

    def _manual(self, g: Gesture, out: list[Effect]) -> None:
        """MANUAL (Section 5.3): toggle tracker authority. Fires and cancels nothing."""
        if self.settings.milestone < 3:
            self._note(
                out,
                "manual",
                event="no_effect",
                event_id=g.event_id,
                reason="MANUAL arrives with M3; logged with no effect (Section 5.3)",
            )
            return
        tracker = self.tracker
        if tracker.authority:
            tracker.authority = False
            self._authority_off_by_performer = True
            self._mode_before_manual = self.mode
            self.mode = "manual"
            self._note(
                out,
                "manual",
                event="authority_off",
                event_id=g.event_id,
                reason="performer hold: tracker authority off; nothing fired or cancelled",
            )
            return
        if not self._authority_off_by_performer:
            reason = (
                "tracker authority was suspended by the system or never enabled; only the "
                "operator re-enables it (ADR-006, I-8)"
            )
        elif self.stopped or self.awaiting_go:
            reason = "STOP is active, or RESUME is awaiting its GO"
        elif not self._tracker_usable():
            reason = "the tracker is not fresh, following and on the selected song (I-8)"
        else:
            tracker.authority = True
            self._authority_off_by_performer = False
            self.mode = self._mode_before_manual
            self._note(
                out,
                "manual",
                event="authority_on",
                event_id=g.event_id,
                reason="performer hold turns back on the authority their own hold turned off",
            )
            return
        self._refuse(out, "manual", reason, g.event_id)

    # ---------------------------------------------------------------- STOP, RESUME

    def _stop(self, out: list[Effect], why: str) -> None:
        self._cancel_all("STOP: all pending work cancelled (I-18)", out)
        self.stopped = True
        self.proposal = None
        self.preset_b_confirmed = False
        self._last_state.clear()
        self.tracker.authority = False
        self._authority_off_by_performer = False
        self._last_go = None
        self._note(out, "stop", reason=why, generation=self.scheduler.generation)

    def _resume(self, out: list[Effect]) -> None:
        if not self.stopped:
            self._refuse(out, "resume", "RESUME needs an active STOP")
            return
        if not self.preset_b_confirmed:
            self._refuse(
                out,
                "resume",
                "confirm H4 preset B on the HUD before RESUME, so each device has one owner "
                "(Section 5.2)",
            )
            return
        self.stopped = False
        self.preset_b_confirmed = False
        self.awaiting_go = True
        generation = self.scheduler.bump()
        for copy in self.devices.values():
            for name in copy:
                copy[name] = None
        self.proposal = None
        self._last_state.clear()
        self._note(
            out,
            "resume",
            generation=generation,
            reason="new generation; no stale cue released; device state unknown; tracker "
            "authority stays suspended; nothing is sent until the next authorized GO (I-18)",
        )

    # --------------------------------------------------------------------- operator

    def _on_operator(self, inp: OperatorIn, out: list[Effect]) -> None:
        action = inp.action
        self._note(out, "operator", action=action, value=inp.value)
        if action == "arm":
            self.armed = True
            self._note(
                out, "arming", armed=True, reason="explicit for this run; never persists (I-2)"
            )
        elif action == "disarm":
            self.armed = False
            self._note(out, "arming", armed=False, reason="operator disarmed outputs")
        elif action == "confirm_preset_b":
            if self.stopped:
                self.preset_b_confirmed = True
            else:
                self._refuse(out, action, "only meaningful while STOP is active")
        elif action == "resume":
            self._resume(out)
        elif action == "enable_tracker":
            if self.stopped:
                self._refuse(out, action, "STOP is active")
            elif not self._tracker_usable():
                self._refuse(
                    out,
                    action,
                    "the tracker is not fresh, following and on the selected song; nothing to "
                    "enable (I-8)",
                )
            else:
                self.tracker.authority = True
                self._authority_off_by_performer = False
                self._note(out, "tracker", event="authority_enabled", reason="operator re-enabled")
        elif action == "set_mode":
            if inp.value in MODES:
                self.mode = MODES[MODES.index(inp.value)]
                self._note(out, "mode", mode=self.mode)
            else:
                self._refuse(out, action, f"unknown mode '{inp.value}'")
        elif action == "select_song":
            if inp.value is None:
                self._refuse(out, action, "no song given")
            else:
                self._select_song(inp.value, "HUD", out)
        elif action == "next_song":
            self._next_song(out)
        else:
            self._resync(out)

    def _resync(self, out: list[Effect]) -> None:
        if self.stopped:
            self._refuse(out, "resync", "STOP is active")
            return
        if self.awaiting_go:
            self._refuse(out, "resync", "nothing is sent after RESUME until the next authorized GO")
            return
        if not self._last_state:
            self._refuse(out, "resync", "no retryable state has been sent since the last GO")
            return
        for key in sorted(self._last_state):
            occurrence, value, cue_class = self._last_state[key]
            device_id, name = key.split(".")
            out.append(
                Commit(
                    occurrence=occurrence,
                    generation=self.scheduler.generation,
                    cue_class=cue_class,
                    intents=(Intent(device_id, name, value),),
                    reason="explicit operator resync of retryable state",
                    window_end_ns=self.now_ns,
                    authorized_by=None,
                    resync=True,
                )
            )

    # ------------------------------------------------------------------------ ports

    def _on_port(self, inp: PortStatus, out: list[Effect]) -> None:
        port = next((p for p in self.rig.ports if p.id == inp.port), None)
        unknown: list[str] = []
        if port is not None and port.direction in ("out", "both"):
            for device in self.rig.devices:
                if any(c.port == inp.port for c in device.commands.values()):
                    for name in self.devices[device.id]:
                        self.devices[device.id][name] = None
                    unknown.append(device.id)
        self._note(
            out,
            "port",
            port=inp.port,
            connected=inp.connected,
            devices_unknown=unknown,
            reason="device state unknown; nothing is resent; authority is never restored by a "
            "reconnect (I-5, I-8)",
        )

    # ---------------------------------------------------------------------- tracker

    def _on_tracker(self, state: MusicalState, out: list[Effect]) -> None:
        tr = self.tracker
        if state.tracker_session in tr.retired:
            self._note(
                out, "tracker", event="dropped", reason="packet from an earlier tracker session"
            )
            return
        if state.tracker_session != tr.session:
            if tr.session is not None:
                tr.retired.add(tr.session)
                self._suspend_tracker(out, "new tracker session (tracker restarted)")
            tr.session = state.tracker_session
        elif state.seq <= tr.seq:
            self._note(
                out,
                "tracker",
                event="dropped",
                reason=f"sequence {state.seq} is not newer than {tr.seq}; only the newest matters",
            )
            return
        tr.seq = state.seq
        tr.state = state
        tr.rx_ns = self.now_ns
        if tr.authority and state.mode != "following":
            self._suspend_tracker(out, f"the tracker reports '{state.mode}'")
        if (
            tr.authority
            and not self.awaiting_go
            and self.mode != "manual"
            and self.settings.milestone >= 4
            and not self.stopped
            and self.visit is not None
            and self.song is not None
            and state.song.id == self.song.id
            and state.mode == "following"
            and self._tracker_fresh()
        ):
            self._follow(state, out)

    def _follow(self, state: MusicalState, out: list[Effect]) -> None:
        visit = self.visit
        song = self.song
        assert visit is not None and song is not None
        if not state.hypotheses or state.beat is None or state.next_downbeat is None:
            return
        top = max(state.hypotheses, key=lambda h: h.p)
        if top.p < self.settings.relocalize_min_p:
            return
        beat_ns = self._tempo_beat_ns(song, state)
        anchor = state.next_downbeat.t_ns - top.bar * beat_ns * song.beats_per_bar
        if top.section == visit.section:
            if abs(anchor - visit.anchor_ns) * 2 <= visit.beat_ns:
                return
            pending = [i for i in self.scheduler.pending() if i.cue.at.section == visit.section]
            # A GO or BACK placed its entry cues by the GO timing policy (Section 8); the
            # tracker re-times only the bar-counted cues that follow.
            entries = [
                i for i in pending if visit.go_kind is not None and self._cue_beats(i.cue) == 0
            ]
            survivors = [i for i in pending if i not in entries]
            generation = self.scheduler.bump(pending)
            old = visit.anchor_ns
            visit.anchor_ns = anchor
            visit.beat_ns = beat_ns
            for item in survivors:
                self._time(item)
            self._note(
                out,
                "relocalize",
                section=visit.section,
                visit=visit.number,
                old_anchor_ns=old,
                anchor_ns=anchor,
                generation=generation,
                rescheduled=[i.key for i in survivors],
                reason="relocalized within the same visit: new generation, no new occurrences "
                "(I-6)",
            )
            return
        if top.section not in song.successors()[visit.section]:
            if self._last_ignored != top.section:
                self._last_ignored = top.section
                self._note(
                    out,
                    "tracker",
                    event="ignored",
                    section=top.section,
                    reason="hypothesis is not a successor of the current section; fails closed",
                )
            return
        self._enter(
            top.section,
            tracker_anchor=(anchor, beat_ns),
            authorization=None,
            corroboration=None,
            excluded=frozenset(),
            via="tracker",
            out=out,
        )
