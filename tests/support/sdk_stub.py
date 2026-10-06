"""A test-only stand-in for SignalGate at the HTTP layer the SDK uses.

``install_sdk_stub`` replaces ``requests.adapters.HTTPAdapter.send`` for the
whole class, so every SDK client in the process -- a plain ``Client(key)``
and one built on the kit transport alike -- gets its answers from a test's
``responder`` instead of the network. No socket is opened. Requests are
routed only by the suffix of their URL path; the host is never looked at.

In forward mode (``install_sdk_stub(monkeypatch, forward_to(app))``) every
check and log goes on, in process, to an ASGI app the test controls, such
as the fake server in ``tests/fake_server``, and its answer comes back to
the SDK. Still no socket, and still no base URL: only the path suffix picks
the route.

``forbid_sockets`` makes any socket connect fail, so a test can prove that
nothing reached the network.

Nothing in the product can reach this module: it lives under ``tests/`` and
is switched on only by a test calling it.
"""

from __future__ import annotations

import json
import socket
import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit

import pytest
import requests
from fastapi.testclient import TestClient
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

Endpoint = Literal["check", "log", "other"]
RaiseKind = Literal["read_timeout", "connect_timeout", "connect"]

CHECK_SUFFIX = "/v0/check"
LOG_SUFFIX = "/v0/log"


@dataclass(frozen=True)
class Reply:
    """One scripted answer.

    ``body`` is sent as JSON when it is a dict or a list, as-is when it is
    bytes, UTF-8 encoded when it is a str, and empty when it is None.
    ``raise_`` makes the request fail the way ``requests`` fails instead of
    answering. ``delay_s`` holds the answer (or the failure) back on the
    calling thread. ``request_id`` is echoed as ``X-Request-Id`` unless it
    is None.
    """

    status: int = 200
    body: dict[str, Any] | list[Any] | bytes | str | None = None
    headers: Mapping[str, str] | None = None
    raise_: RaiseKind | None = None
    delay_s: float = 0.0
    request_id: str | None = "req-fixed"


# What a responder receives for each request: (endpoint, headers, body bytes).
Responder = Callable[[str, dict[str, str], bytes], Reply]


@dataclass
class StubLog:
    """Every request the stub answered, in order: ``(endpoint, headers, body_bytes)``."""

    requests: list[tuple[str, dict[str, str], bytes]] = field(default_factory=list)
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def _add(self, endpoint: str, headers: dict[str, str], body: bytes) -> None:
        with self._lock:
            self.requests.append((endpoint, headers, body))

    def count(self, endpoint: str | None = None) -> int:
        """How many requests reached the stub, for one endpoint or all."""
        with self._lock:
            return sum(1 for entry in self.requests if endpoint in (None, entry[0]))

    def bodies(self, endpoint: str) -> list[bytes]:
        """The request bodies sent to ``endpoint``, in order."""
        with self._lock:
            return [body for name, _, body in self.requests if name == endpoint]

    def headers(self, endpoint: str) -> list[dict[str, str]]:
        """The request headers sent to ``endpoint``, in order."""
        with self._lock:
            return [dict(headers) for name, headers, _ in self.requests if name == endpoint]

    def clear(self) -> None:
        with self._lock:
            self.requests.clear()


def endpoint_for(url: str) -> Endpoint:
    """``"check"``, ``"log"`` or ``"other"``, from the URL path's suffix only."""
    path = urlsplit(url).path
    if path.endswith(CHECK_SUFFIX):
        return "check"
    if path.endswith(LOG_SUFFIX):
        return "log"
    return "other"


def sequence(*replies: Reply) -> Responder:
    """A responder that gives ``replies`` in order, then repeats the last one.

    Thread-safe, so the SDK's background log worker can share it.
    """
    if not replies:
        raise ValueError("sequence() needs at least one reply")
    remaining = list(replies)
    lock = threading.Lock()

    def respond(endpoint: str, headers: dict[str, str], body: bytes) -> Reply:
        with lock:
            return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    return respond


def by_endpoint(replies: Mapping[str, Reply | Responder]) -> Responder:
    """A responder that picks a reply (or a nested responder) by endpoint.

    An endpoint missing from ``replies`` fails like a refused connection.
    """

    def respond(endpoint: str, headers: dict[str, str], body: bytes) -> Reply:
        chosen = replies.get(endpoint)
        if chosen is None:
            return Reply(raise_="connect")
        if isinstance(chosen, Reply):
            return chosen
        return chosen(endpoint, headers, body)

    return respond


def check_verdict(action: str = "allow", *, request_id: str = "req-fixed") -> Reply:
    """A 2xx ``/v0/check`` answer carrying ``action``."""
    return Reply(
        body={
            "ok": True,
            "request_id": request_id,
            "error": None,
            "data": {"action": action, "request_id": request_id, "processing_time_us": 0},
        },
        request_id=request_id,
    )


def log_accepted(*, request_id: str = "req-fixed") -> Reply:
    """A 2xx ``/v0/log`` answer."""
    return Reply(
        body={"ok": True, "request_id": request_id, "error": None, "data": None},
        request_id=request_id,
    )


def error_reply(status: int, code: str = "TEST_CODE", *, request_id: str = "req-fixed") -> Reply:
    """A non-2xx answer in the error envelope shape, with a neutral test code."""
    return Reply(
        status=status,
        body={
            "ok": False,
            "request_id": request_id,
            "error": {"code": code, "message": "test error", "request_id": request_id},
            "data": None,
        },
        request_id=request_id,
    )


