"""Starts and stops the sample app as a subprocess on a free loopback port.

The console always dials the app on ``127.0.0.1`` with a fresh per-boot
token, waits for its health check to answer, and stops it again -- gracefully
first over HTTP, then with a signal if it doesn't listen. Restarts reuse the
same port, are serialised, and a run may ``hold()`` a boot open so no restart
starts underneath it.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import os
import secrets
import socket
import sys
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

from playground.machine.events import BootFailure
from playground.supervisor.boot_log import bind_failed, boot_failure, failure_location
from playground.supervisor.control_client import ControlClient, ShutdownReply
from playground.supervisor.drain import DrainResult
from playground.supervisor.drain import drain as _drain_app
from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    APP_START_ATTEMPTS,
    APP_START_TIMEOUT_S,
    APP_STOP_GRACE_S,
    LOOPBACK_REQUEST_TIMEOUT_S,
)

HEALTH_PATH = "/__playground/health"
CONTROL_TOKEN_ENV_VAR = "PLAYGROUND_CONTROL_TOKEN"
LIFELINE_ENV_VAR = "PLAYGROUND_LIFELINE"

OnDrainWait = Callable[[int], "Awaitable[None] | None"]
BeforeSpawn = Callable[[], "Awaitable[None] | None"]


class AppStartError(RuntimeError):
    """The sample app didn't answer its health check in time, or exited early.

    ``location`` is where its start-up failed (``app/<file>.py:<line>``) and
    ``failure`` what its output said about why, when either is known.
    """

    def __init__(
        self,
        message: str,
        *,
        location: str | None = None,
        failure: BootFailure | None = None,
    ) -> None:
        super().__init__(message)
        self.location = location
        self.failure = failure


class AppUnavailable(RuntimeError):
    """A ``hold()`` was requested, or was open, while the app wasn't alive."""


class _BindFailed(Exception):
    """Internal only: this attempt's port was already taken."""


class _EarlyExit(Exception):
    """Internal only: the child exited before it answered its health check."""


class _HealthTimeout(Exception):
    """Internal only: the child never answered its health check in time."""


@dataclass(frozen=True)
class Boot:
    """One successful launch of the sample app."""

    number: int
    boot_id: str
    base_url: str
    token: str = field(repr=False)
    pid: int
    port: int


@dataclass(frozen=True)
class StopResult:
    """What happened when the app was told to stop."""

    drain: DrainResult | None
    shutdown: ShutdownReply | None
    killed: bool
    returncode: int | None


class Hold:
    """Proof that a boot was alive when acquired, and a way to check it still is.

    The two-argument constructor is the frozen shape: ``_owner`` is a
    keyword-only, private back-reference the launcher supplies so
    ``intact()`` can ask the owning :class:`AppProcess`; it's never part of
    the frozen signature and a caller who builds a bare
    ``Hold(boot_id, invalidated)`` still gets a valid (if unconfirmable)
    object rather than a ``TypeError``.
    """

    def __init__(
        self,
        boot_id: str,
        invalidated: asyncio.Event,
        *,
        _owner: AppProcess | None = None,
    ) -> None:
        self.boot_id = boot_id
        self.invalidated = invalidated
        self._owner = _owner

    async def intact(self) -> bool:
        """True iff the process is alive, not invalidated, and still this boot."""
        if self.invalidated.is_set():
            return False
        if self._owner is None or not self._owner.alive:
            return False
        control = self._owner.control
        if control is None:
            return False
        try:
            health = await control.health()
        except Exception:  # noqa: BLE001 - any failure to confirm means not intact
            return False
        return health.boot_id == self.boot_id


def app_command(port: int) -> list[str]:
    """The exact command line that launches the sample app on ``port``."""
    return [
        sys.executable,
        "-m",
        "uvicorn",
        "app.main:app",
        "--host",
        "127.0.0.1",
        "--port",
        str(port),
        "--proxy-headers",
        "--forwarded-allow-ips",
        "127.0.0.1",
        "--no-access-log",
    ]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def _probe_health(
    client: httpx.AsyncClient,
    headers: dict[str, str],
    process: asyncio.subprocess.Process,
    *,
    timeout_s: float,
) -> str | None:
    """One health request: the boot id, or None if it didn't answer 200.

    Gives up as soon as the child exits: when the port turns out to be held
    by something else, that listener may accept the request and never
    answer, and the child's own exit is what says to move on.
    """
    request = asyncio.ensure_future(client.get(HEALTH_PATH, headers=headers, timeout=timeout_s))
    try:
        while not request.done():
            if process.returncode is not None:
                raise _EarlyExit
            await asyncio.wait({request}, timeout=APP_HEALTH_POLL_S)
    finally:
        if not request.done():
            request.cancel()
            await asyncio.wait({request})

    error = request.exception()
    if isinstance(error, httpx.HTTPError):
        return None
    if error is not None:
        raise error
    response = request.result()
    if response.status_code != 200:
        return None
    try:
        body = response.json()
    except ValueError:
        return None
    if not isinstance(body, dict):
        return None
    return str(body.get("boot_id", ""))


