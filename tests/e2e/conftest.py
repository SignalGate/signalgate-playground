"""Fixtures shared by the end-to-end console tests.

Every test here runs the console as a real subprocess, via
``sys.executable -m playground`` against a *copy* of the tracked tree in a
temp directory -- never this checkout -- so a run can never leave an
``app/.env`` (or anything else) behind in the working tree. ``uv run`` is not
used to launch that subprocess: ``sys.executable`` already is the
interpreter ``uv`` set up for this checkout, with every dependency on its
own site-packages, so pointing it at the temp copy's source (via ``cwd`` and
``PYTHONPATH``) is faster and just as deterministic as re-resolving the
project through ``uv`` again for every test.

When a test ends, no sample app it started may still be running. Only the
test's own apps count: the ones its boot files name, and any sample app
whose working directory is inside its temp tree. Sample apps started by
anything else on this machine (a developer's own console, another checkout's
tests) are never counted and never touched.
"""

from __future__ import annotations

import contextlib
import json
import os
import queue
import re
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Iterator, Mapping
from pathlib import Path

import filelock
import pytest

from playground.traffic.local_constants import (
    APP_START_TIMEOUT_S,
    APP_STOP_GRACE_S,
    LOOPBACK_REQUEST_TIMEOUT_S,
)
from playground.tui.prompt import PROMPT_GLYPH
from tests.e2e.seam import Seam, install_hook, python_path

REPO_ROOT = Path(__file__).resolve().parents[2]

# How long a console may take to exit once asked to: the exit path's own
# bound (a graceful stop of the sample app, then a kill after the grace)
# plus the time to reap it.
EXIT_WITHIN_S = 2 * APP_STOP_GRACE_S

# How long the opening may take to appear: the sample app's own start-up
# bound, plus the console's own start.
READY_WITHIN_S = APP_START_TIMEOUT_S + APP_STOP_GRACE_S

# How long a typed command may take to answer.
REPLY_WITHIN_S = LOOPBACK_REQUEST_TIMEOUT_S

# The budget for a whole scripted chapter-1 session.
RUN_WITHIN_S = 55.0

# The command line every sample app runs with (see the launcher).
APP_COMMAND_MARK = "uvicorn app.main:app"

# Keys and screen text the terminal tests share.
CTRL_C = b"\x03"
CTRL_D = b"\x04"

OPENING = "Type /start when you're ready."
RUN_PANEL = "Actions"
CLOSE_QUESTION = "Ready to connect SignalGate? (yes/no)"
HELP_LAST_COMMAND = "/quit      stop and exit"
BYE = "Bye. Run uv run playground"
SAVED = "Progress saved."
PROMPT = PROMPT_GLYPH.strip()


def _tracked_files() -> list[str]:
    output = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT,
        check=True,
        capture_output=True,
    ).stdout
    return [path for path in output.decode().split("\0") if path]


def copy_tracked_tree(dest: Path) -> Path:
    """Copy every git-tracked file into ``dest``; return ``dest``."""
    for relative in _tracked_files():
        target = dest / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(REPO_ROOT / relative, target)
    return dest


# -- the sample apps a test owns ------------------------------------------------


def command_of(pid: int) -> str:
    """The full command line of ``pid``, or "" once it is gone."""
    result = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    return result.stdout.strip()


def _cwd_of(pid: int) -> Path | None:
    if sys.platform.startswith("linux"):
        try:
            return Path(os.readlink(f"/proc/{pid}/cwd"))
        except OSError:
            return None
    result = subprocess.run(
        ["lsof", "-a", "-d", "cwd", "-p", str(pid), "-Fn"],
        capture_output=True,
        text=True,
        check=False,
    )
    for line in result.stdout.splitlines():
        if line.startswith("n"):
            return Path(line[1:])
    return None


def _is_inside(path: Path, tree: Path) -> bool:
    resolved = path.resolve()
    root = tree.resolve()
    return resolved == root or root in resolved.parents


