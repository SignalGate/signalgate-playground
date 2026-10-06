"""Tests for the sample-app launcher, against tiny stub ASGI apps."""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import signal
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

import playground.supervisor.drain as drain_module
import playground.supervisor.launcher as launcher_module
from playground.supervisor.launcher import (
    AppProcess,
    AppStartError,
    AppUnavailable,
    app_command,
)
from playground.traffic.local_constants import APP_STOP_GRACE_S
from tests.supervisor._clock import RecordingClock

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEALTHY_ROOT = FIXTURES / "healthy"
BROKEN_ROOT = FIXTURES / "broken"
STUBBORN_ROOT = FIXTURES / "stubborn"
WEDGED_ROOT = FIXTURES / "wedged"
GRANDCHILD_ROOT = FIXTURES / "grandchild"
NO_SHUTDOWN_ROOT = FIXTURES / "no_shutdown"

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_app_command_is_the_exact_frozen_launch_line():
    command = app_command(54321)
    assert command == [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        "54321",
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1",
        "--no-access-log",
    ]


def test_start_health_stop_leaves_no_process_behind(tmp_path):
    asyncio.run(_start_health_stop_leaves_no_process_behind(tmp_path))


async def _start_health_stop_leaves_no_process_behind(tmp_path: Path) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")

    base_url, token, boot_id = await process.start()
    assert boot_id == "stub-boot-id"
    assert token

    async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
        response = await client.get(
            "/__playground/health", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.json()["boot_id"] == "stub-boot-id"

    pid = process.pid
    assert pid is not None

    await process.stop()

    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    # Idempotent: stopping an already-stopped process is a no-op.
    await process.stop()


def test_start_raises_when_the_app_exits_before_it_is_healthy(tmp_path):
    asyncio.run(_start_raises_when_the_app_exits_before_it_is_healthy(tmp_path))


async def _start_raises_when_the_app_exits_before_it_is_healthy(tmp_path: Path) -> None:
    process = AppProcess(root=BROKEN_ROOT, log_path=tmp_path / "broken.log")

    with pytest.raises(AppStartError) as caught:
        await process.start()

    # The error carries what the app's output said about why it didn't start.
    failure = caught.value.failure
    assert failure is not None
    assert failure.error == "RuntimeError"
    assert failure.message == "stub app intentionally fails to start"
    assert failure.location == caught.value.location == "app/main.py:3"
    assert failure.source is not None
    assert failure.source.strip() == 'raise RuntimeError("stub app intentionally fails to start")'
    assert failure.hint is None

    # The launcher must not leave the failed process behind either.
    pid = process.pid
    assert pid is not None
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


# -- Launch -------------------------------------------------------------------


def test_env_carries_a_fresh_token_and_the_lifeline_flag(tmp_path, monkeypatch):
    asyncio.run(_env_carries_a_fresh_token_and_the_lifeline_flag(tmp_path, monkeypatch))


async def _env_carries_a_fresh_token_and_the_lifeline_flag(tmp_path: Path, monkeypatch) -> None:
    seen_env: dict[str, str] = {}
    real_exec = asyncio.create_subprocess_exec

    async def spy_exec(*args, **kwargs):
        seen_env.update(kwargs.get("env") or {})
        return await real_exec(*args, **kwargs)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", spy_exec)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        _base_url, token, _boot_id = await process.start()
        assert seen_env[launcher_module.CONTROL_TOKEN_ENV_VAR] == token
        assert seen_env[launcher_module.LIFELINE_ENV_VAR] == "1"
    finally:
        await process.stop()


def test_stdin_is_a_pipe(tmp_path):
    asyncio.run(_stdin_is_a_pipe(tmp_path))


async def _stdin_is_a_pipe(tmp_path: Path) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        assert process._process is not None
        assert isinstance(process._process.stdin, asyncio.StreamWriter)
    finally:
        await process.stop()


def test_ps_shows_neither_the_token_nor_a_key(tmp_path, monkeypatch):
    asyncio.run(_ps_shows_neither_the_token_nor_a_key(tmp_path, monkeypatch))


async def _ps_shows_neither_the_token_nor_a_key(tmp_path: Path, monkeypatch) -> None:
    # A dummy, non-key-looking value: proves the child's argv is clean even
    # when a real key is sitting in the parent's own environment, not merely
    # that the variable's *name* never appears there.
    dummy_key = "not-a-real-key-just-a-test-value"
    monkeypatch.setenv("SIGNALGATE_API_KEY", dummy_key)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        _base_url, token, _boot_id = await process.start()
        pid = process.pid
        assert pid is not None

        args = await asyncio.to_thread(
            lambda: (
                subprocess.run(
                    ["ps", "-o", "args=", "-p", str(pid)],
                    capture_output=True,
                    text=True,
                    check=True,
                ).stdout
            )
        )

        assert token not in args
        assert "--key" not in args
        assert "SIGNALGATE_API_KEY" not in args
        assert dummy_key not in args
    finally:
        await process.stop()


# -- Port -----------------------------------------------------------------------


def test_a_pre_occupied_port_moves_to_a_healthy_app_on_another_port(tmp_path, monkeypatch):
    asyncio.run(_a_pre_occupied_port_moves_to_a_healthy_app_on_another_port(tmp_path, monkeypatch))


async def _a_pre_occupied_port_moves_to_a_healthy_app_on_another_port(
    tmp_path: Path, monkeypatch
) -> None:
    import socket as socket_module

    occupied_socket = socket_module.socket(socket_module.AF_INET, socket_module.SOCK_STREAM)
    occupied_socket.bind(("127.0.0.1", 0))
    occupied_socket.listen(1)
    occupied_port = occupied_socket.getsockname()[1]

    real_free_port = launcher_module._free_port
    calls = {"n": 0}

    def patched_free_port() -> int:
        calls["n"] += 1
        if calls["n"] == 1:
            return occupied_port
        return real_free_port()

    monkeypatch.setattr(launcher_module, "_free_port", patched_free_port)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        base_url, token, boot_id = await process.start()
        assert boot_id == "stub-boot-id"
        assert f":{occupied_port}" not in base_url

        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            response = await client.get(
                "/__playground/health", headers={"Authorization": f"Bearer {token}"}
            )
            assert response.status_code == 200
    finally:
        await process.stop()
        occupied_socket.close()


def _silent_listener() -> socket.socket:
    """A loopback socket that accepts connections into its backlog and never
    answers: a port held by something that isn't the sample app."""
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.bind(("127.0.0.1", 0))
    listener.listen(socket.SOMAXCONN)
    return listener


def test_a_health_request_is_abandoned_as_soon_as_the_child_exits(tmp_path, monkeypatch):
    asyncio.run(_a_health_request_is_abandoned_as_soon_as_the_child_exits(tmp_path, monkeypatch))


async def _a_health_request_is_abandoned_as_soon_as_the_child_exits(
    tmp_path: Path, monkeypatch
) -> None:
    poll = launcher_module.APP_HEALTH_POLL_S
    # A request that is never answered could otherwise wait this long.
    monkeypatch.setattr(
        launcher_module, "LOOPBACK_REQUEST_TIMEOUT_S", launcher_module.APP_START_TIMEOUT_S
    )
    listener = _silent_listener()
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    child = SimpleNamespace(returncode=None)

    async def exit_soon() -> None:
        await asyncio.sleep(poll * 3)
        child.returncode = -signal.SIGTERM

    app = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    exiter = asyncio.ensure_future(exit_soon())
    try:
        started = time.monotonic()
        with pytest.raises(launcher_module._EarlyExit):
            await app._wait_healthy(base_url, "tok", child)
        assert time.monotonic() - started < poll * 37
    finally:
        await exiter
        listener.close()


def test_a_health_request_never_outlives_the_start_timeout(tmp_path, monkeypatch):
    asyncio.run(_a_health_request_never_outlives_the_start_timeout(tmp_path, monkeypatch))


async def _a_health_request_never_outlives_the_start_timeout(tmp_path: Path, monkeypatch) -> None:
    small_timeout = launcher_module.APP_HEALTH_POLL_S * 7
    monkeypatch.setattr(launcher_module, "APP_START_TIMEOUT_S", small_timeout)
    monkeypatch.setattr(launcher_module, "LOOPBACK_REQUEST_TIMEOUT_S", small_timeout * 613)
    listener = _silent_listener()
    base_url = f"http://127.0.0.1:{listener.getsockname()[1]}"
    never_exits = SimpleNamespace(returncode=None)

    app = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        started = time.monotonic()
        with pytest.raises(launcher_module._HealthTimeout):
            await app._wait_healthy(base_url, "tok", never_exits)
        assert time.monotonic() - started < small_timeout * 3
    finally:
        listener.close()


# Port reuse across a genuine restart is exercised against the real app
# below (its boot id actually varies per process, unlike this fixture's).


# -- Stop -----------------------------------------------------------------------


def _read_fixture_log(path: Path) -> list[dict]:
    """Every event the fixture has finished writing so far.

    The fixture may be part-way through appending a line while a test reads
    the log, so only newline-terminated lines count.
    """
    if not path.exists():
        return []
    *complete, _unfinished = path.read_text(encoding="utf-8").split("\n")
    return [json.loads(line) for line in complete if line.strip()]


def _script_metrics(path: Path, counters: dict) -> None:
    """Replace the fixture's scripted metrics in one step.

    The fixture reads this file on every poll, possibly while a test is
    rewriting it, so it must only ever see the old counters or the new
    ones: written to a sibling file first, then swapped into place.
    """
    staging = path.with_name(path.name + ".next")
    staging.write_text(json.dumps(counters), encoding="utf-8")
    os.replace(staging, path)


def _queued_in(counters: dict) -> int:
    """What the scripted counters leave queued, worked out independently."""
    return (
        counters.get("log_enqueued_total", 0)
        - counters.get("log_sent_total", 0)
        - counters.get('log_dropped_total{reason="retry_exhausted"}', 0)
    )


def _time_kills(monkeypatch, child) -> list[float]:
    """Record the monotonic time of every ``child.kill()`` call, then send
    the real kill."""
    kill_times: list[float] = []
    real_kill = child.kill

    def timed_kill() -> None:
        kill_times.append(time.monotonic())
        real_kill()

    monkeypatch.setattr(child, "kill", timed_kill)
    return kill_times


def test_healthy_logs_shutdown_and_needs_no_signal(tmp_path, monkeypatch):
    asyncio.run(_healthy_logs_shutdown_and_needs_no_signal(tmp_path, monkeypatch))


async def _healthy_logs_shutdown_and_needs_no_signal(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "fixture.log"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    _base_url, _token, _boot_id = await process.start()
    pid = process.pid

    result = await process.stop()

    assert result is not None
    assert result.shutdown is not None
    assert result.shutdown.closed is True
    assert result.killed is False
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    events = _read_fixture_log(log_path)
    kinds = [event["kind"] for event in events]
    assert kinds == ["request"]  # /shutdown only; never a signal


def test_default_stop_grace_is_8_seconds():
    assert APP_STOP_GRACE_S == 8.0


def test_stubborn_is_sigkilled_after_the_grace(tmp_path, monkeypatch):
    asyncio.run(_stubborn_is_sigkilled_after_the_grace(tmp_path, monkeypatch))


async def _stubborn_is_sigkilled_after_the_grace(tmp_path: Path, monkeypatch) -> None:
    log_path = tmp_path / "fixture.log"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    small_grace = launcher_module.APP_HEALTH_POLL_S * 6
    monkeypatch.setattr(launcher_module, "APP_STOP_GRACE_S", small_grace)

    process = AppProcess(root=STUBBORN_ROOT, log_path=tmp_path / "app.log")
    _base_url, _token, _boot_id = await process.start()
    pid = process.pid
    kill_times = _time_kills(monkeypatch, process._process)

    started = time.monotonic()
    result = await process.stop()
    elapsed = time.monotonic() - started

    assert result is not None
    assert result.shutdown is not None  # it did answer 200 ...
    assert result.killed is True  # ... but never left on its own
    # The kill waits out the whole grace: the app gets all of it to finish
    # its own clean shutdown before it is cut short.
    assert len(kill_times) == 1
    assert kill_times[0] - started >= small_grace
    assert elapsed >= small_grace
    assert elapsed < small_grace * 10  # bounded by the grace, not by luck
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    events = _read_fixture_log(log_path)
    kinds = [event["kind"] for event in events]
    # Its own /shutdown already answered 200, so the launcher never needs to
    # send it a signal at all: the eventual SIGKILL is a raw OS signal this
    # fixture has no chance to observe or ignore, which is exactly the point.
    assert kinds == ["request"]


def test_wedged_shutdown_is_killed_within_the_grace_not_the_request_timeout(tmp_path, monkeypatch):
    asyncio.run(
        _wedged_shutdown_is_killed_within_the_grace_not_the_request_timeout(tmp_path, monkeypatch)
    )


async def _wedged_shutdown_is_killed_within_the_grace_not_the_request_timeout(
    tmp_path: Path, monkeypatch
) -> None:
    log_path = tmp_path / "fixture.log"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    small_grace = launcher_module.APP_HEALTH_POLL_S * 6
    monkeypatch.setattr(launcher_module, "APP_STOP_GRACE_S", small_grace)
    # Far bigger than the grace: proves the bound really is the grace, not
    # however long a POST is normally allowed to wait.
    monkeypatch.setattr(launcher_module, "LOOPBACK_REQUEST_TIMEOUT_S", small_grace * 613)

    process = AppProcess(root=WEDGED_ROOT, log_path=tmp_path / "app.log")
    _base_url, _token, _boot_id = await process.start()
    pid = process.pid
    kill_times = _time_kills(monkeypatch, process._process)

    started = time.monotonic()
    result = await process.stop()
    elapsed = time.monotonic() - started

    assert result is not None
    assert result.shutdown is None  # its handler never answers
    assert result.killed is True
    assert len(kill_times) == 1
    assert kill_times[0] - started >= small_grace  # the grace runs out first ...
    assert elapsed < small_grace * 10  # ... and the kill follows it, not the POST timeout
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)

    events = _read_fixture_log(log_path)
    assert any(event["kind"] == "request" for event in events)  # the POST did arrive


def test_a_fixture_without_shutdown_falls_back_to_terminate(tmp_path):
    asyncio.run(_a_fixture_without_shutdown_falls_back_to_terminate(tmp_path))


async def _a_fixture_without_shutdown_falls_back_to_terminate(tmp_path: Path) -> None:
    process = AppProcess(root=NO_SHUTDOWN_ROOT, log_path=tmp_path / "app.log")
    _base_url, _token, _boot_id = await process.start()
    pid = process.pid

    result = await process.stop()

    assert result is not None
    assert result.shutdown is None
    assert result.killed is False  # a plain SIGTERM was enough
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_grandchild_stop_completes_within_the_grace(tmp_path, monkeypatch):
    asyncio.run(_grandchild_stop_completes_within_the_grace(tmp_path, monkeypatch))


async def _grandchild_stop_completes_within_the_grace(tmp_path: Path, monkeypatch) -> None:
    # The launcher sends the app's output to a file, so asyncio closes the
    # child's transport itself once it exits and even a plain wait() would
    # return here. The hang a grandchild can cause needs a pipe it holds
    # open; the version-proof wait is proven against exactly that below.
    log_path = tmp_path / "fixture.log"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    small_grace = launcher_module.APP_HEALTH_POLL_S * 6
    monkeypatch.setattr(launcher_module, "APP_STOP_GRACE_S", small_grace)

    process = AppProcess(root=GRANDCHILD_ROOT, log_path=tmp_path / "app.log")
    _base_url, _token, _boot_id = await process.start()
    pid = process.pid

    grandchild_pid = None
    for event in _read_fixture_log(log_path):
        if event["kind"] == "grandchild":
            grandchild_pid = event["pid"]

    try:
        started = time.monotonic()
        result = await asyncio.wait_for(process.stop(), timeout=small_grace * 50)
        elapsed = time.monotonic() - started

        assert result is not None
        assert result.killed is False  # it exits itself, promptly, on /shutdown
        assert elapsed < small_grace * 10
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    finally:
        if grandchild_pid is not None:
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, 9)


