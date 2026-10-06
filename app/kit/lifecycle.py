"""How the sample app asks itself to exit, and how it knows the console
that started it is gone.

`request_exit()` is the one place that turns "we're done" into either a
clean uvicorn shutdown or, failing that, a SIGINT to this same process; it
never sends more than one signal. `exit_hook_ok()` is the boot self-test's
read of whether that path is actually wired to a real server. `start_lifeline()`
watches this boot's own stdin: as long as the console holds the write end
open, the read blocks; once it closes (the console exited, however it did)
or the descriptor errors, the app asks itself to exit, and forces the issue
if that doesn't work quickly enough.
"""

from __future__ import annotations

import os
import signal
import stat
import threading
import time
from typing import Any

from app.kit.local_constants import LIFELINE_EXIT_GRACE_S

LIFELINE_ENV = "PLAYGROUND_LIFELINE"

_exit_lock = threading.Lock()
_exit_requested = False


def _uvicorn_server() -> Any | None:
    """The running uvicorn `Server`, found through its own SIGTERM handler.

    None where nothing installed one this way: under TestClient, a plain
    script, or before uvicorn has set its handlers up.
    """
    handler = signal.getsignal(signal.SIGTERM)
    server = getattr(handler, "__self__", None)
    if server is not None and hasattr(server, "should_exit"):
        return server
    return None


def request_exit() -> None:
    """Ask this process to exit. Idempotent, and never sends more than one signal.

    Sets the uvicorn server's `should_exit` when one can be found. Otherwise,
    only when this boot has the stdin lifeline enabled, raises exactly one
    `SIGINT` on this process. With neither in place (for example under
    TestClient) it does nothing.
    """
    global _exit_requested
    with _exit_lock:
        if _exit_requested:
            return
        _exit_requested = True
    server = _uvicorn_server()
    if server is not None:
        server.should_exit = True
        return
    if os.environ.get(LIFELINE_ENV) == "1":
        signal.raise_signal(signal.SIGINT)


def exit_hook_ok() -> str:
    """The boot self-test's `exit_hook` outcome: "pass" under a real uvicorn
    server, "skip" everywhere else (a skip is not a failure)."""
    return "pass" if _uvicorn_server() is not None else "skip"


def _stdin_is_pipe_or_socket(fd: int = 0) -> bool:
    """Whether `fd` is a FIFO or a socket (asyncio uses a socketpair for a
    `PIPE` stdin on some platforms). False for a regular file, `/dev/null`,
    a closed descriptor, or a terminal."""
    try:
        mode = os.fstat(fd).st_mode
    except OSError:
        return False
    return stat.S_ISFIFO(mode) or stat.S_ISSOCK(mode)


def _lifeline_wait(fd: int) -> None:
    while True:
        try:
            chunk = os.read(fd, 1)
        except OSError:
            break
        if not chunk:
            break
        # Any byte is ignored; the console never writes to this pipe, so
        # this only ever loops back to wait for the close that means EOF.
    request_exit()
    time.sleep(LIFELINE_EXIT_GRACE_S)
    os._exit(1)  # a graceful exit didn't happen in time


def start_lifeline(fd: int = 0) -> threading.Thread | None:
    """Start the stdin lifeline thread for this boot, when it applies.

    Runs only when `PLAYGROUND_LIFELINE=1` and `fd` is a pipe or a socket.
    Off for TestClient, a subprocess test that leaves the flag unset, and a
    regular-file or `/dev/null` stdin. Returns the daemon thread it started,
    or None when the lifeline doesn't apply here.
    """
    if os.environ.get(LIFELINE_ENV) != "1":
        return None
    if not _stdin_is_pipe_or_socket(fd):
        return None
    thread = threading.Thread(target=_lifeline_wait, args=(fd,), daemon=True)
    thread.start()
    return thread


def _reset() -> None:
    """Forget the once-guard on `request_exit()` (tests only)."""
    global _exit_requested
    with _exit_lock:
        _exit_requested = False


__all__ = ["exit_hook_ok", "request_exit", "start_lifeline"]
