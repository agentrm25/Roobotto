"""The scheduler: prepare, schedule, commit and cancel, with generations and send deadlines.

An occurrence is prepared once (ADR-012): once it is pending or finished, preparing it again is
refused, so cancels, reschedules and relocalization never create a second one (I-7). The
generation increments on every cancel, reschedule, STOP, RESUME and relocalization. It
invalidates pending work and is never part of the identity (I-6): items that survive a bump are
re-stamped; the supervisor refuses any commit from an older generation.
"""

from collections.abc import Iterable
from dataclasses import dataclass

from roobotto.config.song import Cue
from roobotto.cues.gate import TimingClass
from roobotto.messages import Authorization, CueOccurrence, Lifecycle, OccurrenceId


@dataclass
class ScheduledCue:
    occurrence: OccurrenceId
    cue: Cue
    intents: dict[str, str]
    """``device.command`` -> value still to send. The performer can remove some (I-3)."""
    generation: int
    lifecycle: Lifecycle
    authorization: Authorization | None
    corroboration: str | None
    due_ns: int = 0
    send_at_ns: int = 0
    """The send deadline: due time minus device latency minus the margin."""
    window_end_ns: int = 0
    """The last moment ``on_miss`` allows a send. Past it the item expires (I-4)."""
    timing_class: TimingClass | None = None
    last_decision: str | None = None

    @property
    def key(self) -> str:
        return self.occurrence.key

    def snapshot(self) -> CueOccurrence:
        return CueOccurrence.model_validate(
            {
                "id": self.occurrence,
                "generation": self.generation,
                "class": self.cue.cue_class,
                "intents": self.intents or dict(self.cue.state),
                "due_ns": max(self.due_ns, self.send_at_ns),
                "send_deadline_ns": self.send_at_ns,
                "lifecycle": self.lifecycle,
                "authorized_by": self.authorization,
            }
        )


class Scheduler:
    def __init__(self) -> None:
        self.generation = 0
        self._pending: dict[str, ScheduledCue] = {}
        self._done: dict[str, Lifecycle] = {}

    def pending(self) -> list[ScheduledCue]:
        return list(self._pending.values())

    def get(self, key: str) -> ScheduledCue | None:
        return self._pending.get(key)

    def known(self, key: str) -> bool:
        return key in self._pending or key in self._done

    def outcome(self, key: str) -> Lifecycle | None:
        item = self._pending.get(key)
        return item.lifecycle if item else self._done.get(key)

    def prepare(
        self,
        occurrence: OccurrenceId,
        cue: Cue,
        intents: dict[str, str],
        authorization: Authorization | None,
        corroboration: str | None,
    ) -> ScheduledCue | None:
        """Plan an occurrence. ``None`` if it is already pending or finished (I-7)."""
        if self.known(occurrence.key):
            return None
        item = ScheduledCue(
            occurrence=occurrence,
            cue=cue,
            intents=dict(intents),
            generation=self.generation,
            lifecycle="planned",
            authorization=authorization,
            corroboration=corroboration,
        )
        self._pending[item.key] = item
        return item

    def schedule(
        self,
        item: ScheduledCue,
        *,
        due_ns: int,
        send_at_ns: int,
        window_end_ns: int,
        timing_class: TimingClass | None,
    ) -> None:
        item.due_ns = due_ns
        item.send_at_ns = send_at_ns
        item.window_end_ns = window_end_ns
        item.timing_class = timing_class
        item.generation = self.generation
        item.lifecycle = "scheduled"

    def bump(self, survivors: Iterable[ScheduledCue] = ()) -> int:
        """Start a new generation. Only ``survivors`` are re-stamped; the rest go stale."""
        self.generation += 1
        for item in survivors:
            if item.key in self._pending:
                item.generation = self.generation
        return self.generation

    def cancel_all(self) -> list[ScheduledCue]:
        cancelled = list(self._pending.values())
        self.bump()
        for item in cancelled:
            self.finish(item, "cancelled")
        return cancelled

    def cancel_device(self, device: str) -> list[tuple[ScheduledCue, list[str]]]:
        """Remove every pending intent for ``device`` (I-3). Empty items are cancelled."""
        touched: list[tuple[ScheduledCue, list[str]]] = []
        for item in list(self._pending.values()):
            removed = [k for k in item.intents if k.split(".")[0] == device]
            if not removed:
                continue
            for k in removed:
                del item.intents[k]
            touched.append((item, removed))
        if touched:
            self.bump(self._pending.values())
            for item, _ in touched:
                if not item.intents:
                    self.finish(item, "cancelled")
        return touched

    def due(self, now_ns: int) -> list[ScheduledCue]:
        """Scheduled items of the current generation whose send deadline has come."""
        ready = [
            i
            for i in self._pending.values()
            if i.lifecycle == "scheduled"
            and i.generation == self.generation
            and i.send_at_ns <= now_ns
        ]
        return sorted(ready, key=lambda i: (i.send_at_ns, i.due_ns, i.key))

    def commit(self, item: ScheduledCue) -> None:
        """Hand the item to the supervisor. It is finished either way: at most once."""
        item.lifecycle = "committed"
        self.finish(item, "committed")

    def mark(self, key: str, lifecycle: Lifecycle) -> None:
        if key in self._done:
            self._done[key] = lifecycle

    def finish(self, item: ScheduledCue, lifecycle: Lifecycle) -> None:
        item.lifecycle = lifecycle
        self._pending.pop(item.key, None)
        self._done[item.key] = lifecycle
