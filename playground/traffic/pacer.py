"""Paced runs through SignalGate: visitors leased a batch at a time, just in time.

Envelopes are single-use and meant to be used soon after they are handed
out, so a run never leases everything up front. It leases one batch (at most
the session's ``limits.lease_max`` visits), sends those visitors through the
sample app, and leases the next batch only when both hold:

- no more than one batch is waiting to start;
- the app's SDK backlog (log events queued, not yet sent) is at most
  ``max(BACKLOG_FLOOR, limits.use_within_s × the drain rate)``, the drain
  rate being how fast the app's SDK sends its log events, measured on your
  machine over the last few seconds (``DrainMeter``).

Near the end of a run it tapers. Once the log events of the visitors not
leased yet fit in one ``limits.use_within_s`` of delivery, the run stops
leasing ahead of the app's SDK: the backlog plus what the visitors already
leased may still add has to be within ``max(BACKLOG_FLOOR, PACER_POLL_S ×
the drain rate)`` before the next batch, and after each such batch it waits
for one fresh reading of the counters. So the app has little left to send
when the last visitor is done, and the wait after the run stays short. The
run as a whole still goes as fast as the app's SDK sends.

If the backlog would take longer than ``limits.max_queue_age_s`` to drain,
or stops draining for that long, the run stops leasing and is marked
degraded. The limits come from the server; nothing here hard-codes one.
"""

from __future__ import annotations

import asyncio
import time
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from playground.supervisor.control_client import KEY_SENT, ControlClient, counter, queued
from playground.traffic.driver import RunResult, _run_one, _Tally
from playground.traffic.local_constants import (
    BACKLOG_FLOOR,
    DRAIN_RATE_WINDOW_S,
    LOGS_PER_VISIT_MAX,
    LOOPBACK_REQUEST_TIMEOUT_S,
    ONLINE_CONCURRENCY,
    PACER_POLL_S,
)
from playground.traffic.plan import Visit

Lease = Callable[[int], Awaitable[list[Mapping[str, Any]]]]


class LeaseStopped(Exception):
    """The lease callable refuses to lease any more; ``reason`` says why."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def visit_of(wire: Mapping[str, Any]) -> Visit:
    """A leased visit, as the driver sends it."""
    script = wire["script"]
    return Visit(
        ref=wire["ref"],
        cls=wire["class"],
        user_id=wire["user_id"],
        client_ip=wire["client_ip"],
        success=bool(script["success"]),
        success_after_ms=int(script["success_after_ms"]),
        envelopes=wire["envelopes"],
    )


class DrainMeter:
    """The app's SDK counters, read every ``PACER_POLL_S``: backlog and drain rate."""

    def __init__(self, control: ControlClient | None, baseline: Mapping[str, int]) -> None:
        self._control = control
        self.baseline = dict(baseline)
        self.latest = dict(baseline)
        self._samples: deque[tuple[float, int]] = deque()

    @property
    def backlog(self) -> int:
        return queued(self.latest)

    @property
    def rate(self) -> float:
        """Log events sent per second over the last ``DRAIN_RATE_WINDOW_S``."""
        if len(self._samples) < 2:
            return 0.0
        (t0, sent0), (t1, sent1) = self._samples[0], self._samples[-1]
        return (sent1 - sent0) / (t1 - t0) if t1 > t0 else 0.0

    def delta(self, key: str) -> int:
        return counter(self.latest, key) - counter(self.baseline, key)

    async def read(self) -> None:
        if self._control is None:
            return
        try:
            self.latest = await self._control.metrics()
        except Exception:  # noqa: BLE001 - a missed reading just keeps the last one
            return
        now = time.monotonic()
        self._samples.append((now, counter(self.latest, KEY_SENT)))
        while len(self._samples) > 2 and now - self._samples[0][0] > DRAIN_RATE_WINDOW_S:
            self._samples.popleft()

    async def run(self, on_tick: Callable[[], None]) -> None:
        """Read the counters and call ``on_tick`` every ``PACER_POLL_S``, until cancelled."""
        while True:
            await self.read()
            on_tick()
            await asyncio.sleep(PACER_POLL_S)


