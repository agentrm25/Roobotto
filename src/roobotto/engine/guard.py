"""The future-read guard (I-9, ADR-014).

During replay nothing may read input from after "now". The replay harness advances the guard to
each input's time before the engine sees it. Any time-indexed source (audio blocks from T-07
onward) reads through ``GuardedSeries``, which raises if asked for data past "now".
"""

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass, field


class FutureReadError(RuntimeError):
    """Something tried to read data from after "now"."""


class FutureReadGuard:
    def __init__(self) -> None:
        self._now: int | None = None

    @property
    def now_ns(self) -> int | None:
        return self._now

    def advance(self, t_ns: int) -> None:
        """Move "now" forward. It never moves back, so a clock jump cannot open the future."""
        if self._now is None or t_ns > self._now:
            self._now = t_ns

    def check(self, t_ns: int, what: str = "data") -> None:
        if self._now is None or t_ns > self._now:
            raise FutureReadError(
                f"attempt to read {what} at t={t_ns} ns, after now={self._now} ns (I-9)"
            )


@dataclass
class GuardedSeries[T]:
    """Time-stamped items readable only up to the guard's "now"."""

    guard: FutureReadGuard
    times: list[int] = field(default_factory=lambda: [])
    items: list[T] = field(default_factory=lambda: [])

    def append(self, t_ns: int, item: T) -> None:
        if self.times and t_ns < self.times[-1]:
            raise ValueError("items must be appended in time order")
        self.times.append(t_ns)
        self.items.append(item)

    def read_until(self, t_ns: int) -> Sequence[T]:
        """Every item stamped at or before ``t_ns``. Raises if ``t_ns`` is after now."""
        self.guard.check(t_ns, "a guarded series")
        return self.items[: bisect_right(self.times, t_ns)]
