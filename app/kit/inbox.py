"""A confirmation token store keyed by visitor ref, read-once.

`kit.actions` puts a token here after a successful action, together with the
run the issuing action belonged to; the control route (standing in for
however a real out-of-band channel would deliver it) takes it out at most
once. The stored run travels with the token so the ledger's delivery entry
can carry it even when the request that picks the token up sends no run of
its own. No network code.
"""

from __future__ import annotations

import threading

_lock = threading.Lock()
_pending: dict[str, tuple[str, str]] = {}


def put(ref: str, token: str, run: str) -> None:
    """Store `token` for `ref`, replacing any earlier pending token.

    `run` is the run the issuing action belonged to (possibly "")."""
    with _lock:
        _pending[ref] = (token, run)


def take(ref: str) -> tuple[str, str] | None:
    """Remove and return `(token, run)` pending for `ref`, or None if there isn't one."""
    with _lock:
        return _pending.pop(ref, None)


def clear() -> None:
    """Forget every pending token (tests only)."""
    with _lock:
        _pending.clear()


__all__ = ["clear", "put", "take"]
