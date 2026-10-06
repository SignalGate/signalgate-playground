"""An app that drives a real SignalGate client's background worker, to prove
a close on shutdown leaves no worker thread behind.

This fixture builds one real ``signalgate.Client`` -- a dummy key, and its
HTTP layer stubbed right here (a fixed, slightly delayed local reply, no
socket ever opened; the same idea as ``tests/support/sdk_stub.py``, but
inlined because this module runs as its own process, outside pytest) -- and
stands it in for the app's shared client: ``signalgate_client.close_client``
closes this real client with the app's own close bound, and
``client_state``/``metrics_snapshot`` describe it. Logging a handful of
events at start-up gives the client's own background worker thread real,
in-flight work, so closing it through the real
``POST /__playground/shutdown`` route exercises the same bounded wait the
route always uses.

``GET /fixture/worker-threads`` reports which worker threads are alive, so a
test can see the worker running right before it asks for the shutdown. At
interpreter exit the fixture prints the same list, so the test can tell the
worker stopped without reaching back into the (by-then-gone) process.
"""

from __future__ import annotations

import asyncio
import atexit
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlsplit

import requests
import signalgate
from fastapi import FastAPI
from requests.adapters import HTTPAdapter
from requests.structures import CaseInsensitiveDict

import app.signalgate_client as sg_client
from app.kit import lifecycle
from app.kit.control import router as control_router
from app.kit.local_constants import CLIENT_CLOSE_TIMEOUT_S

DUMMY_KEY = "dummy-key-for-the-sdk-worker-fixture"
WORKER_THREAD_NAME = "signalgate-log-worker"

# Every log reply is a touch slow, so a handful of queued events keep the
# worker busy for a moment rather than draining before shutdown ever asks.
_LOG_REPLY_DELAY_S = CLIENT_CLOSE_TIMEOUT_S / 50
EVENTS_TO_LOG = 10


def _stub_send(adapter: HTTPAdapter, request: requests.PreparedRequest, *args, **kwargs):
    del args, kwargs
    if urlsplit(request.url or "").path.endswith("/v0/log"):
        time.sleep(_LOG_REPLY_DELAY_S)
    response = requests.Response()
    response.status_code = 200
    response._content = b'{"ok": true, "request_id": "req-fixed", "error": null, "data": null}'
    response.headers = CaseInsensitiveDict({"Content-Type": "application/json"})
    response.encoding = "utf-8"
    response.url = request.url or ""
    response.request = request
    response.reason = "stubbed"
    response.connection = adapter
    return response


HTTPAdapter.send = _stub_send

_client = signalgate.Client(DUMMY_KEY)


def _log_a_few() -> None:
    payload = signalgate.EncryptedPayload(encrypted="x", timestamp=0, nonce="n")
    for _ in range(EVENTS_TO_LOG):
        event = signalgate.Event(
            user_id="u",
            ip="127.0.0.1",
            method="playground.action",
            timestamp=datetime.now(timezone.utc).isoformat(),
            payload=payload,
        )
        _client.log(event)


def _worker_threads() -> list[str]:
    return [t.name for t in threading.enumerate() if t.name == WORKER_THREAD_NAME]


def _report_worker_threads_at_exit() -> None:
    print(f"sdk-worker-threads-at-exit: {_worker_threads()}", flush=True)


atexit.register(_report_worker_threads_at_exit)

# The client already exists, so both are true from the start; no lazy build
# to model here.
sg_client.client_state = lambda: (1, True)
sg_client.metrics_snapshot = _client.metrics.snapshot_flat
sg_client.close_client = lambda: _client.close(timeout_s=CLIENT_CLOSE_TIMEOUT_S)


@asynccontextmanager
async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
    # The same order as the app's own lifespan: lifeline first, then work.
    lifecycle.start_lifeline()
    _log_a_few()
    print(f"sdk-worker-threads-after-start-up: {_worker_threads()}", flush=True)
    yield
    await asyncio.to_thread(sg_client.close_client)


app = FastAPI(lifespan=lifespan)
app.include_router(control_router)


@app.get("/fixture/worker-threads")
async def worker_threads() -> dict[str, Any]:
    return {"threads": _worker_threads(), "events_logged": EVENTS_TO_LOG}