def _spy(monkeypatch, owner, name: str, events: list, label: str, child) -> None:
    """Record ``(label, calling function, child.returncode)`` each time
    ``owner.name`` is called, then call the real one."""
    real = getattr(owner, name)

    def spy(*args, **kwargs):
        events.append((label, sys._getframe(1).f_code.co_name, child.returncode))
        return real(*args, **kwargs)

    monkeypatch.setattr(owner, name, spy)


def _launcher_calls(events: list) -> list:
    """The spied calls the launcher made itself: asyncio also closes the
    stdin pipe on its own once it has handled the exit, from its own code."""
    return [
        (label, returncode)
        for label, caller, returncode in events
        if caller in ("stop", "_close_stdin")
    ]


def test_stdin_write_end_is_closed_once_the_exit_is_observed(tmp_path, monkeypatch):
    asyncio.run(_stdin_write_end_is_closed_once_the_exit_is_observed(tmp_path, monkeypatch))


async def _stdin_write_end_is_closed_once_the_exit_is_observed(tmp_path: Path, monkeypatch) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    await process.start()
    child = process._process
    events: list = []
    _spy(monkeypatch, child.stdin, "close", events, "stdin.close", child)
    _spy(monkeypatch, process.control, "aclose", events, "control.aclose", child)

    result = await process.stop()

    assert result is not None
    assert result.killed is False
    launcher_calls = _launcher_calls(events)
    assert [label for label, _ in launcher_calls] == ["stdin.close", "control.aclose"]
    assert launcher_calls[0][1] is not None  # closed once the exit was seen, not before
    assert child.stdin.is_closing()


