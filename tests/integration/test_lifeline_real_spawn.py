"""The real sample app, started by the console's own launcher, looks after itself.

The app is spawned exactly as the console spawns it (``AppProcess``: asyncio,
the frozen command line, a stdin pipe the console holds), from a temp copy of
the tracked tree. On the integrated tree:

- its boot self-test passes every check, the exit hook included;
- when the console's end of the stdin pipe closes, which is what happens
  when the console is killed, the app notices and exits by itself, cleanly.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path

import pytest

from playground.supervisor.launcher import AppProcess
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_STOP_GRACE_S
from tests.e2e.conftest import copy_tracked_tree

SELF_TEST_CHECKS = (
    "sdk_version",
    "transport_import",
    "transport_signature",
    "transport_protocol",
    "facade",
    "client_hook",
    "probe_hooks",
    "exit_hook",
)


@pytest.fixture
def tree(tmp_path: Path) -> Path:
    return copy_tracked_tree(tmp_path / "tree")


async def _close_the_lifeline(tree: Path) -> dict:
    app = AppProcess(tree, tree / ".playground" / "app.log")
    await app.start()
    try:
        assert app.control is not None
        health = await app.control.health()
        process = app._process
        assert process is not None and process.stdin is not None

        process.stdin.close()  # the console's end of the pipe, as a kill -9 leaves it
        started = time.monotonic()
        while process.returncode is None and time.monotonic() - started < APP_STOP_GRACE_S:
            await asyncio.sleep(APP_HEALTH_POLL_S)
        return {
            "health": health,
            "returncode": process.returncode,
            "elapsed": time.monotonic() - started,
            "exited_by_itself": app.exited,
        }
    finally:
        await app.stop()
        if app.control is not None:
            await app.control.aclose()


def test_the_real_app_passes_its_self_test_and_exits_when_its_lifeline_closes(tree):
    result = asyncio.run(_close_the_lifeline(tree))

    health = result["health"]
    assert health.self_test == {
        "ok": True,
        "failed": [],
        "checks": dict.fromkeys(SELF_TEST_CHECKS, "pass"),
    }
    assert health.clients_created == 0
    assert health.client_ready is False

    assert result["exited_by_itself"] is True
    assert result["returncode"] == 0
    assert result["elapsed"] < APP_STOP_GRACE_S
    log = (tree / ".playground" / "app.log").read_text()
    assert "Application shutdown complete" in log
    assert "Traceback" not in log
