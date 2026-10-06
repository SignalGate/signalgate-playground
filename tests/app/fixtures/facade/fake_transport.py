"""An in-memory stand-in for the kit transport, so `RecordingClient` can be
tested on its own. The facade's tests swap it in for
`app.kit.facade.KitTransport`.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from typing import Any

import signalgate

Responder = Callable[[str, dict[str, Any]], signalgate.HttpResponse]


def allow_responder(url: str, body: dict[str, Any]) -> signalgate.HttpResponse:
    """A 2xx response carrying an allow verdict, good for both check and log URLs."""
    return signalgate.HttpResponse(
        status_code=200,
        body={
            "ok": True,
            "request_id": "req-fake",
            "error": None,
            "data": {"action": "allow", "request_id": "req-fake", "processing_time_us": 0},
        },
        request_id="req-fake",
    )


def raising_responder(exc: BaseException) -> Responder:
    """A responder that raises `exc` on every call, for transparency tests."""

    def respond(url: str, body: dict[str, Any]) -> signalgate.HttpResponse:
        raise exc

    return respond


class FakeTransport:
    """An in-memory stand-in for `KitTransport`. `respond` decides every outcome;
    `delay_s` sleeps on the calling thread first, to exercise timing."""

    def __init__(self, api_key: str | None = None, *, logger: Any = None) -> None:
        self.api_key = api_key
        self.logger = logger
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.respond: Responder = allow_responder
        self.delay_s = 0.0

    def post(
        self,
        url: str,
        body: dict[str, Any],
        *,
        timeout_ms: int,
        request_id: str,
        idempotency_key: str,
    ) -> signalgate.HttpResponse:
        self.calls.append((url, body))
        if self.delay_s:
            time.sleep(self.delay_s)
        return self.respond(url, body)

    def close(self) -> None:
        self.closed = True

    def __repr__(self) -> str:
        return "FakeTransport(api_key=<redacted>)"


__all__ = ["FakeTransport", "Responder", "allow_responder", "raising_responder"]