def test_stdin_write_end_is_closed_when_the_kill_is_sent(tmp_path, monkeypatch):
    asyncio.run(_stdin_write_end_is_closed_when_the_kill_is_sent(tmp_path, monkeypatch))


async def _stdin_write_end_is_closed_when_the_kill_is_sent(tmp_path: Path, monkeypatch) -> None:
    small_grace = launcher_module.APP_HEALTH_POLL_S * 6
    monkeypatch.setattr(launcher_module, "APP_STOP_GRACE_S", small_grace)

    process = AppProcess(root=STUBBORN_ROOT, log_path=tmp_path / "app.log")
    await process.start()
    child = process._process
    events: list = []
    # Timed first, so the spy below still sees stop() as the caller.
    kill_times = _time_kills(monkeypatch, child)
    _spy(monkeypatch, child, "kill", events, "kill", child)
    _spy(monkeypatch, child.stdin, "close", events, "stdin.close", child)

    started = time.monotonic()
    result = await process.stop()

    assert result is not None
    assert result.killed is True
    assert len(kill_times) == 1
    assert kill_times[0] - started >= small_grace  # only once the grace ran out
    launcher_calls = _launcher_calls(events)
    assert [label for label, _ in launcher_calls] == ["kill", "stdin.close"]
    # Closed right as the kill went out, before the child was even reaped.
    assert launcher_calls[1][1] is None
    assert child.stdin.is_closing()


