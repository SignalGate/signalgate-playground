"""The pacer's taper near the end of a run, against a simulated SDK queue.

The visitors reach a stand-in for the sample app through an httpx
transport. Each action queues one log event and each success another; a
stand-in meter lets the queue drain at a fixed rate R. The run tapers once
the logs of the visitors it hasn't leased yet fit in ``use_within_s`` of
delivery. With the meter read fresh at every decision, the bound is exact:
from then on it never leases while what is still to be sent from the
visitors it leased could pass ``max(BACKLOG_FLOOR, PACER_POLL_S × R)``, and
the queue left when the last visitor is done stays within that (plus one
visitor's logs), whether the SDK kept up until then or the queue was full.
With a meter that is read only now and then, as the real one is, the run
still never leases the rest of its plan in one burst.
"""

from __future__ import annotations

import asyncio
import itertools
import math
import time
from dataclasses import dataclass
from typing import Any

import httpx
import pytest

import playground.traffic.pacer as pacer_module
from playground.traffic.local_constants import BACKLOG_FLOOR, LOGS_PER_VISIT_MAX
from playground.traffic.pacer import PacedRun

# PACER_POLL_S, made small so these runs take well under a second each.
POLL_S = 0.01
# The simulated SDK's drain rate, in log events per second.
RATE = 1000.0
LEASE_MAX = 20
# How far the clock may read a sleep short (the loop wakes a hair early).
CLOCK_SLACK_S = time.get_clock_info("monotonic").resolution + POLL_S / 100


@pytest.fixture(autouse=True)
def _small_poll(monkeypatch):
    monkeypatch.setattr(pacer_module, "PACER_POLL_S", POLL_S)


def taper_allowance(rate: float = RATE) -> float:
    return max(BACKLOG_FLOOR, POLL_S * rate)


class SyncMeter:
    """The SDK queue, read fresh each time: every read drains it by R·dt."""

    def __init__(self, rate: float = RATE) -> None:
        self.rate = rate
        self._level = 0.0
        self._at = time.monotonic()
        self.last_read = 0

    def _drain(self) -> None:
        now = time.monotonic()
        self._level = max(0.0, self._level - self.rate * (now - self._at))
        self._at = now

    def queue(self) -> None:
        """The app queued one log event."""
        self._drain()
        self._level += 1

    def peek(self) -> int:
        """What is really queued now, without it counting as the pacer's reading."""
        self._drain()
        return math.ceil(self._level - 1e-9)

    @property
    def backlog(self) -> int:
        self.last_read = self.peek()
        return self.last_read


class StaleMeter(SyncMeter):
    """The same queue, but its reading changes only when ``refresh`` runs."""

    def __init__(self, rate: float = RATE) -> None:
        super().__init__(rate)
        self._reading = 0

    def refresh(self) -> None:
        self._reading = self.peek()

    @property
    def backlog(self) -> int:
        self.last_read = self._reading
        return self._reading


class App:
    """The sample app's action, inbox and confirm routes; each log goes to the meter."""

    def __init__(self, meter: SyncMeter) -> None:
        self.meter = meter
        self.tokens: dict[str, str] = {}

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/action":
            ref = request.headers["X-Playground-Ref"]
            self.tokens[ref] = f"tok-{ref}"
            self.meter.queue()
            return httpx.Response(200, json={"ok": True})
        if request.url.path == "/__playground/inbox":
            ref = request.url.params["ref"]
            return httpx.Response(200, json={"tokens": {ref: self.tokens.pop(ref)}})
        self.meter.queue()  # the confirm: the success log
        return httpx.Response(200, json={"ok": True})


def _visit(n: int) -> dict[str, Any]:
    envelope = {"encrypted": "e", "timestamp": 1, "nonce": f"n{n}", "v": 0}
    return {
        "ref": f"ref-{n}",
        "class": "real" if n % 4 == 0 else "attack",
        "user_id": f"pg-u-{n}",
        "client_ip": "192.0.2.1",
        # Every visitor succeeds at once: each adds both its logs.
        "script": {"success": True, "success_after_ms": 0},
        "envelopes": {"action_log": envelope, "success_log": envelope},
    }


@dataclass(frozen=True)
class Lease:
    at: float
    count: int
    unleased: int
    backlog: int  # what the pacer read for this decision
    pending: int  # the backlog plus what the leased visitors may still add
    true_backlog: int  # what was really queued then


def _pending(paced: PacedRun, backlog: int) -> int:
    not_started = len(paced.visits) - paced.tally.started
    under_way = paced.tally.started - paced.tally.done
    return backlog + LOGS_PER_VISIT_MAX * not_started + under_way


