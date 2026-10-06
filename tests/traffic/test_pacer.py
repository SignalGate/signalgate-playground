"""The pacer: leases a batch at a time, keeps the SDK backlog in bounds, stops when it can't.

Visitors reach a stand-in for the sample app through an httpx transport;
the backlog and drain rate come from a stand-in meter the test sets.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx

from playground.traffic.local_constants import BACKLOG_FLOOR
from playground.traffic.pacer import LeaseStopped, PacedRun

LEASE_MAX = 5


class _Meter:
    def __init__(self, backlog: int = 0, rate: float = 0.0) -> None:
        self.backlog = backlog
        self.rate = rate


def _visit(n: int) -> dict[str, Any]:
    envelope = {"encrypted": "e", "timestamp": 1, "nonce": f"n{n}", "v": 0}
    return {
        "ref": f"ref-{n}",
        "class": "real" if n % 4 == 0 else "attack",
        "user_id": f"pg-u-{n}",
        "client_ip": "192.0.2.1",
        "script": {"success": False, "success_after_ms": 0},
        "envelopes": {"action_log": envelope},
    }


class _App:
    """The sample app's action route, recording what each visitor sent."""

    def __init__(self) -> None:
        self.bodies: list[dict[str, Any]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.bodies.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})


def _run(size: int, meter: _Meter, lease, *, max_queue_age_s: float = 60.0):
    app = _App()
    paced = PacedRun(size)
    result = asyncio.run(
        paced.run(
            lease=lease,
            lease_max=LEASE_MAX,
            meter=meter,  # type: ignore[arg-type]
            use_within_s=10.0,
            max_queue_age_s=max_queue_age_s,
            base_url="http://app.test",
            control_token="t",
            stop=asyncio.Event(),
            run_id="run-1",
            transport=httpx.MockTransport(app),
        )
    )
    return paced, result, app


def _leaser():
    counts: list[int] = []
    handed = [0]

    async def lease(count: int) -> list[dict[str, Any]]:
        counts.append(count)
        batch = [_visit(handed[0] + i) for i in range(count)]
        handed[0] += count
        return batch

    return lease, counts


def test_a_run_leases_batch_by_batch_and_sends_each_envelope_where_it_goes():
    lease, counts = _leaser()
    paced, result, app = _run(12, _Meter(), lease)
    assert counts == [5, 5, 2]
    assert (result.total, result.done, result.actions) == (12, 12, 12)
    assert all(body["signalgate_log"]["nonce"] for body in app.bodies)
    assert all("signalgate" not in body for body in app.bodies)
    assert not paced.degraded and paced.stopped_by is None
    assert result.real == 3 and result.attack == 9


def test_a_backlog_that_would_age_out_stops_the_run_as_degraded():
    lease, counts = _leaser()
    paced, result, _ = _run(12, _Meter(backlog=10_000, rate=1.0), lease, max_queue_age_s=60.0)
    assert paced.degraded and counts == [] and result.done == 0


def test_a_backlog_that_stops_draining_stops_the_run_as_degraded():
    lease, counts = _leaser()
    stuck = _Meter(backlog=BACKLOG_FLOOR + 1, rate=0.0)
    paced, _, _ = _run(12, stuck, lease, max_queue_age_s=0.01)
    assert paced.degraded and counts == []


def test_a_refused_lease_stops_the_run_and_says_why():
    async def lease(count: int) -> list[dict[str, Any]]:
        raise LeaseStopped("KEY_ROTATED")

    paced, result, _ = _run(12, _Meter(), lease)
    assert paced.stopped_by == "KEY_ROTATED" and result.done == 0
