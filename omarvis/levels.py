from __future__ import annotations

import math
import sys
import threading
import time
from array import array
from collections.abc import Callable


def rms_level(chunk: bytes) -> float:
    """Return the RMS of int16 mono PCM normalized to 0.0-1.0."""
    if len(chunk) < 2:
        return 0.0
    samples = array("h")
    samples.frombytes(chunk[: len(chunk) - (len(chunk) % 2)])
    if sys.byteorder != "little":
        samples.byteswap()
    if not samples:
        return 0.0
    mean_square = sum(sample * sample for sample in samples) / len(samples)
    return min(1.0, math.sqrt(mean_square) / 32768.0)


class LevelThrottle:
    """Coalesce input/output levels while preserving transitions to silence."""

    def __init__(
        self,
        sink: Callable[[float, float], None],
        *,
        interval: float = 0.1,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sink = sink
        self._interval = interval
        self._clock = clock
        self._in_level = 0.0
        self._out_level = 0.0
        self._last_emit: float | None = None
        self._final_zero_emitted = False
        self._lock = threading.Lock()

    @staticmethod
    def _normalized(value: float) -> float:
        return min(1.0, max(0.0, float(value)))

    def update_in(self, value: float) -> None:
        with self._lock:
            previous = self._in_level
            self._in_level = self._normalized(value)
            self._maybe_emit(silence_edge=previous > 0.0 and self._in_level == 0.0)

    def update_out(self, value: float) -> None:
        with self._lock:
            previous = self._out_level
            self._out_level = self._normalized(value)
            self._maybe_emit(silence_edge=previous > 0.0 and self._out_level == 0.0)

    def _maybe_emit(self, *, silence_edge: bool) -> None:
        now = self._clock()
        if (
            self._last_emit is None
            or now - self._last_emit >= self._interval
            or silence_edge
        ):
            self._emit(now)

    def _emit(self, now: float) -> None:
        self._last_emit = now
        self._sink(round(self._in_level, 3), round(self._out_level, 3))

    def force_zero(self) -> None:
        """Emit a final all-zero sample once, regardless of throttling."""
        with self._lock:
            if self._final_zero_emitted:
                return
            self._final_zero_emitted = True
            self._in_level = 0.0
            self._out_level = 0.0
            self._emit(self._clock())
