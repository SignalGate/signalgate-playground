"""Watches the sample app's own files for changes worth a restart.

Exactly two things count: any ``*.py`` under ``app/`` (recursively, minus the
usual noise -- caches, dotfiles, editor swap files) and ``app/.env`` itself.
Everything else -- ``tests/``, ``playground/``, ``.playground/``, an
``.env.example`` -- never triggers a restart.
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from pathlib import Path

from watchfiles import Change, DefaultFilter, awatch

from playground.traffic.local_constants import RELOAD_DEBOUNCE_S, RELOAD_SETTLE_S

ENV_BASENAME = ".env"

_FILTER = DefaultFilter()


def _matches_one(path: Path, app_dir: Path) -> bool:
    try:
        relative = path.relative_to(app_dir)
    except ValueError:
        return False
    if relative == Path(ENV_BASENAME):
        return True
    if path.suffix != ".py":
        return False
    return _FILTER(Change.added, str(path))


def _resolved_or_given(path: Path) -> Path:
    """``path.resolve()``, or ``path`` itself when it can't be resolved (a
    symlink that points back at itself, say): a path the watcher is asked
    about must never end the watch."""
    try:
        return path.resolve()
    except (OSError, RuntimeError):
        return path


def is_watched(path: str, app_dir: str) -> bool:
    """True for ``app_dir/.env``, or a watched ``*.py`` under ``app_dir``.

    Compares both the path as given and its resolved form against both
    spellings of ``app_dir``, so a caller need not resolve anything first.
    """
    given = Path(path)
    resolved = _resolved_or_given(given)
    given_dir = Path(app_dir)
    resolved_dir = _resolved_or_given(given_dir)
    for candidate in (given, resolved):
        for directory in (given_dir, resolved_dir):
            if _matches_one(candidate, directory):
                return True
    return False


def _stat_pair(path: Path) -> tuple[int, int]:
    try:
        info = path.stat()
    except OSError:
        return (-1, -1)
    return (info.st_size, info.st_mtime_ns)


_UNREADABLE_MARKER = b"<unreadable>"


def _read_bytes_or_marker(path: Path) -> bytes:
    """``path.read_bytes()``, or a stable marker for a file that's vanished,
    is unreadable, or turned into something else between the walk and this
    read. A hash that can't be taken must never crash the watcher, on this
    call or at start-up."""
    try:
        return path.read_bytes()
    except OSError:
        return _UNREADABLE_MARKER


def _is_watched_file(path: Path, app_dir: Path) -> bool:
    try:
        return path.is_file() and is_watched(str(path), str(app_dir))
    except OSError:
        return False


def _watched_py_files(app_dir: Path) -> list[Path]:
    """Every watched ``*.py`` under ``app_dir`` as the tree is right now.

    Files and whole directories can come and go while this runs (a package
    renamed or deleted mid-save). ``os.walk`` skips a directory it can't
    open or finish listing instead of raising, and each file is checked on
    its own, so the result is simply the tree as it was found.
    """
    found: list[Path] = []
    for directory, _dirnames, filenames in os.walk(app_dir):
        for name in filenames:
            if not name.endswith(".py"):
                continue
            path = Path(directory, name)
            if _is_watched_file(path, app_dir):
                found.append(path)
    return sorted(found, key=lambda path: path.relative_to(app_dir).as_posix())


def _code_hash_sync(app_dir: Path) -> str:
    digest = hashlib.sha256()
    for path in _watched_py_files(app_dir):
        digest.update(path.relative_to(app_dir).as_posix().encode("utf-8"))
        digest.update(_read_bytes_or_marker(path))

    size, mtime_ns = _stat_pair(app_dir / ENV_BASENAME)
    digest.update(f"env:{size}:{mtime_ns}".encode())
    return digest.hexdigest()


async def code_hash(app_dir: str) -> str:
    """A sha256 over what each watched file currently is: contents for
    ``*.py``, size and mtime only (never contents) for ``app/.env``.
    """
    return await asyncio.to_thread(_code_hash_sync, Path(app_dir))


class AppWatcher:
    """Tracks whether the sample app's own files have changed since baseline."""

    def __init__(self, app_dir: str) -> None:
        self._app_dir = Path(app_dir)
        self._baseline_hash = _code_hash_sync(self._app_dir)
        self.dirty = False
        self.last_change: float | None = None
        self.generation = 0
        self._dirty_event = asyncio.Event()

    async def run(self, stop: asyncio.Event) -> None:
        """Watch until ``stop`` fires, marking dirty on every real change."""

        def _watch_filter(_change: Change, path: str) -> bool:
            return is_watched(path, str(self._app_dir))

        async for _batch in awatch(
            self._app_dir,
            watch_filter=_watch_filter,
            debounce=int(RELOAD_DEBOUNCE_S * 1000),
            stop_event=stop,
            recursive=True,
        ):
            current = await code_hash(str(self._app_dir))
            if current == self._baseline_hash:
                continue
            # A save that empties a file and then rewrites it can be caught
            # half-way, with the file briefly empty. Look again once the write
            # has had a moment to finish before calling it a change.
            await asyncio.sleep(RELOAD_SETTLE_S)
            if await code_hash(str(self._app_dir)) == self._baseline_hash:
                continue
            self.dirty = True
            self.last_change = time.monotonic()
            self.generation += 1
            self._dirty_event.set()

    async def baseline(self) -> None:
        """Re-hash now and clear ``dirty``; called right before every spawn."""
        self._baseline_hash = await code_hash(str(self._app_dir))
        self.dirty = False
        self._dirty_event.clear()

    async def wait_dirty(self) -> None:
        """Wait until a real change has been seen since the last baseline."""
        await self._dirty_event.wait()

    async def wait_settled(self) -> None:
        """Wait for ``dirty`` and then for one quiet ``RELOAD_DEBOUNCE_S`` window."""
        await self.wait_dirty()
        while True:
            changed_at = self.last_change
            await asyncio.sleep(RELOAD_DEBOUNCE_S)
            if self.last_change == changed_at:
                return


__all__ = ["ENV_BASENAME", "AppWatcher", "code_hash", "is_watched"]