_HOLDS_ITS_OUTPUT_PIPE_OPEN = (
    "import subprocess, sys\n"
    "held = subprocess.Popen([sys.executable, '-c', 'import threading; threading.Event().wait()'])\n"
    "print(held.pid, flush=True)\n"
    "sys.stdin.readline()\n"
)


def test_the_exit_wait_returns_even_while_a_grandchild_holds_a_pipe_open():
    asyncio.run(_the_exit_wait_returns_even_while_a_grandchild_holds_a_pipe_open())


async def _the_exit_wait_returns_even_while_a_grandchild_holds_a_pipe_open() -> None:
    """With piped output, a grandchild that inherits the pipe keeps it open
    after the child exits. On 3.12 a plain ``wait()`` started before the
    exit then never returns; the launcher's own wait must, on every version.
    """
    child = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        _HOLDS_ITS_OUTPUT_PIPE_OPEN,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
    )
    grandchild_pid = int((await child.stdout.readline()).decode("ascii").strip())
    waiter = asyncio.ensure_future(AppProcess._await_returncode(child))
    try:
        child.stdin.write(os.linesep.encode("ascii"))  # the child exits on this
        await child.stdin.drain()

        done, _pending = await asyncio.wait({waiter}, timeout=APP_STOP_GRACE_S)
        assert waiter in done
        assert child.returncode is not None
        os.kill(grandchild_pid, 0)  # ... while the grandchild still holds the pipe
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(grandchild_pid, signal.SIGKILL)
        child.stdin.close()
        await asyncio.wait({asyncio.ensure_future(child.stdout.read())}, timeout=APP_STOP_GRACE_S)
        if not waiter.done():
            waiter.cancel()
        await asyncio.wait({waiter})


def test_stop_returns_even_when_the_kill_never_lands(tmp_path, monkeypatch):
    asyncio.run(_stop_returns_even_when_the_kill_never_lands(tmp_path, monkeypatch))


async def _stop_returns_even_when_the_kill_never_lands(tmp_path: Path, monkeypatch) -> None:
    small_grace = launcher_module.APP_HEALTH_POLL_S * 6
    monkeypatch.setattr(launcher_module, "APP_STOP_GRACE_S", small_grace)

    process = AppProcess(root=STUBBORN_ROOT, log_path=tmp_path / "app.log")
    await process.start()
    child = process._process
    pid = child.pid
    kills: list[int] = []
    # The kill is swallowed, so the child outlives both the grace and the
    # bounded reap: nothing in stop() may wait on it past those bounds.
    monkeypatch.setattr(child, "kill", lambda: kills.append(pid))
    try:
        stopping = asyncio.ensure_future(process.stop())
        done, _pending = await asyncio.wait({stopping}, timeout=small_grace * 50)
        assert stopping in done
        result = stopping.result()
        assert kills == [pid]
        assert result is not None
        assert result.killed is True
        assert result.returncode is None  # it really is still running
        os.kill(pid, 0)
    finally:
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)
        await _wait_until(lambda: child.returncode is not None, timeout_s=small_grace * 50)


def test_retiring_the_exit_monitor_never_swallows_the_callers_cancellation(tmp_path):
    asyncio.run(_retiring_the_exit_monitor_never_swallows_the_callers_cancellation(tmp_path))


async def _retiring_the_exit_monitor_never_swallows_the_callers_cancellation(
    tmp_path: Path,
) -> None:
    app = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    never_exits = SimpleNamespace(returncode=None)
    app._monitor_task = asyncio.ensure_future(app._monitor_exit(never_exits))
    retiring = asyncio.ensure_future(app._retire_monitor())
    await asyncio.sleep(0)  # the monitor is cancelled; retiring now waits on it
    retiring.cancel()
    with pytest.raises(asyncio.CancelledError):
        await retiring
    assert retiring.cancelled()


