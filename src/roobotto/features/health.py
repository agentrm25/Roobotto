"""Input health per feed: ``ok``, ``degraded`` (blocks arrive but carry nothing usable: silence
or clipping) or ``lost`` (blocks stopped arriving). The replay harness and, later, the live
tracker report every change. A lost feed is reported, never filled with zeros."""

import math

import numpy as np
import numpy.typing as npt

from roobotto.messages import Health

Array = npt.NDArray[np.float64]
SILENCE_RMS = 1e-4
CLIP_LEVEL = 0.999
SILENT_SECONDS = 1.5
"""Silence this long degrades a feed: longer than any gap between hits at a playable tempo."""


class HealthMonitor:
    def __init__(self, rate: int, block: int, silent_seconds: float = SILENT_SECONDS) -> None:
        self.rate = rate
        self.block = block
        self._silent = 0
        self._silent_blocks = max(1, math.ceil(silent_seconds * rate / block))
        self.status: Health = "ok"

    def push(self, samples: Array) -> Health:
        rms = float(np.sqrt(np.mean(samples**2))) if len(samples) else 0.0
        clipped = bool(np.any(np.abs(samples) >= CLIP_LEVEL))
        self._silent = self._silent + 1 if rms < SILENCE_RMS else 0
        self.status = "degraded" if clipped or self._silent >= self._silent_blocks else "ok"
        return self.status

    def lost(self) -> Health:
        self._silent = 0
        self.status = "lost"
        return self.status
