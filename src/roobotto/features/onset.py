"""Onset envelope: spectral flux of the trailing window, from past audio only."""

import numpy as np
import numpy.typing as npt

from roobotto.features.frames import TrailingSpectrum

Array = npt.NDArray[np.float64]


class OnsetExtractor:
    """Half-wave rectified log-magnitude spectral flux, one value per pushed block."""

    def __init__(
        self, rate: int, block: int, window: int = 2048, compression: float = 100.0
    ) -> None:
        self._spectrum = TrailingSpectrum(rate, block, window)
        self._previous: Array | None = None
        self._compression = compression

    def push(self, samples: Array) -> float:
        magnitude = self._spectrum.push(samples)
        compressed = np.log1p(self._compression * magnitude)
        if self._previous is None:
            flux = 0.0
        else:
            diff = compressed - self._previous
            flux = float(np.sum(diff[diff > 0]) / len(diff))
        self._previous = compressed
        return round(flux, 6)