def test_stop_is_idempotent_and_carries_the_shutdown_metrics(tmp_path, monkeypatch):
    asyncio.run(_stop_is_idempotent_and_carries_the_shutdown_metrics(tmp_path, monkeypatch))


async def _stop_is_idempotent_and_carries_the_shutdown_metrics(tmp_path: Path, monkeypatch) -> None:
    metrics_path = tmp_path / "metrics.json"
    _script_metrics(metrics_path, {"log_enqueued_total": 3})
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    await process.start()

    first = await process.stop()
    assert first is not None
    assert first.shutdown is not None
    assert first.shutdown.metrics == {"log_enqueued_total": 3}

    second = await process.stop()
    assert second is None  # already stopped: a no-op, never raises


# -- Restart, against the real app (root = repo root) ---------------------------
#
# The stub fixtures keep a fixed boot id; a genuine restart must prove a
# *different* boot id, which only the real app -- whose id is a fresh UUID
# per process -- can show. These tests stick to what `start()`, `restart()`
# and `stop()` themselves need, never a full `ControlClient.health()`.


async def _wait_until(predicate, *, timeout_s: float) -> None:
    deadline = time.monotonic() + timeout_s
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("condition never became true")
        await asyncio.sleep(launcher_module.APP_HEALTH_POLL_S)


def _live_child_count(parent_pid: int) -> int:
    """How many direct children ``parent_pid`` (this test process) has right
    now, by asking the OS -- never the launcher's own bookkeeping, since the
    whole point is to catch it lying."""
    result = subprocess.run(
        ["pgrep", "-P", str(parent_pid)],
        capture_output=True,
        text=True,
        check=False,
    )
    return len([line for line in result.stdout.splitlines() if line.strip()])


async def _sample_live_children_while(
    parent_pid: int, samples: list[int], interval_s: float, stop: asyncio.Event
) -> None:
    while not stop.is_set():
        samples.append(await asyncio.to_thread(_live_child_count, parent_pid))
        await asyncio.sleep(interval_s)


def test_restart_against_the_real_app_gives_a_new_boot_id(tmp_path):
    asyncio.run(_restart_against_the_real_app_gives_a_new_boot_id(tmp_path))


