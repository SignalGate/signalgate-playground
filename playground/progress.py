"""How long a wait on your laptop still takes, estimated from what it has done so far.

The console waits for the sample app's SDK to send its queued log events. It
measures how fast that queue has been shrinking and turns what is left into
a few plain words ("about 30 s left"). Nothing here promises anything about
SignalGate: the estimate describes your app's SDK on this laptop only.
"""

from __future__ import annotations

import math
from collections import deque

from playground.copy.en import text
from playground.traffic.local_constants import DRAIN_RATE_WINDOW_S, ETA_STEP_S

# Seconds in a minute (a unit, not a wait).
_PER_MINUTE = 60


def eta_seconds(remaining: int, rate: float, *, at_most: float | None = None) -> float | None:
    """Seconds until ``remaining`` events are gone at ``rate`` events per second.

    None unless both are positive (and the rate is a real number): nothing
    left, or no measured progress yet, gives no estimate. ``at_most`` is how
    much longer the console will wait at all: the estimate never says more,
    and once there is no wait left it gives none.
    """
    if remaining <= 0 or not math.isfinite(rate) or rate <= 0:
        return None
    seconds = remaining / rate
    if at_most is not None:
        if at_most <= 0:
            return None
        seconds = min(seconds, at_most)
    return seconds


def eta_words(seconds: float | None) -> str | None:
    """``seconds`` as the console says it, or None when there is no estimate.

    Under a minute it is rounded up to a whole number of ``ETA_STEP_S``
    seconds; from a minute on, to whole minutes. Always rounded up, never
    down, so the wait doesn't look shorter than it is.
    """
    if seconds is None or not math.isfinite(seconds) or seconds < 0:
        return None
    if seconds < _PER_MINUTE:
        steps = max(1, math.ceil(seconds / ETA_STEP_S))
        return text("eta.seconds", seconds=steps * ETA_STEP_S)
    return text("eta.minutes", minutes=math.ceil(seconds / _PER_MINUTE))


class RateWindow:
    """How fast a backlog shrinks, measured over the last ``window_s`` seconds.

    Feed it ``add(t, remaining)`` readings (``t`` from a monotonic clock).
    ``rate()`` is the events per second the backlog fell by, from the newest
    reading back to the last one at least ``window_s`` older (or the oldest
    kept, early on). A backlog that grew or stayed put gives 0, never a
    negative rate.
    """

    def __init__(self, window_s: float = DRAIN_RATE_WINDOW_S) -> None:
        self._window_s = window_s
        self._readings: deque[tuple[float, int]] = deque()

    def add(self, t: float, remaining: int) -> None:
        """One reading: ``remaining`` events were left at time ``t``."""
        if self._readings and t < self._readings[-1][0]:
            self._readings.clear()  # the clock went back: start measuring again
        self._readings.append((t, remaining))
        # Keep one reading at or before the window's start, and none older.
        while len(self._readings) > 2 and self._readings[1][0] <= t - self._window_s:
            self._readings.popleft()

    def rate(self) -> float:
        """Events per second the backlog fell by across the window (0 or more)."""
        if len(self._readings) < 2:
            return 0.0
        (t0, left0), (t1, left1) = self._readings[0], self._readings[-1]
        if t1 <= t0:
            return 0.0
        return max(0.0, (left0 - left1) / (t1 - t0))


__all__ = ["RateWindow", "eta_seconds", "eta_words"]
