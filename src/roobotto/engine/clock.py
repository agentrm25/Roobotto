"""The clock interface (ADR-005, ADR-014).

All runtime time is nanoseconds on one clock: ``time.monotonic_ns()`` live, a simulated clock in
tests and replay. Only the process wrapper reads a clock. It stamps every input with the time
it became available, and the engine core reads time only from those stamps.
"""

import time
from typing import Protocol


class Clock(Protocol):
    def now_ns(self) -> int: ...


class MonotonicClock:
    """The live clock. The only wall-clock read in Roobotto's runtime."""

    def now_ns(self) -> int:
        return time.monotonic_ns()


class SimClock:
    """A clock that moves only when told to. Used by tests, replay and ``run --dry-run``."""

    def __init__(self, start_ns: int = 0) -> None:
        if start_ns < 0:
            raise ValueError("start_ns must be 0 or more")
        self._now = start_ns

    def now_ns(self) -> int:
        return self._now

    def advance(self, delta_ns: int) -> int:
        if delta_ns < 0:
            raise ValueError("advance() moves forward only; use jump_to() for a clock jump")
        self._now += delta_ns
        return self._now

    def jump_to(self, t_ns: int) -> None:
        """Set the time, backwards too, to simulate a clock jump in fault-injection tests."""
        if t_ns < 0:
            raise ValueError("t_ns must be 0 or more")
        self._now = t_ns
