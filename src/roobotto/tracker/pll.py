"""An onset-driven phase-locked loop for beat tracking, limited to the song's tempo range.

Causal by construction: it consumes one onset-envelope value per block and reports a beat only
once "now" has reached it. Onsets are detected as local peaks of the envelope above an adaptive
threshold (a slow exponential mean plus a margin), which costs one block of latency. An onset
inside the lock window around the predicted beat corrects phase and period; a predicted beat
with no onset is still reported when its time passes, so counting continues through quiet bars.
"""

from dataclasses import dataclass, field

LOCK_WINDOW = 0.25
"""Half-width of the lock window, as a fraction of the period."""
PHASE_GAIN = 0.5
PERIOD_GAIN = 0.15
MIN_ONSET_GAP = 0.3
"""Minimum gap between accepted onsets, as a fraction of the period (rejects double hits)."""


@dataclass
class OnsetPLL:
    nominal_bpm: float
    tempo_range: tuple[float, float]
    start_s: float
    """The annotated song start: the first beat, which anchors the phase."""
    hop_s: float
    threshold_margin: float = 0.6
    mean_decay: float = 0.98
    _period: float = field(init=False)
    _next_beat: float = field(init=False)
    _mean: float = 0.0
    _peak_value: float = 0.0
    _previous: float = 0.0
    _previous_t: float = 0.0
    _rising: bool = False
    _last_onset: float = -1.0
    _beats: int = 0

    def __post_init__(self) -> None:
        lo, hi = self.tempo_range
        if not lo < hi:
            raise ValueError("tempo range must be (low, high) with low < high")
        self._period = 60.0 / min(max(self.nominal_bpm, lo), hi)
        self._next_beat = self.start_s

    @property
    def bpm(self) -> float:
        return 60.0 / self._period

    @property
    def period_s(self) -> float:
        return self._period

    @property
    def next_beat_s(self) -> float:
        return self._next_beat

    def _clamp(self, period: float) -> float:
        lo, hi = self.tempo_range
        return min(max(period, 60.0 / hi), 60.0 / lo)

    def push(self, t_s: float, onset: float) -> list[float]:
        """Consume the envelope value for the block ending at ``t_s``. Returns the beats whose
        time has now passed, in order."""
        beats: list[float] = []
        # Peak detection, one block late: the previous value was a local maximum.
        peak_at: float | None = None
        if (
            self._rising
            and onset < self._previous
            and self._previous > self._mean * (1 + self.threshold_margin) + 1e-9
        ):
            peak_at = self._previous_t
        self._rising = onset > self._previous
        self._mean = self.mean_decay * self._mean + (1 - self.mean_decay) * onset
        self._previous, self._previous_t = onset, t_s

        usable = peak_at is not None and peak_at >= self.start_s - LOCK_WINDOW * self._period
        if (
            peak_at is not None
            and usable
            and (self._last_onset < 0 or peak_at - self._last_onset >= MIN_ONSET_GAP * self._period)
        ):
            self._last_onset = peak_at
            error = peak_at - self._next_beat
            if abs(error) <= LOCK_WINDOW * self._period:
                # Lock: this onset is the predicted beat. Correct phase and period.
                self._period = self._clamp(self._period + PERIOD_GAIN * error)
                beat = self._next_beat + PHASE_GAIN * error
                beats.append(min(beat, t_s))
                self._beats += 1
                self._next_beat = beat + self._period
        # Predicted beats whose time has passed without an onset are still counted.
        while self._next_beat + LOCK_WINDOW * self._period <= t_s:
            beats.append(self._next_beat)
            self._beats += 1
            self._next_beat += self._period
        return beats
