"""Footswitch gestures from raw press and release edges (plan Section 5.3, ADR-010, I-17).

Press-gesture actions (GO, STOP, EXTEND) act on the press edge and are mapped by provenance
classification; each sits alone on its switch (rig validation). This module recognizes the
two timed gestures on a shared switch:

- **tap**: released before ``tap_max_ms``. Recognized on release; the release authorizes it.
- **hold**: held for ``hold_ms``. Recognized the moment the hold reaches ``hold_ms``; the press
  authorizes it. The release that follows does nothing.

Neither gesture can produce a forward action: rig validation keeps GO and EXTEND off any switch
with a tap or hold control. The recognizer is deterministic and reads time only from its
arguments.
"""

from dataclasses import dataclass

from roobotto.config.rig import Rig

NS_PER_MS = 1_000_000


@dataclass(frozen=True)
class Gesture:
    action: str
    switch: str
    event_id: str
    """The edge that authorizes it: the release for a tap, the press for a hold."""
    pressed_ns: int
    at_ns: int
    """The authorization point: the release, or ``pressed_ns + hold_ms``."""


@dataclass
class _Held:
    pressed_ns: int
    press_event: str
    hold_fired: bool = False


class Gestures:
    def __init__(self, rig: Rig) -> None:
        self._tap = {c.switch: c.action for c in rig.controls if c.gesture == "tap"}
        self._hold = {c.switch: c.action for c in rig.controls if c.gesture == "hold"}
        self._tap_max_ns = rig.timing.tap_max_ms * NS_PER_MS
        self._hold_ns = rig.timing.hold_ms * NS_PER_MS
        self._held: dict[str, _Held] = {}

    def handles(self, switch: str) -> bool:
        return switch in self._tap or switch in self._hold

    def press(self, switch: str, t_ns: int, event_id: str) -> str | None:
        """Start timing a press. Returns a note if an earlier press never saw its release."""
        note = None
        if switch in self._held:
            note = "a new press before the previous release; timing restarts from this press"
        self._held[switch] = _Held(t_ns, event_id)
        return note

    def release(self, switch: str, t_ns: int, event_id: str) -> tuple[Gesture | None, str]:
        held = self._held.pop(switch, None)
        if held is None:
            return None, "a release with no press; ignored"
        if held.hold_fired:
            return None, "release after a hold; the hold already acted"
        duration = t_ns - held.pressed_ns
        if duration < self._tap_max_ns and switch in self._tap:
            gesture = Gesture(self._tap[switch], switch, event_id, held.pressed_ns, t_ns)
            return gesture, f"tap: released after {duration // NS_PER_MS} ms"
        return None, f"released after {duration // NS_PER_MS} ms: neither a tap nor a hold"

    def due(self, now_ns: int) -> list[Gesture]:
        """Holds that have reached ``hold_ms`` by ``now_ns``."""
        fired: list[Gesture] = []
        for switch in sorted(self._held):
            held = self._held[switch]
            at = held.pressed_ns + self._hold_ns
            if not held.hold_fired and switch in self._hold and now_ns >= at:
                held.hold_fired = True
                fired.append(
                    Gesture(self._hold[switch], switch, held.press_event, held.pressed_ns, at)
                )
        return fired

    def next_wakeup_ns(self) -> int | None:
        times = [
            h.pressed_ns + self._hold_ns
            for s, h in self._held.items()
            if not h.hold_fired and s in self._hold
        ]
        return min(times, default=None)