@dataclass
class PacedRun:
    """One paced run: its tally so far, the visits it leased, and how it ended."""

    size: int
    tally: _Tally = field(init=False)
    visits: list[Mapping[str, Any]] = field(default_factory=list)
    degraded: bool = False
    stopped_by: str | None = None

    def __post_init__(self) -> None:
        self.tally = _Tally(self.size)

    @staticmethod
    def taper_allowance(rate: float) -> float:
        """How many log events may still be on their way while the run tapers."""
        return max(BACKLOG_FLOOR, PACER_POLL_S * rate)

    def pending(self, backlog: int) -> int:
        """The log events still to be sent from what this run has leased so far.

        That is the SDK ``backlog``, plus every log a leased visitor that
        hasn't started may still add, plus the success log a visitor under
        way may still add (a visitor counts as done only after it confirmed).
        """
        not_started = len(self.visits) - self.tally.started
        under_way = self.tally.started - self.tally.done
        return backlog + LOGS_PER_VISIT_MAX * not_started + under_way

    def result(self, stop: asyncio.Event) -> RunResult:
        attack = sum(1 for v in self.visits if v.get("class") == "attack")
        return RunResult(
            total=self.size,
            done=self.tally.done,
            actions=self.tally.actions,
            successes=self.tally.successes,
            attack=attack,
            real=len(self.visits) - attack,
            stopped=stop.is_set() and self.tally.done < self.size,
            statuses=self.tally.statuses,
            codes=self.tally.codes,
        )

    async def run(
        self,
        *,
        lease: Lease,
        lease_max: int,
        meter: DrainMeter,
        use_within_s: float,
        max_queue_age_s: float,
        base_url: str,
        control_token: str,
        stop: asyncio.Event,
        run_id: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> RunResult:
        """Lease and send ``size`` visitors, paced; honour ``stop`` early.

        ``transport`` is for tests only: how the visitors reach the app.
        """
        semaphore = asyncio.Semaphore(ONLINE_CONCURRENCY)
        limits = httpx.Limits(
            max_connections=ONLINE_CONCURRENCY, max_keepalive_connections=ONLINE_CONCURRENCY
        )
        tasks: list[asyncio.Future[None]] = []
        stalled_since: float | None = None
        # Once the run tapers it keeps tapering, whatever the next reading
        # says. ``held_since`` is when the taper started holding the next
        # batch back; held back for longer than ``max_queue_age_s``, the
        # counters are taken to be stuck and the run stops tapering for good.
        tapering = False
        taper_left = False
        held_since: float | None = None

        async def nothing(_progress: object) -> None:
            return None

        async with httpx.AsyncClient(
            base_url=base_url,
            trust_env=False,
            limits=limits,
            timeout=LOOPBACK_REQUEST_TIMEOUT_S,
            transport=transport,
        ) as client:
            try:
                while len(self.visits) < self.size and not stop.is_set():
                    waiting = len(self.visits) - self.tally.started
                    backlog, rate = meter.backlog, meter.rate
                    allowance = max(BACKLOG_FLOOR, use_within_s * rate)
                    if rate > 0 and backlog / rate > max_queue_age_s:
                        self.degraded = True
                        break
                    if waiting >= lease_max or backlog > allowance:
                        stalled_since = stalled_since or time.monotonic()
                        if backlog > allowance and (
                            time.monotonic() - stalled_since > max_queue_age_s
                        ):
                            self.degraded = True
                            break
                        await asyncio.sleep(PACER_POLL_S)
                        continue
                    stalled_since = None
                    unleased = self.size - len(self.visits)
                    count = min(lease_max, unleased)
                    tapering = tapering or (
                        not taper_left
                        and rate > 0
                        and unleased * LOGS_PER_VISIT_MAX <= use_within_s * rate
                    )
                    if tapering:
                        room = self.taper_allowance(rate) - self.pending(backlog)
                        if room >= LOGS_PER_VISIT_MAX:
                            count = min(count, int(room // LOGS_PER_VISIT_MAX))
                            held_since = None
                        else:
                            held_since = held_since or time.monotonic()
                            if time.monotonic() - held_since <= max_queue_age_s:
                                await asyncio.sleep(PACER_POLL_S)
                                continue
                            tapering, taper_left = False, True
                    try:
                        batch = await lease(count)
                    except LeaseStopped as stopped:
                        self.stopped_by = stopped.reason
                        break
                    if not batch:
                        break
                    self.visits.extend(batch)
                    for wire in batch:
                        tasks.append(
                            asyncio.ensure_future(
                                _run_one(
                                    visit_of(wire),
                                    client=client,
                                    control_token=control_token,
                                    semaphore=semaphore,
                                    stop=stop,
                                    tally=self.tally,
                                    on_progress=nothing,
                                    run_id=run_id,
                                )
                            )
                        )
                    if tapering and len(self.visits) < self.size:
                        # The counters are read every PACER_POLL_S: the next
                        # batch waits for a reading that knows about this one.
                        await asyncio.sleep(PACER_POLL_S)
                if tasks:
                    await asyncio.gather(*tasks)
            finally:
                for task in tasks:
                    if not task.done():
                        task.cancel()
                if tasks:
                    await asyncio.wait(tasks)
        return self.result(stop)


__all__ = ["DrainMeter", "Lease", "LeaseStopped", "PacedRun", "visit_of"]