# Headers the in-process client sets for itself when forwarding.
_NOT_FORWARDED = frozenset({"host", "content-length", "connection", "accept-encoding"})
# Answer headers handed back to the SDK besides X-Request-Id.
_FORWARDED_BACK = ("Content-Type", "Retry-After")


def forward_to(app: Any) -> Responder:
    """Forward mode: a responder that hands every check and log to ``app``.

    ``app`` is an ASGI app. A check goes to its ``POST /v0/check`` and a log
    to its ``POST /v0/log``, with the SDK's own headers and body bytes; the
    app's status, body and ``X-Request-Id`` go back to the SDK unchanged. Any
    other request fails like a refused connection. The app runs in process
    (``fastapi.testclient.TestClient``), so no socket is opened, and an
    exception inside it comes back as a 500. Thread-safe: the SDK's
    background log worker and the app's request threads can share it.
    """
    client = TestClient(app, raise_server_exceptions=False)
    paths = {"check": CHECK_SUFFIX, "log": LOG_SUFFIX}

    def respond(endpoint: str, headers: dict[str, str], body: bytes) -> Reply:
        path = paths.get(endpoint)
        if path is None:
            return Reply(raise_="connect")
        forwarded = {k: v for k, v in headers.items() if k.lower() not in _NOT_FORWARDED}
        answer = client.post(path, content=body, headers=forwarded)
        back = {name: answer.headers[name] for name in _FORWARDED_BACK if name in answer.headers}
        return Reply(
            status=answer.status_code,
            body=answer.content,
            headers=back,
            request_id=answer.headers.get("X-Request-Id"),
        )

    return respond


def _body_bytes(body: Any) -> bytes:
    if body is None:
        return b""
    if isinstance(body, bytes):
        return body
    if isinstance(body, str):
        return body.encode("utf-8")
    return str(body).encode("utf-8")


def _encode_reply(reply: Reply) -> tuple[bytes, dict[str, str]]:
    headers: dict[str, str] = {}
    if isinstance(reply.body, (dict, list)):
        content = json.dumps(reply.body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    elif isinstance(reply.body, bytes):
        content = reply.body
    elif isinstance(reply.body, str):
        content = reply.body.encode("utf-8")
        headers["Content-Type"] = "text/plain; charset=utf-8"
    else:
        content = b""
    if reply.request_id is not None:
        headers["X-Request-Id"] = reply.request_id
    headers.update(reply.headers or {})
    return content, headers


def _as_responder(responder: Reply | Responder) -> Responder:
    if isinstance(responder, Reply):
        fixed = responder
        return lambda endpoint, headers, body: fixed
    return responder


def install_sdk_stub(monkeypatch: pytest.MonkeyPatch, responder: Reply | Responder) -> StubLog:
    """Answer every ``requests`` call in this test from ``responder``.

    ``responder`` is a ``Reply`` given to every request, or a callable
    ``(endpoint, headers, body_bytes) -> Reply``. Returns the ``StubLog``
    that records each request. ``monkeypatch`` undoes the patch after the
    test.
    """
    respond = _as_responder(responder)
    log = StubLog()

    def send(adapter: HTTPAdapter, request: requests.PreparedRequest, *args: Any, **kwargs: Any):
        url = request.url or ""
        endpoint = endpoint_for(url)
        headers = dict(request.headers)
        body = _body_bytes(request.body)
        log._add(endpoint, headers, body)
        reply = respond(endpoint, headers, body)
        if reply.delay_s:
            time.sleep(reply.delay_s)
        if reply.raise_ == "read_timeout":
            raise requests.exceptions.ReadTimeout("stubbed read timeout", request=request)
        if reply.raise_ == "connect_timeout":
            raise requests.exceptions.ConnectTimeout("stubbed connect timeout", request=request)
        if reply.raise_ == "connect":
            raise requests.exceptions.ConnectionError("stubbed connection error", request=request)
        content, reply_headers = _encode_reply(reply)
        response = requests.Response()
        response.status_code = reply.status
        response._content = content
        response.headers = CaseInsensitiveDict(reply_headers)
        response.encoding = "utf-8"
        response.url = url
        response.request = request
        response.reason = "stubbed"
        response.connection = adapter
        return response

    monkeypatch.setattr(HTTPAdapter, "send", send)
    return log


class SocketBlocked(OSError):
    """Raised by a socket connect while ``forbid_sockets`` is active."""


@dataclass
class SocketGuard:
    """The addresses a test tried to connect to while sockets were forbidden."""

    attempts: list[Any] = field(default_factory=list)


def forbid_sockets(monkeypatch: pytest.MonkeyPatch) -> SocketGuard:
    """Make every ``socket.connect`` / ``connect_ex`` in this test fail.

    Returns a ``SocketGuard`` whose ``attempts`` lists every address tried.
    """
    guard = SocketGuard()

    def refuse(sock: socket.socket, address: Any, *args: Any) -> Any:
        guard.attempts.append(address)
        raise SocketBlocked("socket connections are disabled in this test")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)
    return guard


def request_json(body: bytes) -> Any:
    """Parse a recorded request body as JSON."""
    return json.loads(body.decode("utf-8"))


__all__ = [
    "CHECK_SUFFIX",
    "LOG_SUFFIX",
    "Endpoint",
    "Reply",
    "Responder",
    "SocketBlocked",
    "SocketGuard",
    "StubLog",
    "by_endpoint",
    "check_verdict",
    "endpoint_for",
    "error_reply",
    "forbid_sockets",
    "forward_to",
    "install_sdk_stub",
    "log_accepted",
    "request_json",
    "sequence",
]
