"""Chroma: pitch-class energy of the trailing window, and beat-synchronous averages of it that
are emitted only once a beat has passed."""

import numpy as np
import numpy.typing as npt

from roobotto.features.frames import TrailingSpectrum

Array = npt.NDArray[np.float64]
LOW_HZ = 55.0
HIGH_HZ = 5000.0


class ChromaExtractor:
    """Twelve pitch classes (C=0 ... B=11), normalized to sum to 1 per frame, or all zeros for
    silence. One vector per pushed block, from the trailing window only."""

    def __init__(self, rate: int, block: int, window: int = 2048) -> None:
        self._spectrum = TrailingSpectrum(rate, block, window)
        freqs = self._spectrum.freqs
        usable = (freqs >= LOW_HZ) & (freqs <= min(HIGH_HZ, rate / 2))
        classes = np.full(len(freqs), -1)
        classes[usable] = (np.round(12 * np.log2(freqs[usable] / 440.0)).astype(int) + 9) % 12
        self._map: Array = np.zeros((12, len(freqs)))
        for pc in range(12):
            self._map[pc, classes == pc] = 1.0

    def push(self, samples: Array) -> list[float]:
        magnitude = self._spectrum.push(samples)
        energy = self._map @ (magnitude**2)
        total = float(energy.sum())
        if total <= 1e-12:
            return [0.0] * 12
        return [round(float(x), 6) for x in energy / total]


class BeatSyncChroma:
    """Averages frame chroma over each completed beat interval. ``beat_passed`` is called with
    a beat's time once "now" has reached it; the average covers frames up to that beat."""

    def __init__(self) -> None:
        self._frames: list[tuple[int, list[float]]] = []
        self._last_beat_ns: int | None = None

    def push(self, t_ns: int, chroma: list[float]) -> None:
        self._frames.append((t_ns, chroma))

    def beat_passed(self, beat_ns: int) -> list[float] | None:
        """The mean chroma over [previous beat, this beat), or ``None`` for the first beat."""
        if self._last_beat_ns is None:
            self._last_beat_ns = beat_ns
            self._frames = [(t, c) for t, c in self._frames if t >= beat_ns]
            return None
        inside = [c for t, c in self._frames if self._last_beat_ns <= t < beat_ns]
        self._frames = [(t, c) for t, c in self._frames if t >= beat_ns]
        self._last_beat_ns = beat_ns
        if not inside:
            return [0.0] * 12
        mean = np.mean(np.asarray(inside, dtype=np.float64), axis=0)
        return [round(float(x), 6) for x in mean]