def owned_app_pids(tree: Path, boot_pids: Iterable[int] = ()) -> set[int]:
    """The live sample apps a test owns.

    Those are the processes its boot files name, plus any sample app whose
    working directory is inside ``tree``. A process is never matched by its
    command line alone.
    """
    owned = {pid for pid in boot_pids if "uvicorn" in command_of(pid)}
    result = subprocess.run(
        ["pgrep", "-f", APP_COMMAND_MARK], capture_output=True, text=True, check=False
    )
    for token in result.stdout.split():
        pid = int(token)
        cwd = _cwd_of(pid)
        if cwd is not None and _is_inside(cwd, tree):
            owned.add(pid)
    return owned


def boot_file_pids(base: Path) -> list[int]:
    """The pids named by the boot files of every seam under ``base``."""
    pids: list[int] = []
    for path in base.glob("*/dump/boot-*.json"):
        with contextlib.suppress(OSError, ValueError, KeyError):
            pids.append(int(json.loads(path.read_text())["pid"]))
    return pids


def wait_until_gone(tree: Path, boot_pids: Iterable[int], within: float) -> set[int]:
    """Wait up to ``within`` for the test's own apps to exit; return any left."""
    pids = list(boot_pids)
    deadline = time.monotonic() + within
    while True:
        left = owned_app_pids(tree, pids)
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(LOOPBACK_REQUEST_TIMEOUT_S / 100)


def wait_pids_gone(pids: Iterable[int], within: float) -> set[int]:
    """Wait up to ``within`` for these sample-app processes to exit; return any left."""
    pids = list(pids)
    deadline = time.monotonic() + within
    while True:
        left = {pid for pid in pids if "uvicorn" in command_of(pid)}
        if not left or time.monotonic() > deadline:
            return left
        time.sleep(LOOPBACK_REQUEST_TIMEOUT_S / 100)


def apps_left_running(tree: Path) -> set[int]:
    """The test's own sample apps still running right now, with no wait.

    A console stops its app, and waits for it to exit, before it exits
    itself, so once a console has exited this must be empty at once (an
    app that only goes away later went by its own lifeline instead).
    """
    return owned_app_pids(tree, boot_file_pids(tree.parent))


def _no_app_left(base: Path, tree: Path) -> None:
    left = wait_until_gone(tree, boot_file_pids(base), EXIT_WITHIN_S)
    for pid in left:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    assert not left, f"sample app process(es) left running: {sorted(left)}"


@pytest.fixture
def tracked_copy(tmp_path: Path) -> Iterator[Path]:
    """A copy of every git-tracked file, in its own temp directory.

    When the test ends, no sample app it started may still be running.
    """
    dest = copy_tracked_tree(tmp_path / "tree")
    yield dest
    _no_app_left(tmp_path, dest)


@pytest.fixture
def no_network_hook_dir(tmp_path: Path) -> Path:
    """A directory holding the loopback-only start-up hook, for ``PYTHONPATH``."""
    return install_hook(tmp_path / "no_network_hook")


@pytest.fixture
def seam(tmp_path: Path) -> Seam:
    """The start-up hook with the SDK answered locally."""
    return Seam.create(tmp_path / "seam")


@pytest.fixture
def gated_seam(tmp_path: Path) -> Seam:
    """The start-up hook with the SDK answered locally and log deliveries held."""
    return Seam.create(tmp_path / "seam", gated=True)


