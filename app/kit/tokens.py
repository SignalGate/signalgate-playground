"""Single-use confirmation tokens.

`issue` mints a token bound to one user; `verify` redeems it. A token can be
redeemed at most once ever: the first `verify` call for a given token
consumes it, whether or not the user matched, so a wrong guess can't be
retried. Every `verify` call is recorded in the shared ledger.
"""

from __future__ import annotations

import secrets
import threading

from app.kit.context import get_ref, get_run
from app.kit.sequence import ledger

_lock = threading.Lock()
_pending: dict[str, str] = {}


def issue(user: str) -> str:
    """Mint and register a fresh single-use token bound to `user`."""
    value = secrets.token_urlsafe(16)
    with _lock:
        _pending[value] = user
    return value


def verify(user: str, token: str) -> bool:
    """Redeem `token` for `user`. Returns whether it matched a pending,
    unused token issued for this exact `user`."""
    with _lock:
        owner = _pending.pop(token, None)
    ok = owner is not None and owner == user
    ledger.append({"kind": "confirm", "ref": get_ref(), "ok": ok, "run": get_run()})
    return ok


__all__ = ["issue", "verify"]
