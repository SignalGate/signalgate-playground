"""What the sample app's log says a handler raised.

When a request fails with a 5xx, the app's server writes the exception's
traceback to ``.playground/app.log``. The code gate notes where the log ends
before it sends its probes (``log_offset``) and reads what was written after
that (``read_handler_errors``), so a finding can name the error and the line
in the routes file instead of only pointing at the log.

Only a few things are kept from a traceback: the exception's name, its
message, the innermost line in the routes file it went through, and the
outermost function there (the handler the request entered, even when a
helper or a lambda in the same file raised). Every other frame is ignored. The message is cleaned before it is kept:
anything shaped like an API key is masked, long runs of envelope-like
characters are cut, absolute paths under the repo root are shortened, and
the whole is capped in length. A missing or unreadable log reads as no
errors.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path

from playground.copy.en import text
from playground.verify.static import KEY_SHAPE

# Where the console writes the app's output, under the repo root.
APP_LOG_RELATIVE = Path(".playground") / "app.log"

_TRACEBACK = "Traceback (most recent call last):"
# The lines Python prints between the tracebacks of chained exceptions.
_CHAINED = (
    "The above exception was the direct cause of the following exception:",
    "During handling of the above exception, another exception occurred:",
)
_FRAME = re.compile(r'^\s*File "(?P<path>[^"]+)", line (?P<line>\d+), in (?P<function>.+)$')
_EXCEPTION_LINE = re.compile(r"^(?P<error>[A-Za-z_][\w.]*): (?P<message>.*)$")
_BARE_EXCEPTION_LINE = re.compile(r"^(?P<error>[A-Za-z_][\w.]*)$")
# Encrypted bytes, nonces and the like: never shown.
_LONG_RUN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
# How many characters of a message are kept.
MESSAGE_MAX_CHARS = 160


@dataclass(frozen=True)
class HandlerError:
    """One exception a handler raised: its name, its cleaned message, the
    innermost line of the routes file it went through, and the outermost
    function of that file it went through (the handler itself, when a helper
    or a lambda defined next to it is where it was raised)."""

    error: str
    message: str
    line: int
    function: str


@dataclass
class _Part:
    """One traceback as Python printed it: routes frames seen, then its exception."""

    frames: list[tuple[int, str]]
    exception: tuple[str, str] | None = None


def log_offset(path: Path) -> int:
    """Where the log at ``path`` ends now (0 when it doesn't exist)."""
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _read_from(path: Path, offset: int) -> str:
    try:
        with open(path, "rb") as handle:
            handle.seek(0, os.SEEK_END)
            size = handle.tell()
            # A log that shrank was started again: all of it is new.
            handle.seek(offset if 0 <= offset <= size else 0)
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def _same_file(frame_path: str, routes: Path, cache: dict[str, bool]) -> bool:
    if frame_path not in cache:
        try:
            cache[frame_path] = Path(frame_path).resolve() == routes
        except (OSError, ValueError, RuntimeError):
            cache[frame_path] = False
    return cache[frame_path]


def _records(lines: list[str], routes: Path) -> list[list[_Part]]:
    """Every logged exception in ``lines``: its traceback, with any chained ones before it."""
    records: list[list[_Part]] = []
    current: _Part | None = None
    chained = False
    cache: dict[str, bool] = {}
    for line in lines:
        stripped = line.strip()
        if stripped.endswith(_TRACEBACK):
            current = _Part([])
            if chained and records:
                records[-1].append(current)
            else:
                records.append([current])
            chained = False
            continue
        if stripped in _CHAINED:
            chained = True
            continue
        if current is None or current.exception is not None:
            if stripped:
                chained = False
            continue
        frame = _FRAME.match(line)
        if frame is not None:
            if _same_file(frame.group("path"), routes, cache):
                current.frames.append((int(frame.group("line")), frame.group("function").strip()))
            continue
        if line[:1].isspace() or not stripped:
            continue  # a source or caret line under a frame
        match = _EXCEPTION_LINE.match(line) or _BARE_EXCEPTION_LINE.match(line)
        if match is None:
            current = None  # not a traceback after all
            continue
        current.exception = (match.group("error"), match.groupdict().get("message") or "")
    return records


def _shorten_paths(message: str, root: Path) -> str:
    prefixes = {str(root)}
    try:
        prefixes.add(str(root.resolve()))
    except (OSError, RuntimeError):
        pass
    for prefix in sorted(prefixes, key=len, reverse=True):
        for separator in ("/", "\\"):
            message = message.replace(prefix.rstrip("/\\") + separator, "")
    return message


def clean_message(message: str, root: Path) -> str:
    """``message`` as it may be shown: paths shortened, keys masked, long runs cut, capped."""
    message = _shorten_paths(message, root)
    message = KEY_SHAPE.sub(text("verify.key_masked"), message)
    message = _LONG_RUN.sub(text("verify.ellipsis"), message)
    if len(message) > MESSAGE_MAX_CHARS:
        message = message[: MESSAGE_MAX_CHARS - 1] + text("verify.ellipsis")
    return message


def read_handler_errors(path: Path, offset: int, routes: Path) -> list[HandlerError]:
    """The exceptions logged at ``path`` after ``offset`` that went through ``routes``.

    One per logged exception, in the order they were logged. Of a chain of
    exceptions, the last one that went through the routes file counts, with
    the line of its innermost routes frame and the function of its outermost
    one. Paths in messages are shortened against the repo root, which holds
    ``app/routes.py``.
    """
    try:
        target = routes.resolve()
    except (OSError, RuntimeError):
        return []
    root = target.parent.parent
    found: list[HandlerError] = []
    for record in _records(_read_from(path, offset).splitlines(), target):
        usable = [part for part in record if part.frames and part.exception is not None]
        if not usable:
            continue
        part = usable[-1]
        assert part.exception is not None
        line = part.frames[-1][0]
        function = part.frames[0][1]
        error, message = part.exception
        found.append(HandlerError(error, clean_message(message, root), line, function))
    return found


__all__ = [
    "APP_LOG_RELATIVE",
    "MESSAGE_MAX_CHARS",
    "HandlerError",
    "clean_message",
    "log_offset",
    "read_handler_errors",
]
