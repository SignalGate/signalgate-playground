"""In-memory call records: what the app's SignalGate client was asked to do.

The recording client (`app.kit.facade`) and the kit transport
(`app.kit.kit_transport`) each append one record per call or per attempt to
`calls`, and the console reads them back, a page at a time, through the
control routes. Records live in memory for one boot of the app. Nothing here
is ever printed, logged or written to disk.

A record's position is its index among every record appended since the app
started (or since `reset()`), so positions keep counting when the oldest
records are dropped to stay under the cap. A reader pages with cursors that
are positions.

The helpers below collect what a record needs from the calling code:
`capture()` (which request, which run, which thread, the call site),
`bind_call()` / `current_call()` (which check call a transport attempt
belongs to) and `j()` (a JSON-safe copy of a value the caller passed).
"""

from __future__ import annotations

import asyncio
import functools
import importlib.util
import json
import math
import os
import sys
import threading
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from app.kit import context

# The cap, the page limit and the string limit are looked up in this module
# at call time, so a test can patch them small here.
from app.kit.local_constants import CALLS_MAX_RECORDS, PAGE_MAX, RECORD_STR_MAX


class CallLog:
    """An append-only, capped, in-memory list of records, read by cursor."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # JSON text of each live record. `_texts[_head]` is the oldest live
        # record; entries before `_head` were dropped and are trimmed lazily.
        self._texts: list[str] = []
        self._head = 0
        # The position of the oldest live record (how many were dropped).
        self._oldest = 0
        self._record_errors = 0

    @property
    def record_errors(self) -> int:
        """How many records could not be stored (they were not JSON-safe)."""
        with self._lock:
            return self._record_errors

    def __len__(self) -> int:
        with self._lock:
            return len(self._texts) - self._head

    def append(self, record: Mapping[str, Any]) -> None:
        """Store a copy of ``record``. Never raises.

        The record is turned into JSON text once, here, so a later change to
        the caller's dict never changes what was stored. A record that isn't
        a mapping, or can't be written as strict JSON, is counted in
        ``record_errors`` instead.
        """
        try:
            self._store(record)
        except Exception:  # noqa: BLE001 - recording must never break the caller
            with self._lock:
                self._record_errors += 1

    def _store(self, record: Mapping[str, Any]) -> None:
        if not isinstance(record, Mapping):
            raise TypeError("a call record must be a mapping")
        text = json.dumps(dict(record), allow_nan=False, separators=(",", ":"))
        cap = max(0, int(CALLS_MAX_RECORDS))
        with self._lock:
            self._texts.append(text)
            live = len(self._texts) - self._head
            if live > cap:
                dropped = live - cap
                self._head += dropped
                self._oldest += dropped
            if self._head and self._head * 2 >= len(self._texts):
                del self._texts[: self._head]
                self._head = 0

    def read_page(
        self, cursor: int = 0, limit: int | None = None
    ) -> tuple[list[dict[str, Any]], int, bool, int, bool]:
        """Return ``(records, next_cursor, more, oldest_cursor, truncated)``.

        ``records`` holds up to ``limit`` records (default ``PAGE_MAX``) in
        append order, starting at position ``cursor``, or at
        ``oldest_cursor`` when the records at ``cursor`` were already
        dropped; ``truncated`` is true exactly then. ``next_cursor`` is the
        position after the last record returned, and ``more`` says whether
        records exist past it.

        Raises ``ValueError`` for a cursor that isn't a non-negative int no
        larger than the number of records appended so far, or a limit
        outside ``1..PAGE_MAX``.
        """
        page_max = int(PAGE_MAX)
        if limit is None:
            limit = page_max
        if not _is_int(cursor) or cursor < 0:
            raise ValueError("cursor must be a non-negative int")
        if not _is_int(limit) or not 1 <= limit <= page_max:
            raise ValueError("limit is out of range")
        with self._lock:
            oldest = self._oldest
            end_of_log = oldest + len(self._texts) - self._head
            if cursor > end_of_log:
                raise ValueError("cursor is past the end of the records")
            truncated = cursor < oldest
            start = max(cursor, oldest)
            stop = min(start + limit, end_of_log)
            first = self._head + (start - oldest)
            texts = self._texts[first : first + (stop - start)]
        records = [json.loads(text) for text in texts]
        return records, stop, stop < end_of_log, oldest, truncated

    def reset(self) -> None:
        """Forget every record and the error count (for tests)."""
        with self._lock:
            self._texts = []
            self._head = 0
            self._oldest = 0
            self._record_errors = 0


calls = CallLog()


# --- which check call a transport attempt belongs to -------------------------

_bound = threading.local()


def _call_stack() -> list[int]:
    stack = getattr(_bound, "stack", None)
    if stack is None:
        stack = []
        _bound.stack = stack
    return stack


@contextmanager
def bind_call(seq: int) -> Iterator[None]:
    """Mark the calling thread as inside the call numbered ``seq``.

    Nests: the innermost binding is the current one until it exits.
    """
    stack = _call_stack()
    stack.append(seq)
    try:
        yield
    finally:
        stack.pop()


def current_call() -> int | None:
    """The ``seq`` of the innermost call bound on this thread, or None."""
    stack = getattr(_bound, "stack", None)
    return stack[-1] if stack else None


# --- what a record needs from the calling code -------------------------------


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def j(value: Any) -> Any:
    """A JSON-safe copy of ``value`` for a record.

    A JSON scalar is kept as given (a string is cut to ``RECORD_STR_MAX``
    characters, and a float must be finite). Anything else becomes
    ``{"type": <its type name>}``, so a record shows what kind of value was
    passed without holding the value itself.
    """
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value[: int(RECORD_STR_MAX)]
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float) and math.isfinite(value):
        return float(value)
    return {"type": type(value).__name__}


_REPO_ROOT = Path(__file__).resolve().parents[2]
_REPO_ROOT_TEXT = os.path.realpath(_REPO_ROOT)
_KIT_DIR_TEXT = os.path.realpath(_REPO_ROOT / "app" / "kit")
_CLIENT_FILE_TEXT = os.path.realpath(_REPO_ROOT / "app" / "signalgate_client.py")
_PACKAGE_DIR_NAMES = frozenset({"site-packages", "dist-packages"})

# Classification results for `_classify`.
_SKIP = "skip"
_OUTSIDE = "outside"


def _is_under(path: str, directory: str) -> bool:
    return path == directory or path.startswith(directory + os.sep)


@functools.lru_cache(maxsize=1)
def _sdk_dir() -> str | None:
    """Where the installed SDK package lives, found without importing it."""
    try:
        spec = importlib.util.find_spec("signalgate")
    except (ImportError, ValueError):
        return None
    if spec is None or not spec.origin:
        return None
    return os.path.realpath(os.path.dirname(spec.origin))


@functools.lru_cache(maxsize=4096)
def _classify(filename: str) -> str:
    """``_SKIP`` for scaffolding frames, ``_OUTSIDE`` for frames outside the
    repo, or the repo-relative POSIX path of a frame in the app's own code."""
    if not filename or filename.startswith("<"):
        return _OUTSIDE
    real = os.path.realpath(filename)
    if _PACKAGE_DIR_NAMES.intersection(Path(real).parts):
        return _SKIP
    sdk_dir = _sdk_dir()
    if sdk_dir is not None and _is_under(real, sdk_dir):
        return _SKIP
    if _is_under(real, _KIT_DIR_TEXT) or real == _CLIENT_FILE_TEXT:
        return _SKIP
    if not _is_under(real, _REPO_ROOT_TEXT) or real == _REPO_ROOT_TEXT:
        return _OUTSIDE
    return Path(os.path.relpath(real, _REPO_ROOT_TEXT)).as_posix()