def _paced_run(
    size: int,
    meter: SyncMeter,
    *,
    use_within_s: float,
    lease_max: int = LEASE_MAX,
    max_queue_age_s: float = 600.0,
    refresh_every_s: float | None = None,
) -> tuple[PacedRun, list[Lease], int]:
    """Run ``size`` visitors; return the run, its leases and what was queued at the end."""
    paced = PacedRun(size)
    leases: list[Lease] = []
    handed = itertools.count()

    async def lease(count: int) -> list[dict[str, Any]]:
        # Read before the first await: exactly what the pacer saw.
        leases.append(
            Lease(
                at=time.monotonic(),
                count=count,
                unleased=size - len(paced.visits),
                backlog=meter.last_read,
                pending=_pending(paced, meter.last_read),
                true_backlog=meter.peek(),
            )
        )
        await asyncio.sleep(0)  # a real lease is a request
        return [_visit(next(handed)) for _ in range(count)]

    async def main() -> int:
        ticker = None
        if isinstance(meter, StaleMeter):
            assert refresh_every_s is not None

            async def tick() -> None:
                while True:
                    meter.refresh()
                    await asyncio.sleep(refresh_every_s)

            ticker = asyncio.ensure_future(tick())
        try:
            await paced.run(
                lease=lease,
                lease_max=lease_max,
                meter=meter,  # type: ignore[arg-type]
                use_within_s=use_within_s,
                max_queue_age_s=max_queue_age_s,
                base_url="http://app.test",
                control_token="t",
                stop=asyncio.Event(),
                run_id="run-1",
                transport=httpx.MockTransport(App(meter)),
            )
        finally:
            if ticker is not None:
                ticker.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await ticker
        return meter.peek()

    left = asyncio.run(main())
    return paced, leases, left


def _in_taper(leases: list[Lease], use_within_s: float) -> list[Lease]:
    """The leases from the first one decided in taper on."""
    for index, lease in enumerate(leases):
        if lease.unleased * LOGS_PER_VISIT_MAX <= use_within_s * RATE:
            return leases[index:]
    return []


def _check_taper(tapered: list[Lease]) -> None:
    allowance = taper_allowance()
    assert tapered, "the run never tapered"
    for lease in tapered:
        assert lease.pending <= allowance - LOGS_PER_VISIT_MAX, lease
        assert lease.count <= (allowance - lease.pending) // LOGS_PER_VISIT_MAX, lease
    for before, after in itertools.pairwise(tapered):
        assert after.at - before.at >= POLL_S - CLOCK_SLACK_S, (before, after)


def test_a_run_that_tapers_throughout_ends_with_a_short_queue():
    size, use_within_s = 200, 30.0  # the whole plan fits: it tapers from the start
    paced, leases, left = _paced_run(size, SyncMeter(), use_within_s=use_within_s)
    assert paced.tally.done == size and not paced.degraded and paced.stopped_by is None
    tapered = _in_taper(leases, use_within_s)
    assert tapered == leases
    _check_taper(tapered)
    assert max(lease.true_backlog for lease in leases) <= taper_allowance()
    assert left <= taper_allowance() + LOGS_PER_VISIT_MAX


def test_a_run_that_starts_tapering_part_way_holds_back_from_then_on():
    # 1,600 logs to come at 1,000 a second: the visitors outrun the SDK, and
    # the queue fills up to the usual allowance before the taper begins.
    size, use_within_s = 800, 0.5
    paced, leases, left = _paced_run(size, SyncMeter(), use_within_s=use_within_s)
    assert paced.tally.done == size and not paced.degraded
    tapered = _in_taper(leases, use_within_s)
    before = leases[: len(leases) - len(tapered)]
    assert before, "the run tapered from the start"
    # Before the taper, the batches are as big as they can be, and the queue
    # grew close to its usual allowance.
    assert all(lease.count == min(LEASE_MAX, lease.unleased) for lease in before)
    assert max(lease.true_backlog for lease in before) >= 0.8 * use_within_s * RATE
    # From then on it holds back until the queue is short, and keeps it short.
    _check_taper(tapered)
    assert left <= taper_allowance() + LOGS_PER_VISIT_MAX


def test_a_run_with_no_measured_rate_never_tapers():
    meter = SyncMeter(rate=0.0)
    paced, leases, _ = _paced_run(12, meter, use_within_s=30.0, lease_max=5)
    assert [lease.count for lease in leases] == [5, 5, 2]
    assert paced.tally.done == 12 and not paced.degraded


def test_a_meter_read_now_and_then_never_lets_the_rest_go_in_one_burst():
    size = 100
    meter = StaleMeter()
    paced, leases, _ = _paced_run(
        size, meter, use_within_s=30.0, lease_max=size, refresh_every_s=POLL_S
    )
    assert paced.tally.done == size and not paced.degraded
    per_lease = int(taper_allowance() // LOGS_PER_VISIT_MAX)
    assert len(leases) >= math.ceil(size / per_lease)
    assert all(lease.count <= per_lease for lease in leases)
    assert all(lease.count < lease.unleased for lease in leases[:-1])
    for before, after in itertools.pairwise(leases):
        assert after.at - before.at >= POLL_S - CLOCK_SLACK_S
    # What a late reading misses is at most the last batch's logs.
    assert max(lease.true_backlog for lease in leases) <= 2 * taper_allowance()


def test_a_taper_held_back_for_too_long_gives_way():
    # The reading is stuck just under the floor, so the taper never has room:
    # after max_queue_age_s it leases as before, and the run is not degraded.
    class Stuck(SyncMeter):
        @property
        def backlog(self) -> int:
            self.last_read = BACKLOG_FLOOR - 1
            return self.last_read

    max_queue_age_s = POLL_S * 10
    started = time.monotonic()
    paced, leases, _ = _paced_run(60, Stuck(), use_within_s=30.0, max_queue_age_s=max_queue_age_s)
    assert paced.tally.done == 60 and not paced.degraded
    assert leases[0].at - started >= max_queue_age_s  # it did hold back first
    assert [lease.count for lease in leases] == [LEASE_MAX] * 3
