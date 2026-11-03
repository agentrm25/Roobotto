"""Trailing analysis frames: a window that ends at the newest sample and never reaches past it."""

from typing import Any

import numpy as np
import numpy.typing as npt
from scipy import fft as sfft  # pyright: ignore[reportMissingTypeStubs]

Array = npt.NDArray[np.float64]


def _periodic_hann(window: int) -> Array:
    return 0.5 - 0.5 * np.cos(2.0 * np.pi * np.arange(window) / window)


def _rfft(samples: Array) -> npt.NDArray[np.complex128]:
    spectrum: Any = sfft.rfft(samples)  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return np.asarray(spectrum, dtype=np.complex128)  # pyright: ignore[reportUnknownArgumentType]


class TrailingSpectrum:
    """Keeps the last ``window`` samples and returns the magnitude spectrum of that trailing
    window after each pushed block. The first frames see zeros before the start."""

    def __init__(self, rate: int, block: int, window: int = 2048) -> None:
        if block <= 0 or window <= 0:
            raise ValueError("block and window must be positive")
        self.rate = rate
        self.block = block
        self.window = window
        self._buffer: Array = np.zeros(window)
        self._taper: Array = _periodic_hann(window)
        self.freqs: Array = np.fft.rfftfreq(window, 1.0 / rate)

    def push(self, samples: Array) -> Array:
        n = len(samples)
        if n == 0:
            return self.magnitude()
        if n >= self.window:
            self._buffer = np.asarray(samples[-self.window :], dtype=np.float64)
        else:
            self._buffer = np.concatenate([self._buffer[n:], np.asarray(samples, dtype=np.float64)])
        return self.magnitude()

    def magnitude(self) -> Array:
        return np.abs(_rfft(self._buffer * self._taper))