def plain_env(tree: Path, hook_dir: Path, extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment for a console run from ``tree`` with only the loopback hook."""
    env = dict(os.environ)
    env["PYTHONPATH"] = python_path(hook_dir, tree)
    env.update(extra or {})
    return env


# -- a sample app that logs --------------------------------------------------------

LOGGING_ROUTES = Path(__file__).with_name("fixtures") / "routes_logging.py"

# A made-up value for app/.env: never a real key, never sent anywhere (the
# start-up hook answers the SDK's calls locally).
DUMMY_KEY = "e2e-dummy-value-not-a-real-one"

# The logging routes log one action in this many.
LOGGED_ONE_IN = 10


def use_logging_app(tree: Path) -> None:
    """Make the sample app in ``tree`` log one action in ten, with a dummy key set."""
    shutil.copyfile(LOGGING_ROUTES, tree / "app" / "routes.py")
    (tree / "app" / ".env").write_text(f"SIGNALGATE_API_KEY={DUMMY_KEY}\n")


def append_comment(path: Path, note: str = "saved") -> None:
    """Save ``path`` with one more comment line, as an editor would."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(f"# {note}\n")


# -- a console in --ci mode ---------------------------------------------------------


class CiConsole:
    """A ``--ci`` console subprocess, its JSON lines read as they arrive."""

    def __init__(self, cwd: Path, env: Mapping[str, str], args: Iterable[str]) -> None:
        self.process = subprocess.Popen(
            [sys.executable, "-m", "playground", "--ci", *args],
            cwd=cwd,
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.lines: list[dict] = []
        self.stderr_lines: list[str] = []
        self._queue: queue.Queue[dict | None] = queue.Queue()
        threading.Thread(target=self._pump_stdout, daemon=True).start()
        threading.Thread(target=self._pump_stderr, daemon=True).start()

    def _pump_stdout(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            if line.strip():
                self._queue.put(json.loads(line))
        self._queue.put(None)

    def _pump_stderr(self) -> None:
        assert self.process.stderr is not None
        for line in self.process.stderr:
            self.stderr_lines.append(line)

    @property
    def stderr(self) -> str:
        return "".join(self.stderr_lines)

    def next_line(self, within: float) -> dict | None:
        """The next JSON line, or None once stdout closed."""
        try:
            item = self._queue.get(timeout=within)
        except queue.Empty:
            raise AssertionError(
                f"no output within {within}s; last lines: {self.lines[-5:]}"
            ) from None
        if item is not None:
            self.lines.append(item)
        else:
            self._queue.put(None)  # stay closed for later reads
        return item

    def expect(self, within: float, **fields: object) -> dict:
        """Read until a line has every ``fields`` value; return that line."""
        deadline = time.monotonic() + within
        while True:
            line = self.next_line(max(0.0, deadline - time.monotonic()))
            if line is None:
                raise AssertionError(f"output ended before {fields}; last: {self.lines[-5:]}")
            if all(line.get(key) == value for key, value in fields.items()):
                return line

    def send(self, answer: str) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(answer + "\n")
        self.process.stdin.flush()

    def read_to_end(self, within: float) -> None:
        deadline = time.monotonic() + within
        while self.next_line(max(0.0, deadline - time.monotonic())) is not None:
            pass

    def wait_exit(self, within: float) -> int:
        try:
            code = self.process.wait(timeout=within)
        except subprocess.TimeoutExpired:
            self.process.kill()
            self.process.wait()
            raise AssertionError(f"console did not exit; last lines: {self.lines[-5:]}") from None
        self.read_to_end(within)
        return code

    def says(self) -> list[dict]:
        return [line for line in self.lines if line.get("type") == "say"]

    def say_keys(self) -> list[str]:
        return [line["key"] for line in self.says()]

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        for stream in (self.process.stdin, self.process.stdout, self.process.stderr):
            if stream is not None:
                with contextlib.suppress(OSError):
                    stream.close()


@pytest.fixture
def open_ci_console(tracked_copy: Path) -> Iterator[Callable[..., CiConsole]]:
    """Start ``--ci`` consoles in ``tracked_copy``; all stopped at the end."""
    opened: list[CiConsole] = []

    def open_one(env: Mapping[str, str], *args: str) -> CiConsole:
        console = CiConsole(tracked_copy, env, args)
        opened.append(console)
        return console

    yield open_one
    for console in opened:
        console.close()


# -- a console on a real terminal -------------------------------------------------

_ESCAPES = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07|\x1b[()][A-Za-z0-9]|\x1b[=>]")

# Makes the child's standard input its controlling terminal, then runs the
# console exactly as ``python -m playground`` does, with the flags given
# after ``--``. A plain fork of the (multi-threaded) test process is never
# used; the child is started fresh.
_CHILD = (
    "import fcntl, runpy, sys, termios\n"
    "fcntl.ioctl(0, termios.TIOCSCTTY, 0)\n"
    "sys.argv = ['playground', *sys.argv[sys.argv.index('--') + 1 :]]\n"
    "runpy.run_module('playground', run_name='__main__', alter_sys=True)\n"
)

TERMINAL_ROWS = 50
TERMINAL_COLUMNS = 200

_WHITESPACE = re.compile(r"\s+")


def flatten(text: str) -> str:
    """``text`` with every run of whitespace (line breaks too) made one space.

    The console wraps its paragraphs, so a line of copy can reach the screen
    broken in two anywhere a space was. Screen text and the text looked for
    are both flattened before they are compared.
    """
    return _WHITESPACE.sub(" ", text)


class Terminal:
    """A console process attached to a pseudo-terminal, read incrementally.

    ``expect`` and ``count`` compare flattened text (see ``flatten``), so a
    wrapped line still matches; ``expect_exact`` compares the text as it is,
    for checks about the lines themselves. Both move one shared cursor past
    what they matched.
    """

    def __init__(self, cwd: Path, env: Mapping[str, str], args: Iterable[str] = ()) -> None:
        import fcntl
        import pty
        import struct
        import termios

        full_env = dict(env)
        full_env.update(
            {
                "TERM": "xterm-256color",
                "COLUMNS": str(TERMINAL_COLUMNS),
                "LINES": str(TERMINAL_ROWS),
                "NO_COLOR": "1",
                "PROMPT_TOOLKIT_NO_CPR": "1",
            }
        )
        master, slave = pty.openpty()
        size = struct.pack("HHHH", TERMINAL_ROWS, TERMINAL_COLUMNS, 0, 0)
        fcntl.ioctl(slave, termios.TIOCSWINSZ, size)
        self.process = subprocess.Popen(
            [sys.executable, "-c", _CHILD, "--", *args],
            cwd=cwd,
            env=full_env,
            stdin=slave,
            stdout=slave,
            stderr=slave,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave)
        self.fd: int | None = master
        self.raw = ""
        # Where the next search starts: in ``text``, and the same place in ``flat``.
        self._cursor = 0
        self._flat_cursor = 0

    @property
    def pid(self) -> int:
        return self.process.pid

    @property
    def text(self) -> str:
        """Everything shown so far, with terminal control sequences removed."""
        return _ESCAPES.sub("", self.raw).replace("\r", "")

    @property
    def flat(self) -> str:
        """``text`` flattened (see ``flatten``): search copy in this, not in ``text``."""
        return flatten(self.text)

    def _read_available(self, timeout: float) -> None:
        if self.fd is None:
            time.sleep(timeout)
            return
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if ready:
            try:
                chunk = os.read(self.fd, 65536)
            except OSError:
                chunk = b""
            self.raw += chunk.decode("utf-8", errors="replace")

    def drain_output(self, within: float) -> None:
        """Read whatever arrives during ``within`` seconds."""
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            self._read_available(min(0.1, within))

    def expect(self, needle: str, within: float) -> None:
        """Wait for ``needle`` after the last match, however the screen
        wrapped it; move past it."""
        flat_needle = flatten(needle)
        deadline = time.monotonic() + within
        while True:
            text = self.text
            found = flatten(text).find(flat_needle, self._flat_cursor)
            if found >= 0:
                self._flat_cursor = found + len(flat_needle)
                self._cursor = _text_index(text, self._flat_cursor)
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"did not see {needle!r}; tail:\n{text[-3000:]}")
            self._read_available(0.1)

    def expect_exact(self, needle: str, within: float) -> None:
        """Wait for ``needle`` exactly as written, line breaks and spaces
        included, after the last match; move past it."""
        deadline = time.monotonic() + within
        while True:
            text = self.text
            found = text.find(needle, self._cursor)
            if found >= 0:
                self._cursor = found + len(needle)
                self._flat_cursor = len(flatten(text[: self._cursor]))
                return
            if time.monotonic() > deadline:
                raise AssertionError(f"did not see exactly {needle!r}; tail:\n{text[-3000:]}")
            self._read_available(0.1)

    def count(self, needle: str) -> int:
        """How often ``needle`` was shown so far, however the screen wrapped it."""
        return self.flat.count(flatten(needle))

    def type(self, line: str) -> None:
        self.press(line.encode() + b"\r")

    def press(self, keys: bytes) -> None:
        assert self.fd is not None
        os.write(self.fd, keys)

    def poll_exit(self) -> int | None:
        return self.process.poll()

    def wait_exit(self, within: float) -> int:
        deadline = time.monotonic() + within
        while time.monotonic() < deadline:
            code = self.process.poll()
            if code is not None:
                self._read_available(0.1)
                return code
            self._read_available(0.1)
        self.process.kill()
        self.process.wait()
        raise AssertionError(f"console did not exit; tail:\n{self.text[-3000:]}")

    def hang_up(self) -> None:
        """Close the terminal from our side, as closing a terminal window does."""
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None

    def close(self) -> None:
        if self.process.poll() is None:
            self.process.kill()
        self.process.wait()
        self.hang_up()


def _text_index(text: str, flat_index: int) -> int:
    """Where in ``text`` the first ``flat_index`` characters of ``flatten(text)`` end.

    A whitespace run is one character of the flattened text, so a place just
    after one is taken to be after the whole run.
    """
    flat_at = text_at = 0
    for run in _WHITESPACE.finditer(text):
        word = run.start() - text_at
        if flat_at + word >= flat_index:
            return text_at + (flat_index - flat_at)
        flat_at += word + 1
        text_at = run.end()
        if flat_at >= flat_index:
            return text_at
    return min(len(text), text_at + (flat_index - flat_at))


def ended_cleanly(terminal: Terminal, tree: Path) -> None:
    """No traceback on screen, no sample app left, and the lock is free again."""
    assert "Traceback" not in terminal.text
    assert apps_left_running(tree) == set()
    lock = filelock.FileLock(str(tree / ".playground" / "console.lock"), timeout=0)
    lock.acquire()
    lock.release()


def wait_for_run_panel(terminal: Terminal) -> None:
    """Wait until a run's live panel is on screen."""
    terminal.expect(RUN_PANEL, REPLY_WITHIN_S)


@pytest.fixture
def open_terminal(tracked_copy: Path) -> Iterator[Callable[..., Terminal]]:
    """Start consoles on real terminals in ``tracked_copy``; all closed at the end."""
    opened: list[Terminal] = []

    def open_one(env: Mapping[str, str], *args: str) -> Terminal:
        terminal = Terminal(tracked_copy, env, args)
        opened.append(terminal)
        return terminal

    yield open_one
    for terminal in opened:
        terminal.close()


__all__ = [
    "BYE",
    "CLOSE_QUESTION",
    "CTRL_C",
    "CTRL_D",
    "DUMMY_KEY",
    "EXIT_WITHIN_S",
    "HELP_LAST_COMMAND",
    "LOGGED_ONE_IN",
    "OPENING",
    "PROMPT",
    "READY_WITHIN_S",
    "REPLY_WITHIN_S",
    "REPO_ROOT",
    "RUN_PANEL",
    "RUN_WITHIN_S",
    "SAVED",
    "CiConsole",
    "Terminal",
    "append_comment",
    "apps_left_running",
    "command_of",
    "copy_tracked_tree",
    "ended_cleanly",
    "flatten",
    "owned_app_pids",
    "plain_env",
    "use_logging_app",
    "wait_for_run_panel",
    "wait_pids_gone",
    "wait_until_gone",
]