async def _restart_against_the_real_app_gives_a_new_boot_id(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        assert process.number == 1
        old_boot = process.boot
        assert old_boot is not None

        new_boot = await process.restart()

        assert new_boot.boot_id != old_boot.boot_id
        assert new_boot.number == 2
        assert process.number == 2
    finally:
        await process.stop()


def test_old_token_401_new_token_200_after_restart(tmp_path):
    asyncio.run(_old_token_401_new_token_200_after_restart(tmp_path))


async def _old_token_401_new_token_200_after_restart(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        base_url, old_token, _boot_id = await process.start()
        new_boot = await process.restart()

        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            stale = await client.get(
                "/__playground/health", headers={"Authorization": f"Bearer {old_token}"}
            )
            fresh = await client.get(
                "/__playground/health", headers={"Authorization": f"Bearer {new_boot.token}"}
            )
        assert stale.status_code == 401
        assert fresh.status_code == 200
    finally:
        await process.stop()


def test_restart_never_leaves_two_processes_at_once(tmp_path):
    asyncio.run(_restart_never_leaves_two_processes_at_once(tmp_path))


async def _restart_never_leaves_two_processes_at_once(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        old_pid = process.pid
        assert old_pid is not None

        await process.restart()
        new_pid = process.pid
        assert new_pid is not None
        assert new_pid != old_pid

        # The old process must already be gone -- restart() only spawns the
        # new one after confirming the old one exited.
        with pytest.raises(ProcessLookupError):
            os.kill(old_pid, 0)
        os.kill(new_pid, 0)  # still alive; raises nothing
    finally:
        await process.stop()


def test_two_concurrent_restarts_run_serially(tmp_path):
    asyncio.run(_two_concurrent_restarts_run_serially(tmp_path))


async def _two_concurrent_restarts_run_serially(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        assert process.number == 1

        first, second = await asyncio.gather(process.restart(), process.restart())

        assert {first.number, second.number} == {2, 3}
        assert first.boot_id != second.boot_id
        assert process.number == 3
    finally:
        await process.stop()


def test_restart_sampling_never_sees_two_app_processes(tmp_path):
    asyncio.run(_restart_sampling_never_sees_two_app_processes(tmp_path))


async def _restart_sampling_never_sees_two_app_processes(tmp_path: Path) -> None:
    """A sampler polls this test process's own live children (never the
    launcher's own bookkeeping) throughout two concurrent ``restart()``
    calls, so a moment where both the old and the new app are alive at once
    would show up as a count of 2 -- not just a check of the state before
    and after.
    """
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        assert process.number == 1

        samples: list[int] = []
        stop_sampling = asyncio.Event()
        sampler = asyncio.ensure_future(
            _sample_live_children_while(
                os.getpid(), samples, launcher_module.APP_HEALTH_POLL_S, stop_sampling
            )
        )
        try:
            first, second = await asyncio.gather(process.restart(), process.restart())
        finally:
            stop_sampling.set()
            await sampler

        assert samples  # the sampler actually ran alongside the restarts
        assert max(samples) <= 1
        assert {first.number, second.number} == {2, 3}
        assert process.number == 3
    finally:
        await process.stop()


def test_restart_while_held_raises(tmp_path, monkeypatch):
    asyncio.run(_restart_while_held_raises(tmp_path, monkeypatch))


async def _restart_while_held_raises(tmp_path: Path, monkeypatch) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        boot = process.boot
        pid = process.pid
        stops: list[dict] = []
        shutdowns: list[dict] = []
        real_stop = process.stop
        real_shutdown = process.control.shutdown

        async def spy_stop(**kwargs):
            stops.append(kwargs)
            return await real_stop(**kwargs)

        async def spy_shutdown(**kwargs):
            shutdowns.append(kwargs)
            return await real_shutdown(**kwargs)

        monkeypatch.setattr(process, "stop", spy_stop)
        monkeypatch.setattr(process.control, "shutdown", spy_shutdown)

        async with process.hold():
            with pytest.raises(RuntimeError) as excinfo:
                await process.restart()

        # A refusal, not a failed restart: it never touched the process.
        assert type(excinfo.value) is RuntimeError
        assert stops == []
        assert shutdowns == []
        assert process.pid == pid
        assert process.alive
        assert process.boot is boot
        assert process.number == 1

        # The hold was the only reason: released, the same call restarts.
        new_boot = await process.restart()
        assert new_boot.boot_id != boot.boot_id
        assert process.number == 2
    finally:
        await process.stop()


def test_a_restart_queued_behind_another_is_refused_once_a_hold_gets_in_first(tmp_path):
    asyncio.run(_a_restart_queued_behind_another_is_refused_once_a_hold_gets_in_first(tmp_path))


async def _a_restart_queued_behind_another_is_refused_once_a_hold_gets_in_first(
    tmp_path: Path,
) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        first = asyncio.ensure_future(process.restart())
        queued = asyncio.ensure_future(process.restart())
        await _wait_until(lambda: process.restarting, timeout_s=launcher_module.APP_START_TIMEOUT_S)

        # This hold waits for the first restart, then is taken before the
        # queued restart gets its turn; that one must refuse.
        async with process.hold() as held_boot:
            first_boot = await first
            assert held_boot.boot_id == first_boot.boot_id
            with pytest.raises(RuntimeError) as excinfo:
                await queued
            assert type(excinfo.value) is RuntimeError
            assert process.boot is first_boot
            assert process.number == 2
    finally:
        await process.stop()


def test_http_first_on_the_real_app(tmp_path):
    asyncio.run(_http_first_on_the_real_app(tmp_path))


async def _http_first_on_the_real_app(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        result = await process.stop()
        assert result is not None
        assert result.shutdown is not None
        assert result.killed is False
        assert result.returncode == 0
    finally:
        await process.stop()


# -- Hold -------------------------------------------------------------------


def test_hold_while_the_app_is_down_raises_appunavailable(tmp_path):
    asyncio.run(_hold_while_the_app_is_down_raises_appunavailable(tmp_path))


async def _hold_while_the_app_is_down_raises_appunavailable(tmp_path: Path) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    with pytest.raises(AppUnavailable):
        async with process.hold():
            pass  # pragma: no cover -- never reached


def test_sigkill_during_a_hold_sets_invalidated_and_intact_is_false(tmp_path):
    asyncio.run(_sigkill_during_a_hold_sets_invalidated_and_intact_is_false(tmp_path))


async def _sigkill_during_a_hold_sets_invalidated_and_intact_is_false(tmp_path: Path) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        async with process.hold() as h:
            assert await h.intact() is True
            os.kill(process.pid, 9)
            await _wait_until(
                lambda: h.invalidated.is_set(), timeout_s=launcher_module.APP_HEALTH_POLL_S * 100
            )
            assert await h.intact() is False
    finally:
        await process.stop()


def test_hold_requested_during_a_restart_waits_then_gets_the_new_boot(tmp_path):
    asyncio.run(_hold_requested_during_a_restart_waits_then_gets_the_new_boot(tmp_path))


async def _hold_requested_during_a_restart_waits_then_gets_the_new_boot(tmp_path: Path) -> None:
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        old_boot_id = process.boot.boot_id

        restart_task = asyncio.ensure_future(process.restart())
        await _wait_until(lambda: process.restarting, timeout_s=launcher_module.APP_START_TIMEOUT_S)

        async with process.hold() as h:
            # hold() only resumes once the restart is fully done.
            assert process.restarting is False
            assert h.boot_id != old_boot_id
            assert h.boot_id == process.boot.boot_id

        await restart_task
    finally:
        await process.stop()


# -- Unexpected exits ---------------------------------------------------------------


def test_wait_exit_is_none_before_anything_was_started(tmp_path):
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    assert asyncio.run(process.wait_exit()) is None
    assert process.exited is False


@pytest.mark.parametrize("root", [HEALTHY_ROOT, GRANDCHILD_ROOT], ids=["healthy", "grandchild"])
def test_a_sigkill_wakes_wait_exit_promptly(tmp_path, monkeypatch, root):
    asyncio.run(_a_sigkill_wakes_wait_exit_promptly(tmp_path, monkeypatch, root))


async def _a_sigkill_wakes_wait_exit_promptly(tmp_path: Path, monkeypatch, root: Path) -> None:
    log_path = tmp_path / "fixture.log"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    poll = launcher_module.APP_HEALTH_POLL_S

    process = AppProcess(root=root, log_path=tmp_path / "app.log")
    await process.start()
    grandchild_pids = [
        event["pid"] for event in _read_fixture_log(log_path) if event["kind"] == "grandchild"
    ]
    try:
        waiter = asyncio.ensure_future(process.wait_exit())
        await asyncio.sleep(poll * 3)
        assert not waiter.done()
        assert process.exited is False

        os.kill(process.pid, signal.SIGKILL)
        done, _pending = await asyncio.wait({waiter}, timeout=poll * 61)

        assert waiter in done
        assert waiter.result() == -signal.SIGKILL
        assert process.exited is True
        assert await process.stop() is None  # already gone: nothing to stop
    finally:
        for grandchild_pid in grandchild_pids:
            with contextlib.suppress(ProcessLookupError):
                os.kill(grandchild_pid, signal.SIGKILL)
        await process.stop()


def test_a_deliberate_stop_never_wakes_wait_exit(tmp_path):
    asyncio.run(_a_deliberate_stop_never_wakes_wait_exit(tmp_path))


async def _a_deliberate_stop_never_wakes_wait_exit(tmp_path: Path) -> None:
    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    await process.start()
    waiter = asyncio.ensure_future(process.wait_exit())
    try:
        result = await process.stop()
        assert result is not None
        assert process.exited is True
        await asyncio.sleep(launcher_module.APP_HEALTH_POLL_S * 6)
        assert not waiter.done()
    finally:
        waiter.cancel()
        await asyncio.wait({waiter})


def test_wait_exit_sleeps_through_a_restart_and_wakes_for_the_new_boot(tmp_path):
    asyncio.run(_wait_exit_sleeps_through_a_restart_and_wakes_for_the_new_boot(tmp_path))


async def _wait_exit_sleeps_through_a_restart_and_wakes_for_the_new_boot(tmp_path: Path) -> None:
    poll = launcher_module.APP_HEALTH_POLL_S
    process = AppProcess(root=REPO_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        waiter = asyncio.ensure_future(process.wait_exit())

        await process.restart()
        await asyncio.sleep(poll * 6)
        assert not waiter.done()

        os.kill(process.pid, signal.SIGKILL)
        done, _pending = await asyncio.wait({waiter}, timeout=poll * 61)
        assert waiter in done
        assert waiter.result() == -signal.SIGKILL
        assert process.exited is True
    finally:
        await process.stop()


# -- Drain, wired through stop(drain=True) against the scripted fixture ---------


def test_no_shutdown_arrives_until_the_scripted_backlog_clears(tmp_path, monkeypatch):
    asyncio.run(_no_shutdown_arrives_until_the_scripted_backlog_clears(tmp_path, monkeypatch))


async def _no_shutdown_arrives_until_the_scripted_backlog_clears(
    tmp_path: Path, monkeypatch
) -> None:
    log_path = tmp_path / "fixture.log"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))

    def write_metrics(enqueued: int, sent: int, retry_exhausted: int) -> None:
        _script_metrics(
            metrics_path,
            {
                "log_enqueued_total": enqueued,
                "log_sent_total": sent,
                'log_dropped_total{reason="retry_exhausted"}': retry_exhausted,
            },
        )

    # Still 2 queued: enqueued(5) - sent(2) - retry_exhausted(1) = 2.
    write_metrics(enqueued=5, sent=2, retry_exhausted=1)
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()

        async def clear_the_backlog_soon() -> None:
            await asyncio.sleep(launcher_module.APP_HEALTH_POLL_S * 8)
            write_metrics(enqueued=5, sent=3, retry_exhausted=2)  # queued == 0 now

        clearer = asyncio.ensure_future(clear_the_backlog_soon())
        result = await process.stop(drain=True)
        await clearer

        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "drained"

        events = _read_fixture_log(log_path)
        kinds = [event.get("path", event["kind"]) for event in events]
        shutdown_index = kinds.index("/__playground/shutdown")
        # Only metrics polls before the shutdown, and nothing after it.
        assert kinds[:shutdown_index] == ["/__playground/metrics"] * shutdown_index
        assert kinds[shutdown_index + 1 :] == []
        served = [event["served"] for event in events[:shutdown_index]]
        assert all(isinstance(counters, dict) for counters in served)
        # Every poll saw the backlog until the one that found it cleared, and
        # the shutdown came straight after that one, never before it.
        queued_seen = [_queued_in(counters) for counters in served]
        assert len(queued_seen) >= 2
        assert queued_seen == [2] * (len(queued_seen) - 1) + [0]
    finally:
        await process.stop()


def test_rescripted_metrics_are_never_seen_half_written(tmp_path):
    metrics_path = tmp_path / "metrics.json"
    _script_metrics(metrics_path, {"log_enqueued_total": 0})
    stop = threading.Event()
    reads: list[bool] = []  # one entry per read: did it hold a JSON object?

    def read_constantly() -> None:
        while not stop.is_set():
            text = metrics_path.read_text(encoding="utf-8")
            try:
                reads.append(isinstance(json.loads(text), dict))
            except ValueError:
                reads.append(False)

    reader = threading.Thread(target=read_constantly)
    reader.start()
    rewrites = 0
    try:
        while rewrites < 211 or len(reads) < 211:
            rewrites += 1
            _script_metrics(
                metrics_path, {"log_enqueued_total": rewrites, "log_sent_total": rewrites}
            )
    finally:
        stop.set()
        reader.join()

    assert reads.count(False) == 0


def test_an_unparseable_metrics_answer_is_never_read_as_an_empty_queue(tmp_path, monkeypatch):
    asyncio.run(
        _an_unparseable_metrics_answer_is_never_read_as_an_empty_queue(tmp_path, monkeypatch)
    )


async def _an_unparseable_metrics_answer_is_never_read_as_an_empty_queue(
    tmp_path: Path, monkeypatch
) -> None:
    log_path = tmp_path / "fixture.log"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))
    # What a reader would find part-way through an in-place rewrite.
    whole = json.dumps({"log_enqueued_total": 5, "log_sent_total": 1})
    metrics_path.write_text(whole[: len(whole) // 2], encoding="utf-8")
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        result = await process.stop(drain=True)

        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "unreachable"
        assert result.shutdown is not None  # the app itself still stopped cleanly

        polls = [
            event
            for event in _read_fixture_log(log_path)
            if event.get("path") == "/__playground/metrics"
        ]
        assert len(polls) == drain_module.DRAIN_FAILURES_MAX
        assert all(poll["served"] is None for poll in polls)
    finally:
        await process.stop()


def test_drain_against_the_healthy_fixture_can_stall(tmp_path, monkeypatch):
    asyncio.run(_drain_against_the_healthy_fixture_can_stall(tmp_path, monkeypatch))


async def _drain_against_the_healthy_fixture_can_stall(tmp_path: Path, monkeypatch) -> None:
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))
    # Frozen counters: queued stays at 4 forever, so progress never moves.
    _script_metrics(metrics_path, {"log_enqueued_total": 5, "log_sent_total": 1})
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S)
    monkeypatch.setattr(drain_module, "DRAIN_STALL_S", launcher_module.APP_HEALTH_POLL_S * 6)
    monkeypatch.setattr(drain_module, "DRAIN_MAX_S", launcher_module.APP_HEALTH_POLL_S * 4817)
    monkeypatch.setattr(
        drain_module, "DRAIN_REQUEST_TIMEOUT_S", launcher_module.APP_HEALTH_POLL_S * 100
    )

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        result = await process.stop(drain=True)
        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "stalled"
    finally:
        await process.stop()


def test_drain_against_the_healthy_fixture_hits_the_cap_while_still_busy(tmp_path, monkeypatch):
    asyncio.run(
        _drain_against_the_healthy_fixture_hits_the_cap_while_still_busy(tmp_path, monkeypatch)
    )


async def _drain_against_the_healthy_fixture_hits_the_cap_while_still_busy(
    tmp_path: Path, monkeypatch
) -> None:
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))

    def write_metrics(n: int) -> None:
        _script_metrics(
            metrics_path,
            {
                "log_enqueued_total": 100 + n,
                "log_sent_total": 1,
                'log_http_error_total{status="422"}': n,
            },
        )

    write_metrics(0)
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S)
    monkeypatch.setattr(drain_module, "DRAIN_STALL_S", launcher_module.APP_HEALTH_POLL_S * 4817)
    monkeypatch.setattr(drain_module, "DRAIN_MAX_S", launcher_module.APP_HEALTH_POLL_S * 23)
    monkeypatch.setattr(
        drain_module, "DRAIN_REQUEST_TIMEOUT_S", launcher_module.APP_HEALTH_POLL_S * 100
    )

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()

        keep_busy = asyncio.Event()

        async def keep_moving() -> None:
            n = 0
            while not keep_busy.is_set():
                n += 1
                write_metrics(n)
                await asyncio.sleep(launcher_module.APP_HEALTH_POLL_S)

        mover = asyncio.ensure_future(keep_moving())
        result = await process.stop(drain=True)
        keep_busy.set()
        await mover

        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "capped"
    finally:
        await process.stop()