def _site(frame: Any) -> dict[str, Any] | None:
    """The first frame outside the scaffolding, as ``{file, line, func}``.

    Walks the live frames (it never reads source lines) past this kit,
    ``app/signalgate_client.py``, the SDK and any installed package. Returns
    None when that frame is outside the repo.
    """
    while frame is not None:
        where = _classify(frame.f_code.co_filename)
        if where == _SKIP:
            frame = frame.f_back
            continue
        if where == _OUTSIDE:
            return None
        return {"file": where, "line": frame.f_lineno, "func": frame.f_code.co_name}
    return None


def _on_loop() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def capture() -> dict[str, Any]:
    """What a record needs to know about the code that made the call.

    Returns ``{ref, run, site, thread, on_loop}``: the request's ref and run
    (empty outside a request), the call site (see ``_site``), the calling
    thread's name, and whether an asyncio loop is running on this thread.
    Never raises; a part that can't be read is left empty.
    """
    caller = sys._getframe(1)
    return {
        "ref": _read_or(context.get_ref, ""),
        "run": _read_or(context.get_run, ""),
        "site": _read_or(lambda: _site(caller), None),
        "thread": _read_or(lambda: threading.current_thread().name, ""),
        "on_loop": _read_or(_on_loop, False),
    }


def _read_or(read: Callable[[], Any], default: Any) -> Any:
    try:
        return read()
    except Exception:  # noqa: BLE001 - recording must never break the caller
        return default


__all__ = [
    "CallLog",
    "bind_call",
    "calls",
    "capture",
    "current_call",
    "j",
]
