"""Request-scoped context set by a pure-ASGI middleware.

Three things travel with a request but don't belong in a function signature:
which visitor it belongs to (`ref`), whether the caller asked this request to
fail on purpose, and the address the request arrived from. `ContextMiddleware`
reads them off the incoming headers and scope, and the rest of the app reads
them back through the accessors below.

A request may also say which run it belongs to (`X-Playground-Run`), so what
the app records can be told apart run by run. A value that doesn't match
`RUN_PATTERN` in full counts as no run, and `get_run()` returns "".
"""

from __future__ import annotations

import re
import secrets
from contextvars import ContextVar
from typing import Any

REF_HEADER = "x-playground-ref"
ACTION_FAIL_HEADER = "x-playground-action-fail"
RUN_HEADER = "x-playground-run"
# Used with `fullmatch`.
RUN_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")

_ref: ContextVar[str] = ContextVar("playground_ref", default="")
_action_fail: ContextVar[bool] = ContextVar("playground_action_fail", default=False)
_client_ip: ContextVar[str] = ContextVar("playground_client_ip", default="")
_run: ContextVar[str] = ContextVar("playground_run", default="")


def get_ref() -> str:
    """Return the ref for the request currently being handled."""
    return _ref.get()


def action_should_fail() -> bool:
    """Return True if the current request asked to fail the action step."""
    return _action_fail.get()


def get_client_ip() -> str:
    """Return the address ASGI reports for the current request's client."""
    return _client_ip.get()


def get_run() -> str:
    """Return the run id of the request currently being handled, or "" if none."""
    return _run.get()


def _find_header(headers: list[tuple[bytes, bytes]], name: str) -> str | None:
    needle = name.encode("latin-1")
    for key, value in headers:
        if key.lower() == needle:
            return value.decode("latin-1")
    return None


class ContextMiddleware:
    """Pure-ASGI middleware: no base class, no request object, just scope."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = scope.get("headers", [])
        ref = _find_header(headers, REF_HEADER) or secrets.token_hex(8)
        should_fail = _find_header(headers, ACTION_FAIL_HEADER) == "1"
        client = scope.get("client")
        client_ip = client[0] if client else ""
        run_value = _find_header(headers, RUN_HEADER) or ""
        run = run_value if RUN_PATTERN.fullmatch(run_value) else ""

        ref_token = _ref.set(ref)
        fail_token = _action_fail.set(should_fail)
        ip_token = _client_ip.set(client_ip)
        run_token = _run.set(run)
        try:
            await self.app(scope, receive, send)
        finally:
            _ref.reset(ref_token)
            _action_fail.reset(fail_token)
            _client_ip.reset(ip_token)
            _run.reset(run_token)


__all__ = [
    "RUN_HEADER",
    "RUN_PATTERN",
    "ContextMiddleware",
    "action_should_fail",
    "get_client_ip",
    "get_ref",
    "get_run",
]