async def _maybe_await(callback: Callable[[], Any] | None) -> None:
    if callback is None:
        return
    result = callback()
    if inspect.isawaitable(result):
        await result


class AppProcess:
    """Owns at most one sample-app subprocess at a time: start it, hold it
    open across a run, restart it, stop it."""

    def __init__(self, root: Path, log_path: Path) -> None:
        self._root = root
        self._log_path = log_path
        self._process: asyncio.subprocess.Process | None = None
        self._port: int | None = None
        self._monitor_task: asyncio.Task[None] | None = None
        self._expected_exit = False

        self.number = 0
        self.control: ControlClient | None = None
        self.restarting = False
        self._boot: Boot | None = None
        self._hold_count = 0
        self._open_holds: set[Hold] = set()
        self._restart_lock = asyncio.Lock()
        self._restart_idle = asyncio.Event()
        self._restart_idle.set()
        # Set when the current boot exits outside stop() and restart();
        # cleared again when a new boot comes up.
        self._unexpected_exit = asyncio.Event()
        self._unexpected_returncode: int | None = None

    @property
    def pid(self) -> int | None:
        """The subprocess id once started, or ``None`` before that."""
        return self._process.pid if self._process is not None else None

    @property
    def exited(self) -> bool:
        """True once the current subprocess has exited, for whatever reason."""
        return self._process is not None and self._process.returncode is not None

    @property
    def alive(self) -> bool:
        """True while the current subprocess is running."""
        return self._process is not None and self._process.returncode is None

    @property
    def boot(self) -> Boot | None:
        """The most recent successful :class:`Boot`, or None before one exists."""
        return self._boot

    @property
    def held(self) -> bool:
        """True while at least one ``hold()`` is open."""
        return self._hold_count > 0

    # -- launch -----------------------------------------------------------

    async def start(self, *, before_spawn: BeforeSpawn | None = None) -> tuple[str, str, str]:
        """Launch the app; return ``(base_url, control_token, boot_id)``.

        Reuses the port from a previous boot unless that port turned out to
        be taken, in which case it moves to a fresh one, up to
        ``APP_START_ATTEMPTS`` spawns in total.
        """
        if self.control is not None:
            # An unexpected exit (a crash, or a `kill -9` from outside)
            # leaves the previous boot's control client set with nothing
            # left to close it; every start() closes it here first, so a
            # crash-and-respawn loop never leaks a client and its pool.
            stale_control = self.control
            self.control = None
            with contextlib.suppress(Exception):
                await stale_control.aclose()

        await self._retire_monitor()

        offset = self._log_offset()
        await _maybe_await(before_spawn)

        last_error: AppStartError | None = None
        for _attempt in range(APP_START_ATTEMPTS):
            port = self._port if self._port is not None else _free_port()
            try:
                base_url, token, boot_id = await self._spawn_and_wait(port, offset)
            except _BindFailed:
                self._port = None
                offset = self._log_offset()
                continue
            except AppStartError as error:
                last_error = error
                break

            self._port = port
            self.number += 1
            assert self._process is not None
            self._boot = Boot(
                number=self.number,
                boot_id=boot_id,
                base_url=base_url,
                token=token,
                pid=self._process.pid,
                port=port,
            )
            self.control = ControlClient(base_url, token)
            self._expected_exit = False
            self._unexpected_exit.clear()
            self._unexpected_returncode = None
            self._monitor_task = asyncio.ensure_future(self._monitor_exit(self._process))
            return base_url, token, boot_id

        if last_error is not None:
            raise last_error
        raise AppStartError(f"could not bind a free port in {APP_START_ATTEMPTS} attempts")

    async def _spawn_and_wait(self, port: int, offset: int) -> tuple[str, str, str]:
        token = secrets.token_urlsafe()
        base_url = f"http://127.0.0.1:{port}"

        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        env = os.environ | {
            CONTROL_TOKEN_ENV_VAR: token,
            LIFELINE_ENV_VAR: "1",
        }
        posix_only: dict[str, object] = {"start_new_session": True} if os.name == "posix" else {}

        # A one-time, local file open at boot; not worth a thread hop.
        with open(self._log_path, "ab") as log_file:  # noqa: ASYNC230
            process = await asyncio.create_subprocess_exec(
                *app_command(port),
                cwd=self._root,
                env=env,
                stdin=asyncio.subprocess.PIPE,
                stdout=log_file,
                stderr=log_file,
                **posix_only,
            )
        self._process = process

        try:
            boot_id = await self._wait_healthy(base_url, token, process)
        except _EarlyExit:
            self._close_stdin()
            slice_text = self._read_slice(offset)
            if bind_failed(slice_text):
                raise _BindFailed from None
            raise AppStartError(
                "sample app exited before it answered its health check",
                location=failure_location(slice_text, self._root),
                failure=boot_failure(slice_text, self._root),
            ) from None
        except _HealthTimeout:
            with contextlib.suppress(ProcessLookupError):
                process.kill()
            self._close_stdin()
            await self._await_returncode(process, deadline=time.monotonic() + APP_STOP_GRACE_S)
            slice_text = self._read_slice(offset)
            raise AppStartError(
                "sample app did not answer its health check in time",
                location=failure_location(slice_text, self._root),
                failure=boot_failure(slice_text, self._root),
            ) from None

        return base_url, token, boot_id

    async def _wait_healthy(
        self, base_url: str, token: str, process: asyncio.subprocess.Process
    ) -> str:
        deadline = time.monotonic() + APP_START_TIMEOUT_S
        headers = {"Authorization": f"Bearer {token}"}

        async with httpx.AsyncClient(base_url=base_url, trust_env=False) as client:
            while True:
                if process.returncode is not None:
                    raise _EarlyExit
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise _HealthTimeout

                boot_id = await _probe_health(
                    client,
                    headers,
                    process,
                    timeout_s=min(LOOPBACK_REQUEST_TIMEOUT_S, remaining),
                )
                if boot_id is not None:
                    return boot_id

                if time.monotonic() >= deadline:
                    raise _HealthTimeout

                await asyncio.sleep(APP_HEALTH_POLL_S)

    # -- stop / restart -----------------------------------------------------

    async def stop(
        self,
        *,
        drain: bool = False,
        cancel: asyncio.Event | None = None,
        on_drain: OnDrainWait | None = None,
    ) -> StopResult | None:
        """Stop the app: drain (optionally), HTTP shutdown, then a signal if
        it doesn't listen. Idempotent; a no-op when nothing is running."""
        process = self._process
        if process is None or process.returncode is not None:
            return None

        self._expected_exit = True
        try:
            drain_result: DrainResult | None = None
            if drain and self.alive and self.control is not None:
                drain_result = await _drain_app(self.control, cancel=cancel, on_wait=on_drain)

            deadline = time.monotonic() + APP_STOP_GRACE_S
            shutdown_reply = None
            remaining = deadline - time.monotonic()
            if remaining > 0 and self.control is not None:
                timeout = min(LOOPBACK_REQUEST_TIMEOUT_S, remaining)
                try:
                    shutdown_reply = await self.control.shutdown(timeout_s=timeout)
                except Exception:  # noqa: BLE001 - any POST failure falls back to a signal
                    shutdown_reply = None
            if shutdown_reply is None:
                with contextlib.suppress(ProcessLookupError):
                    process.terminate()

            killed = False
            while True:
                if process.returncode is not None:
                    self._close_stdin()
                    break
                if time.monotonic() >= deadline:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    self._close_stdin()
                    killed = True
                    break
                await asyncio.sleep(APP_HEALTH_POLL_S)

            # After a kill, reaping is bounded too: a grandchild that
            # inherited fd 0 can leave a zombie's pipes lingering, but
            # `returncode` itself is set as soon as the kernel reaps this
            # child, so this is a real bound, not a guess.
            reap_deadline = time.monotonic() + APP_STOP_GRACE_S if killed else None
            await self._await_returncode(process, deadline=reap_deadline)

            # This exit is expected, so the monitor has nothing to report.
            await self._retire_monitor()

            if self.control is not None:
                await self.control.aclose()
                self.control = None

            return StopResult(
                drain=drain_result,
                shutdown=shutdown_reply,
                killed=killed,
                returncode=process.returncode,
            )
        finally:
            self._expected_exit = False

    async def restart(
        self,
        *,
        cancel: asyncio.Event | None = None,
        on_drain: OnDrainWait | None = None,
        before_spawn: BeforeSpawn | None = None,
    ) -> Boot:
        """Stop the current boot and start a new one on the same port."""
        if self.held:
            raise RuntimeError("cannot restart while a hold is open")

        async with self._restart_lock:
            if self.held:
                raise RuntimeError("cannot restart while a hold is open")

            self.restarting = True
            self._restart_idle.clear()
            try:
                old_boot_id = self._boot.boot_id if self._boot is not None else None
                await self.stop(drain=True, cancel=cancel, on_drain=on_drain)
                _base_url, _token, boot_id = await self.start(before_spawn=before_spawn)
                if boot_id == old_boot_id:
                    raise AppStartError("restart produced the same boot id")
                assert self._boot is not None
                return self._boot
            finally:
                self.restarting = False
                self._restart_idle.set()

    # -- hold ---------------------------------------------------------------

    @contextlib.asynccontextmanager
    async def hold(self) -> AsyncIterator[Hold]:
        """An open hold blocks no restart from starting; a restart blocks no
        hold from waiting for it to finish first."""
        while self.restarting:
            await self._restart_idle.wait()
        if not self.alive or self._boot is None:
            raise AppUnavailable("the sample app is not running")

        current = self._boot
        the_hold = Hold(current.boot_id, asyncio.Event(), _owner=self)
        self._hold_count += 1
        self._open_holds.add(the_hold)
        try:
            yield the_hold
        finally:
            self._open_holds.discard(the_hold)
            self._hold_count -= 1

    # -- waiting on the child -------------------------------------------------

    async def wait_exit(self) -> int | None:
        """Wait for the app to exit outside ``stop()`` and ``restart()``.

        Returns that exit's code, at once if the current boot has already
        ended that way, or None if nothing was ever started. A deliberate
        stop or restart never wakes it: across a restart it keeps waiting,
        now on the new boot.
        """
        if self._process is None:
            return None
        await self._unexpected_exit.wait()
        return self._unexpected_returncode

    async def _monitor_exit(self, process: asyncio.subprocess.Process) -> None:
        await self._await_returncode(process)
        if process is not self._process or self._expected_exit:
            return
        self._report_unexpected_exit(process)

    def _report_unexpected_exit(self, process: asyncio.subprocess.Process) -> None:
        for one_hold in list(self._open_holds):
            one_hold.invalidated.set()
        self._unexpected_returncode = process.returncode
        self._unexpected_exit.set()

    async def _retire_monitor(self) -> None:
        """Stop the current boot's exit monitor.

        The monitor's own wait for the child has no bound, so it is
        cancelled rather than awaited. If the child already exited outside
        stop() and the monitor hadn't noticed yet, the exit is reported here
        instead, so it is never lost. ``asyncio.wait`` never raises the
        monitor's own cancellation, so a cancellation of the caller still
        reaches the caller.
        """
        monitor = self._monitor_task
        self._monitor_task = None
        if monitor is None:
            return
        if monitor.done():
            if not monitor.cancelled():
                monitor.exception()  # retrieve any error so it is never logged as unseen
            return
        monitor.cancel()
        await asyncio.wait({monitor})
        process = self._process
        if process is not None and process.returncode is not None and not self._expected_exit:
            self._report_unexpected_exit(process)

    @staticmethod
    async def _await_returncode(
        process: asyncio.subprocess.Process, *, deadline: float | None = None
    ) -> None:
        # Never a raw `await process.wait()`: on 3.12 that only resolves once
        # every pipe transport has disconnected, which a grandchild that
        # inherited fd 0 can delay indefinitely even after `kill()`.
        # `returncode` itself is set as soon as the child is reaped, before
        # its pipes close, on both 3.10 and 3.12.
        #
        # ``deadline`` is only ever given once this launcher has tried to end
        # the child. The exit monitor, which follows a live child's own
        # lifetime, stays unbounded on purpose; nothing ever awaits it
        # directly -- stop() and start() cancel it instead.
        while process.returncode is None:
            if deadline is not None and time.monotonic() >= deadline:
                return
            await asyncio.sleep(APP_HEALTH_POLL_S)

    def _close_stdin(self) -> None:
        # Always closed here: asyncio closes it by itself once it sees the
        # exit only while no output is piped, and a second close is a no-op.
        process = self._process
        if process is None or process.stdin is None:
            return
        with contextlib.suppress(Exception):
            process.stdin.close()

    # -- this boot's log slice -----------------------------------------------

    def _log_offset(self) -> int:
        try:
            return self._log_path.stat().st_size
        except OSError:
            return 0

    def _read_slice(self, offset: int) -> str:
        try:
            with open(self._log_path, "rb") as handle:
                handle.seek(offset)
                data = handle.read()
        except OSError:
            data = b""
        return data.decode("utf-8", errors="replace")


__all__ = [
    "AppProcess",
    "AppStartError",
    "AppUnavailable",
    "Boot",
    "Hold",
    "StopResult",
    "app_command",
]