def test_drain_against_the_healthy_fixture_is_skipped_when_cancelled(tmp_path, monkeypatch):
    asyncio.run(_drain_against_the_healthy_fixture_is_skipped_when_cancelled(tmp_path, monkeypatch))


async def _drain_against_the_healthy_fixture_is_skipped_when_cancelled(
    tmp_path: Path, monkeypatch
) -> None:
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))
    _script_metrics(metrics_path, {"log_enqueued_total": 5})
    # Coarse enough that the cancel below lands mid-poll or mid-sleep,
    # whichever the scheduler happens to hit -- either way this must be a
    # prompt `skipped`, never a wait for the poll itself to finish.
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S * 4)

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        cancel = asyncio.Event()

        async def cancel_soon() -> None:
            await asyncio.sleep(launcher_module.APP_HEALTH_POLL_S)
            cancel.set()

        canceller = asyncio.ensure_future(cancel_soon())
        result = await process.stop(drain=True, cancel=cancel)
        await canceller

        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "skipped"
    finally:
        await process.stop()


def test_drain_against_the_healthy_fixture_reports_on_wait_throttled(tmp_path, monkeypatch):
    asyncio.run(_drain_against_the_healthy_fixture_reports_on_wait_throttled(tmp_path, monkeypatch))


async def _drain_against_the_healthy_fixture_reports_on_wait_throttled(
    tmp_path: Path, monkeypatch
) -> None:
    log_path = tmp_path / "fixture.log"
    metrics_path = tmp_path / "metrics.json"
    monkeypatch.setenv("PLAYGROUND_FIXTURE_LOG", str(log_path))
    monkeypatch.setenv("PLAYGROUND_FIXTURE_METRICS_FILE", str(metrics_path))
    _script_metrics(metrics_path, {"log_enqueued_total": 5})
    report_every = launcher_module.APP_HEALTH_POLL_S * 8
    monkeypatch.setattr(drain_module, "DRAIN_POLL_S", launcher_module.APP_HEALTH_POLL_S)
    monkeypatch.setattr(drain_module, "DRAIN_STALL_S", launcher_module.APP_HEALTH_POLL_S * 43)
    monkeypatch.setattr(drain_module, "DRAIN_REPORT_EVERY_S", report_every)
    monkeypatch.setattr(drain_module, "DRAIN_MAX_S", launcher_module.APP_HEALTH_POLL_S * 31)
    clock = RecordingClock()
    monkeypatch.setattr(drain_module, "time", clock)

    def metrics_polls() -> int:
        return sum(
            1
            for event in _read_fixture_log(log_path)
            if event.get("path") == "/__playground/metrics"
        )

    # (count reported, polls the app had seen by then, the drain's own clock)
    reports: list[tuple[int, int, float]] = []

    def on_wait(remaining: int) -> None:
        reports.append((remaining, metrics_polls(), clock.last))

    process = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    try:
        await process.start()
        result = await process.stop(drain=True, on_drain=on_wait)

        assert result is not None
        assert result.drain is not None
        assert result.drain.outcome == "capped"
        assert [count for count, _polls, _at in reports] == [5] * len(reports)
        assert reports[0][1] == 1  # straight after the first poll
        assert len(reports) >= 2
        gaps = [later[2] - earlier[2] for earlier, later in itertools.pairwise(reports)]
        assert all(gap >= report_every for gap in gaps)
        assert metrics_polls() > len(reports)
    finally:
        await process.stop()
