"""`/health` (and its `_authorize` dependency) never wait on the AnyIO
threadpool, and answering it is delayed only by something that blocks the
loop directly -- never by a `def` route elsewhere doing its own work.

Both timed cases size their own block duration and budget from a warm,
unblocked read of this same fixture app, and synchronise with the blocking
handlers through a barrier or an event rather than a guessed sleep, so
nothing here describes a real network wait: only how this process schedules
its own work.
"""

from __future__ import annotations

import inspect
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app.kit import control, lifecycle
from app.kit.local_constants import CLIENT_CLOSE_TIMEOUT_S
from playground.traffic.local_constants import APP_HEALTH_POLL_S
from tests.app.fixtures.control import blocking_app as blocking_app_module
from tests.app.fixtures.control.blocking_app import THREADPOOL_SIZE
from tests.app.fixtures.control.blocking_app import app as blocking_app
from tests.contract.samples import HEALTH, SHUTDOWN, assert_same_shape

CONTROL_TOKEN = "health-loop-token"

# How much longer than a warm, unblocked health read the block should run,
# and how small a fraction of that block a fast health answer must beat.
BLOCK_FACTOR = 80
BUDGET_FRACTION = 5

# A floor under the block duration, in case a warm read is too fast to
# multiply into a duration `time.sleep` can tell apart from zero: half of
# the console's own health-poll interval.
MINIMUM_BLOCK_S = APP_HEALTH_POLL_S / 2

# However slow a machine's own warm-read baseline is, health must answer
# within a few of the console's health-poll intervals while the threadpool
# is saturated. The relative budget above stays as the tighter,
# environment-scaled check; this absolute one never loosens.
HEALTH_LATENCY_BUDGET_S = APP_HEALTH_POLL_S * 4


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture
def blocking_client(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", CONTROL_TOKEN)
    monkeypatch.setattr(blocking_app_module, "ready_barrier", None)
    blocking_app_module.loop_block_started.clear()
    blocking_app_module.release_threads.clear()
    with TestClient(blocking_app) as test_client:
        yield test_client


def _warm_health_latency_s(client: TestClient) -> float:
    """One unblocked health read: this test's own unit of time."""
    started = time.perf_counter()
    response = client.get("/__playground/health", headers=_auth(CONTROL_TOKEN))
    assert response.status_code == 200
    return time.perf_counter() - started


def _block_duration_s(baseline_s: float) -> float:
    return max(baseline_s * BLOCK_FACTOR, MINIMUM_BLOCK_S)


def test_authorize_and_health_are_coroutine_functions():
    assert inspect.iscoroutinefunction(control._authorize)
    assert inspect.iscoroutinefunction(control.health)


def test_health_answers_fast_while_the_threadpool_is_saturated(
    blocking_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    block_s = _block_duration_s(_warm_health_latency_s(blocking_client))
    # Whichever is tighter: the relative budget scales with a slow machine's
    # own baseline, but must never loosen past the absolute one.
    budget_s = min(block_s / BUDGET_FRACTION, HEALTH_LATENCY_BUDGET_S)

    barrier = threading.Barrier(THREADPOOL_SIZE + 1)
    monkeypatch.setattr(blocking_app_module, "ready_barrier", barrier)

    threads = [
        threading.Thread(
            target=blocking_client.post,
            args=("/block-thread",),
            kwargs={"params": {"seconds": block_s}},
        )
        for _ in range(THREADPOOL_SIZE)
    ]
    for thread in threads:
        thread.start()

    # Every thread-pool slot is now occupied by a `/block-thread` call
    # sleeping inside it: the pool is saturated, no guessing involved.
    barrier.wait()

    started = time.perf_counter()
    response = blocking_client.get("/__playground/health", headers=_auth(CONTROL_TOKEN))
    elapsed = time.perf_counter() - started

    for thread in threads:
        thread.join()

    assert response.status_code == 200
    assert_same_shape(response.json(), HEALTH)
    assert elapsed < budget_s


def test_an_async_handler_blocking_the_loop_delays_health(blocking_client: TestClient):
    block_s = _block_duration_s(_warm_health_latency_s(blocking_client))

    thread = threading.Thread(
        target=blocking_client.post,
        args=("/block-loop",),
        kwargs={"params": {"seconds": block_s}},
    )
    thread.start()

    # The loop is now inside the blocking handler's `time.sleep`.
    blocking_app_module.loop_block_started.wait()

    started = time.perf_counter()
    response = blocking_client.get("/__playground/health", headers=_auth(CONTROL_TOKEN))
    elapsed = time.perf_counter() - started

    thread.join()

    assert response.status_code == 200
    assert_same_shape(response.json(), HEALTH)
    assert elapsed >= block_s


def test_shutdown_asks_to_exit_while_the_threadpool_is_still_saturated(
    blocking_client: TestClient, monkeypatch: pytest.MonkeyPatch
):
    """`/shutdown`'s exit request runs on the event loop, not in a thread-pool
    slot: with every slot held by a slow `def` handler, the request is made
    while they are all still there, instead of waiting for one to free up."""
    slots_held_at_exit_request: list[int] = []
    monkeypatch.setattr(
        lifecycle,
        "request_exit",
        lambda: slots_held_at_exit_request.append(blocking_app_module.threads_in_pool()),
    )
    barrier = threading.Barrier(THREADPOOL_SIZE + 1)
    monkeypatch.setattr(blocking_app_module, "ready_barrier", barrier)

    # Held until released below; the cap only matters if the exit request
    # did wait for a slot, and then it ends the wait instead of hanging.
    threads = [
        threading.Thread(
            target=blocking_client.post,
            args=("/block-thread",),
            kwargs={"params": {"seconds": CLIENT_CLOSE_TIMEOUT_S}},
        )
        for _ in range(THREADPOOL_SIZE)
    ]
    for thread in threads:
        thread.start()
    barrier.wait()

    try:
        response = blocking_client.post("/__playground/shutdown", headers=_auth(CONTROL_TOKEN))
    finally:
        blocking_app_module.release_threads.set()
        for thread in threads:
            thread.join()

    assert response.status_code == 200
    assert_same_shape(response.json(), SHUTDOWN)
    assert slots_held_at_exit_request == [THREADPOOL_SIZE]
