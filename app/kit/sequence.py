"""A monotonically increasing counter and an append-only ledger.

Both are shared, in-memory, per-process structures: `next_seq()` hands out
strictly increasing sequence numbers, and `ledger` records what happened, in
order, so later readers can pick up where they left off with a cursor.

`Ledger.append` stamps each entry's `seq` itself, while it holds the
ledger's own lock, so an entry's position in the ledger and its `seq` always
agree: paging by position yields entries in `seq` order even when several
threads append at once. Call and construct records take their `seq` from
the same counter through `next_seq()`.

Every append is also written as one JSON line to stdout, in that same
order, so a process watching this app from the outside (a test, a log file)
can observe the ledger without a dedicated read route.

`read_page` pages through the ledger by position: the cursor indexes the
full, unfiltered ledger, and an optional `run` narrows which entries come
back without changing what the cursor counts.
"""

from __future__ import annotations

import json
import threading
from typing import Any

# The page limit is looked up in this module at call time, so a test can
# patch it small here.
from app.kit.local_constants import PAGE_MAX

_seq_lock = threading.Lock()
_next_seq = 1


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def next_seq() -> int:
    """Return a fresh, strictly increasing sequence number."""
    global _next_seq
    with _seq_lock:
        seq = _next_seq
        _next_seq += 1
    return seq


class Ledger:
    """An append-only, in-memory log with cursor-based reads."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        # Held across one whole append, echo included, so stdout lines come
        # out in ledger order; readers only ever take `_lock`, which is never
        # held while writing to stdout.
        self._echo_lock = threading.Lock()
        self._entries: list[dict[str, Any]] = []

    def append(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Stamp `entry` with a fresh `seq`, add it to the ledger, and echo it
        to stdout as one JSON line. Returns the stored entry.

        `entry` must not carry a `seq` of its own: the ledger assigns it, so
        position order and `seq` order can never disagree.
        """
        if "seq" in entry:
            raise ValueError("the ledger assigns seq itself")
        with self._echo_lock:
            with self._lock:
                stored = {"kind": entry["kind"]} if "kind" in entry else {}
                stored["seq"] = next_seq()
                stored.update(entry)
                line = json.dumps({"ledger": stored})
                self._entries.append(stored)
            print(line, flush=True)
        return dict(stored)

    def read_from(self, cursor: int = 0) -> tuple[list[dict[str, Any]], int]:
        """Return the entries at or after `cursor`, and the cursor for the next read."""
        with self._lock:
            entries = list(self._entries[cursor:])
            next_cursor = len(self._entries)
        return entries, next_cursor

    def read_page(
        self, cursor: int = 0, limit: int | None = None, run: str | None = None
    ) -> tuple[list[dict[str, Any]], int, bool]:
        """Return `(entries, next_cursor, more)`.

        `cursor` indexes the full, unfiltered ledger. Scanning starts there
        and stops once `limit` entries matching `run` (all runs when `run`
        is None) have been collected, or the ledger is exhausted; `entries`
        holds what matched, in order. `next_cursor` is the position right
        after the last entry scanned (not necessarily the last one
        returned), and `more` says whether the ledger, as of this read, has
        any entries past it.

        Raises `ValueError` for a cursor that isn't a non-negative int no
        larger than the number of entries appended so far, or a limit
        outside `1..PAGE_MAX`.
        """
        page_max = int(PAGE_MAX)
        if limit is None:
            limit = page_max
        if not _is_int(cursor) or cursor < 0:
            raise ValueError("cursor must be a non-negative int")
        if not _is_int(limit) or not 1 <= limit <= page_max:
            raise ValueError("limit is out of range")
        with self._lock:
            total = len(self._entries)
            if cursor > total:
                raise ValueError("cursor is past the end of the ledger")
            matched: list[dict[str, Any]] = []
            index = cursor
            while index < total and len(matched) < limit:
                entry = self._entries[index]
                index += 1
                if run is None or entry.get("run") == run:
                    matched.append(dict(entry))
            more = index < total
        return matched, index, more

    def __len__(self) -> int:
        with self._lock:
            return len(self._entries)

    def reset(self) -> None:
        """Forget every entry (tests only)."""
        with self._lock:
            self._entries = []


ledger = Ledger()

__all__ = ["Ledger", "ledger", "next_seq"]
