"""A stand-in clock for tests that need to see the times a drain judged by."""

from __future__ import annotations

import time


class RecordingClock:
    """Real monotonic time, plus the last reading it handed out.

    Patched in as a module's ``time``, ``last`` is then exactly the ``now``
    that module last read.
    """

    def __init__(self) -> None:
        self.last: float | None = None

    def monotonic(self) -> float:
        self.last = time.monotonic()
        return self.last
