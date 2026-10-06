"""Stopping the real sample app closes its real SDK client and its worker thread.

The app runs from a temp copy of the tree with a route that logs through
``sg()`` and a dummy key, started by the console's own launcher with the
exact command line and a stdin pipe. The start-up hook answers the SDK's
calls locally and records the SDK worker threads around the client's close
and at the app's exit. After one logged action:

- the shared client exists and its worker thread runs;
- the console's HTTP-first stop gets the post-close counters back, and the
  app exits by itself with code 0 well inside the grace, never killed;
- the worker thread is gone after the close and at exit.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import httpx
import pytest

from playground.supervisor.control_client import KEY_ENQUEUED, KEY_SENT
from playground.supervisor.launcher import AppProcess
from playground.traffic.local_constants import APP_STOP_GRACE_S
from tests.contract.samples import SHUTDOWN, assert_same_shape
from tests.e2e.conftest import LOGGED_ONE_IN, use_logging_app
from tests.e2e.seam import SEAM_ENV, Seam, python_path

pytestmark = pytest.mark.e2e

WORKER = "signalgate-log-worker"


async def _log_then_stop(tree: Path) -> dict:
    app = AppProcess(tree, tree / ".playground" / "app.log")
    base_url, _token, _boot_id = await app.start()
    try:
        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as visitor:
            for number in range(LOGGED_ONE_IN):
                response = await visitor.post(
                    "/api/action",
                    json={"user": f"user-{number}"},
                    headers={"X-Playground-Ref": f"ref-{number}"},
                )
                assert response.status_code == 200
        assert app.control is not None
        health = await app.control.health()
        started = time.monotonic()
        stopped = await app.stop()
        return {
            "health": health,
            "stop": stopped,
            "elapsed": time.monotonic() - started,
            "pid": app.pid,
        }
    finally:
        await app.stop()


def test_stopping_the_app_closes_its_sdk_client_and_worker(tracked_copy, seam: Seam, monkeypatch):
    use_logging_app(tracked_copy)
    monkeypatch.setenv("PYTHONPATH", python_path(seam.hook_dir, tracked_copy))
    monkeypatch.setenv(SEAM_ENV, str(seam.config_path))

    result = asyncio.run(_log_then_stop(tracked_copy))

    health = result["health"]
    assert (health.clients_created, health.client_ready) == (1, True)

    stopped = result["stop"]
    assert stopped is not None
    assert stopped.killed is False
    assert stopped.returncode == 0
    assert result["elapsed"] < APP_STOP_GRACE_S
    reply = stopped.shutdown
    assert reply is not None
    assert_same_shape(
        {
            "ok": reply.ok,
            "boot_id": reply.boot_id,
            "closed": reply.closed,
            "metrics": reply.metrics,
        },
        SHUTDOWN,
    )
    assert reply.closed is True
    assert reply.metrics is not None
    assert reply.metrics[KEY_ENQUEUED] == 1
    assert reply.metrics[KEY_SENT] == 1

    # The first close found the worker running and left none; any later
    # close (the app's own on the way out) is a no-op.
    closes = seam.closed(result["pid"])
    assert closes[0]["workers_before"] == [WORKER]
    assert all(close["workers_after"] == [] for close in closes)
    assert all(close["metrics"] == reply.metrics for close in closes)
    assert seam.exit_record(result["pid"]) == {"pid": result["pid"], "workers": []}
    log = (tracked_copy / ".playground" / "app.log").read_text()
    assert "Application shutdown complete" in log
