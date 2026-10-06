"""Subprocess tests for POST /__playground/shutdown and the stdin lifeline.

The lifeline-carrying tests spawn the app through
``asyncio.create_subprocess_exec`` with ``stdin=PIPE``: the same launch path
the console uses, so the pipe behaves exactly as it does for the real
console. The ``request_exit()`` and gate sections below them are plain, fast
unit tests of ``lifecycle.request_exit`` and ``lifecycle._stdin_is_pipe_or_socket``
and need no subprocess at all.

Every wait below is bounded by the console's own constants
(``playground/traffic/local_constants.py``), not a separate literal: a
graceful exit -- through ``/shutdown`` or the lifeline -- must land inside
``APP_STOP_GRACE_S``, the same bound the real console gives a stop before it
kills the child.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import urllib.error
import urllib.request
from pathlib import Path

from app.kit import lifecycle
from playground.supervisor.launcher import app_command
from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    APP_START_TIMEOUT_S,
    APP_STOP_GRACE_S,
)
from tests.contract.samples import HEALTH, SHUTDOWN, UNAUTHORIZED, assert_same_shape

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_TOKEN = "shutdown-lifeline-token"
APP_TARGET = "app.main:app"
SDK_WORKER_APP_TARGET = "tests.app.fixtures.control.sdk_worker_app:app"
HANGING_APP_TARGET = "tests.app.fixtures.control.hanging_shutdown_app:app"

# How many health polls to keep watching a process that must *not* exit,
# before believing it: a count, not a duration -- the wait it adds up to is
# derived from the same poll interval every health wait already uses.
_STAYS_UP_POLL_COUNT = 23


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _get(url: str, headers: dict[str, str]) -> tuple[int, dict]:
    request = urllib.request.Request(url, headers=headers)
    return _post(request)


def _post(request: urllib.request.Request) -> tuple[int, dict]:
    try:
        with urllib.request.urlopen(request) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


async def _wait_healthy(url: str, headers: dict[str, str], within_s: float) -> dict:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + within_s
    while True:
        try:
            status, body = await asyncio.to_thread(_get, url, headers)
            if status == 200:
                return body
        except OSError:
            pass
        if loop.time() >= deadline:
            raise AssertionError(f"{url} did not answer in time")
        await asyncio.sleep(APP_HEALTH_POLL_S)


async def _wait_exit(process: asyncio.subprocess.Process, within_s: float) -> int:
    return await asyncio.wait_for(process.wait(), timeout=within_s)


def _launch_command(port: int, target: str) -> list[str]:
    """The console's exact launch command, serving `target` instead of the
    sample app when a fixture needs to stand in for it."""
    command = app_command(port)
    assert command.count(APP_TARGET) == 1
    return [target if part == APP_TARGET else part for part in command]


async def _spawn_app_main(
    port: int, *, lifeline: bool, stdin, target: str = APP_TARGET
) -> asyncio.subprocess.Process:
    env = dict(os.environ) | {"PLAYGROUND_CONTROL_TOKEN": CONTROL_TOKEN}
    if lifeline:
        env["PLAYGROUND_LIFELINE"] = "1"
    else:
        env.pop("PLAYGROUND_LIFELINE", None)
    return await asyncio.create_subprocess_exec(
        *_launch_command(port, target),
        cwd=REPO_ROOT,
        env=env,
        stdin=stdin,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


async def _kill_quietly(process: asyncio.subprocess.Process) -> None:
    if process.returncode is None:
        process.kill()
        await process.wait()


# --- POST /shutdown, against the real app --------------------------------------


async def _shutdown_makes_the_app_exit_by_itself() -> tuple[int, dict]:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    process = await _spawn_app_main(port, lifeline=True, stdin=asyncio.subprocess.PIPE)
    try:
        health = await _wait_healthy(f"{base_url}/__playground/health", auth, APP_START_TIMEOUT_S)

        # A wrong token holding a raw non-ASCII byte, exactly as a real
        # server receives it (the test client re-encodes header values).
        wrong_status, wrong_body = await asyncio.to_thread(
            _get, f"{base_url}/__playground/health", {"Authorization": "Bearer \xe9"}
        )

        request = urllib.request.Request(
            f"{base_url}/__playground/shutdown", data=b"", headers=auth, method="POST"
        )
        status, body = await asyncio.to_thread(_post, request)

        returncode = await _wait_exit(process, APP_STOP_GRACE_S)
        output = (await process.stdout.read()).decode(errors="replace")
        return status, {
            "body": body,
            "health": health,
            "wrong_token": (wrong_status, wrong_body),
            "returncode": returncode,
            "output": output,
        }
    finally:
        await _kill_quietly(process)


def test_post_shutdown_exits_the_process_by_itself():
    status, result = asyncio.run(_shutdown_makes_the_app_exit_by_itself())
    assert status == 200
    body = result["body"]
    assert_same_shape(body, SHUTDOWN)
    assert body["ok"] is True
    assert body["closed"] is False  # no client was ever created in this boot
    assert body["metrics"] is None
    assert result["returncode"] == 0
    assert "Application shutdown complete" in result["output"]
    assert "Traceback" not in result["output"]
    # Read before the shutdown call: under this real uvicorn server the
    # exit-hook probe (used only under TestClient elsewhere) reads "pass".
    assert_same_shape(result["health"], HEALTH)
    assert result["health"]["self_test"]["checks"]["exit_hook"] == "pass"
    wrong_status, wrong_body = result["wrong_token"]
    assert wrong_status == 401
    assert_same_shape(wrong_body, UNAUTHORIZED)


# --- the stdin lifeline, against the real app, and the SDK's own worker thread --


async def _lifeline_closes_the_app_when_the_write_end_closes() -> int:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    process = await _spawn_app_main(port, lifeline=True, stdin=asyncio.subprocess.PIPE)
    try:
        await _wait_healthy(f"{base_url}/__playground/health", auth, APP_START_TIMEOUT_S)
        process.stdin.close()
        await process.stdin.wait_closed()
        return await _wait_exit(process, APP_STOP_GRACE_S)
    finally:
        await _kill_quietly(process)


def test_lifeline_closes_the_app_when_the_write_end_closes():
    returncode = asyncio.run(_lifeline_closes_the_app_when_the_write_end_closes())
    assert returncode == 0


async def _shutdown_stops_the_sdk_worker_thread() -> dict:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    process = await _spawn_app_main(
        port, lifeline=True, stdin=asyncio.subprocess.PIPE, target=SDK_WORKER_APP_TARGET
    )
    try:
        health = await _wait_healthy(f"{base_url}/__playground/health", auth, APP_START_TIMEOUT_S)
        _, before = await asyncio.to_thread(_get, f"{base_url}/fixture/worker-threads", {})

        request = urllib.request.Request(
            f"{base_url}/__playground/shutdown", data=b"", headers=auth, method="POST"
        )
        status, body = await asyncio.to_thread(_post, request)

        returncode = await _wait_exit(process, APP_STOP_GRACE_S)
        output = (await process.stdout.read()).decode(errors="replace")
        return {
            "health": health,
            "before": before,
            "status": status,
            "body": body,
            "returncode": returncode,
            "output": output,
        }
    finally:
        await _kill_quietly(process)


def test_shutdown_stops_the_real_sdk_worker_thread():
    """Against a real ``signalgate.Client`` with its background worker busy
    sending, launched with the console's own command line and the lifeline
    on: the worker is running right before ``/shutdown``, the route closes
    the client and reports its final counters, and no worker thread is left
    by the time the process exits by itself."""
    result = asyncio.run(_shutdown_stops_the_sdk_worker_thread())
    worker = "signalgate-log-worker"

    # The positive control: exactly one worker existed before the shutdown.
    assert result["before"]["threads"] == [worker]
    assert f"sdk-worker-threads-after-start-up: {[worker]}" in result["output"]
    assert_same_shape(result["health"], HEALTH)

    assert result["status"] == 200
    body = result["body"]
    assert_same_shape(body, SHUTDOWN)
    assert body["closed"] is True
    assert body["metrics"].get("log_sent_total") == result["before"]["events_logged"]

    assert result["returncode"] == 0
    assert "Application shutdown complete" in result["output"]
    assert "sdk-worker-threads-at-exit: []" in result["output"]


async def _app_stays_up_after_stdin_closes(*, lifeline: bool, stdin) -> bool:
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    auth = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
    process = await _spawn_app_main(port, lifeline=lifeline, stdin=stdin)
    try:
        await _wait_healthy(f"{base_url}/__playground/health", auth, APP_START_TIMEOUT_S)
        if stdin is asyncio.subprocess.PIPE:
            process.stdin.close()
            await process.stdin.wait_closed()
        for _ in range(_STAYS_UP_POLL_COUNT):
            if process.returncode is not None:
                return False
            await asyncio.sleep(APP_HEALTH_POLL_S)
        status, _ = await asyncio.to_thread(_get, f"{base_url}/__playground/health", auth)
        return status == 200 and process.returncode is None
    finally:
        await _kill_quietly(process)


def test_lifeline_off_keeps_the_app_serving_even_after_stdin_closes():
    """With the flag off, `start_lifeline()` never starts a thread, so
    closing the write end -- the one thing that would end the app if the
    flag were on -- has no effect at all."""
    still_up = asyncio.run(
        _app_stays_up_after_stdin_closes(lifeline=False, stdin=asyncio.subprocess.PIPE)
    )
    assert still_up is True


def test_a_regular_file_stdin_keeps_the_app_serving_even_with_the_flag_on():
    with open(os.devnull) as devnull:
        still_up = asyncio.run(_app_stays_up_after_stdin_closes(lifeline=True, stdin=devnull))
    assert still_up is True


# --- the lifeline's os._exit(1) fallback, against a fixture whose shutdown hangs --


async def _forced_exit_when_shutdown_hangs() -> int:
    port = _free_port()
    process = await _spawn_app_main(
        port, lifeline=True, stdin=asyncio.subprocess.PIPE, target=HANGING_APP_TARGET
    )
    try:
        await _wait_healthy(f"http://127.0.0.1:{port}/health", {}, APP_START_TIMEOUT_S)
        process.stdin.close()
        await process.stdin.wait_closed()
        # The fixture's own shutdown hangs well past its patched grace; only
        # the lifeline's os._exit(1) fallback can end this in time.
        return await _wait_exit(process, APP_STOP_GRACE_S)
    finally:
        await _kill_quietly(process)


def test_lifeline_forces_the_exit_when_shutdown_hangs():
    returncode = asyncio.run(_forced_exit_when_shutdown_hangs())
    # os._exit(1): forced, not the lifespan's own graceful 0.
    assert returncode == 1


# --- request_exit()'s own contract (no subprocess needed) ----------------------


def test_request_exit_sets_should_exit_when_a_server_is_found(monkeypatch):
    import signal as signal_module

    class FakeServer:
        should_exit = False

    server = FakeServer()

    def fake_handler(*_args, **_kwargs):
        raise AssertionError("never called; only .__self__ is read")

    fake_handler.__self__ = server
    signals_sent: list[int] = []
    monkeypatch.setattr(signal_module, "getsignal", lambda _sig: fake_handler)
    monkeypatch.setattr(signal_module, "raise_signal", lambda sig: signals_sent.append(sig))

    lifecycle.request_exit()

    assert server.should_exit is True
    assert signals_sent == []


def test_request_exit_sends_exactly_one_sigint_when_no_server_is_found_and_the_flag_is_on(
    monkeypatch,
):
    import signal as signal_module

    monkeypatch.setattr(signal_module, "getsignal", lambda _sig: signal_module.SIG_DFL)
    signals_sent: list[int] = []
    monkeypatch.setattr(signal_module, "raise_signal", lambda sig: signals_sent.append(sig))
    monkeypatch.setenv(lifecycle.LIFELINE_ENV, "1")

    lifecycle.request_exit()
    lifecycle.request_exit()  # the once-guard: a second call never sends again

    assert signals_sent == [signal_module.SIGINT]


def test_request_exit_does_nothing_when_no_server_is_found_and_the_flag_is_off(monkeypatch):
    import signal as signal_module

    monkeypatch.setattr(signal_module, "getsignal", lambda _sig: signal_module.SIG_DFL)
    signals_sent: list[int] = []
    monkeypatch.setattr(signal_module, "raise_signal", lambda sig: signals_sent.append(sig))
    monkeypatch.delenv(lifecycle.LIFELINE_ENV, raising=False)

    lifecycle.request_exit()

    assert signals_sent == []


# --- the gate: what counts as "a pipe or a socket" (no subprocess needed) --------


def test_gate_is_on_for_a_fifo(tmp_path):
    fifo_path = tmp_path / "lifeline.fifo"
    os.mkfifo(fifo_path)
    fd = os.open(fifo_path, os.O_RDONLY | os.O_NONBLOCK)
    try:
        assert lifecycle._stdin_is_pipe_or_socket(fd) is True
    finally:
        os.close(fd)


def test_gate_is_on_for_a_socket():
    left, right = socket.socketpair()
    try:
        assert lifecycle._stdin_is_pipe_or_socket(left.fileno()) is True
    finally:
        left.close()
        right.close()


def test_gate_is_off_for_a_regular_file(tmp_path):
    path = tmp_path / "not-a-pipe.txt"
    path.write_text("x")
    fd = os.open(path, os.O_RDONLY)
    try:
        assert lifecycle._stdin_is_pipe_or_socket(fd) is False
    finally:
        os.close(fd)


def test_gate_is_off_for_dev_null():
    fd = os.open(os.devnull, os.O_RDONLY)
    try:
        assert lifecycle._stdin_is_pipe_or_socket(fd) is False
    finally:
        os.close(fd)


def test_gate_is_off_for_a_closed_descriptor():
    fd = os.open(os.devnull, os.O_RDONLY)
    os.close(fd)
    assert lifecycle._stdin_is_pipe_or_socket(fd) is False
