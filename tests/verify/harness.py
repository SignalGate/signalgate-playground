"""Serve a fixture ``routes.py`` inside the real sample app, and run a gate against it.

The fixture's router sits in front of the real ``app.main`` (its control
routes, facade, kit transport and ledger), served by uvicorn on a loopback
port in a background thread, with the same proxy-header settings the
console gives the app. Its own event loop is separate from the test's, so
a handler that blocks its loop really stalls the app's other requests.

The SDK's HTTP layer is the test stub in forward mode, pointed at the fake
server, so every check and log the app sends lands in the fake in process.

The code gate reads why a request failed from the app's log. Here the
server's error log goes to a temp file (or one the test names), written the
way the server writes it when the console runs the app.
"""

from __future__ import annotations

import asyncio
import contextlib
import importlib.util
import logging
import socket
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import uvicorn
from fastapi import APIRouter, FastAPI
from fastapi.exceptions import RequestValidationError
from fastapi.testclient import TestClient
from uvicorn.logging import DefaultFormatter

import app.main
from app.kit.context import ContextMiddleware
from app.kit.errors import (
    ActionFailed,
    InvalidUser,
    action_failed_handler,
    invalid_user_handler,
    validation_error_handler,
)
from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    APP_START_TIMEOUT_S,
    APP_STOP_GRACE_S,
)
from playground.verify import AppLink, GateResult, probe_roles, run_code_gate, run_rehearsal
from playground.verify.probes import PROBE_VISITS
from tests.contract.playground_api import PROTOCOL_HEADER, PROTOCOL_VERSION, SESSIONS_PATH
from tests.fake_server import FakeServer

ROOT = Path(__file__).resolve().parents[2]
FIXTURES = Path(__file__).resolve().parent / "fixtures"
CONTROL_TOKEN = "verify-control-token"
DUMMY_KEY = "verify-dummy-value"
# The server's own logger, and the format it writes the app's log in.
SERVER_LOGGER = "uvicorn.error"
SERVER_LOG_FORMAT = "%(levelprefix)s %(message)s"


def load_router(path: Path) -> APIRouter:
    """The ``router`` of the routes file at ``path``, loaded under a fresh module name."""
    name = f"verify_fixture_{path.stem}_{uuid.uuid4().hex[:8]}"
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(name, None)
    return module.router


def _app_with(router: APIRouter) -> FastAPI:
    wrapper = FastAPI()
    wrapper.add_middleware(ContextMiddleware)
    wrapper.include_router(router)
    wrapper.add_exception_handler(RequestValidationError, validation_error_handler)
    wrapper.add_exception_handler(InvalidUser, invalid_user_handler)
    wrapper.add_exception_handler(ActionFailed, action_failed_handler)
    wrapper.mount("/", app.main.app)
    return wrapper


@contextmanager
def _server_log_to(app_log: Path | None) -> Iterator[None]:
    """Copy the server's error log (tracebacks included) to ``app_log`` while the block runs.

    Attached only once the server has started: its logging setup replaces
    any handler set before that.
    """
    if app_log is None:
        yield
        return
    handler = logging.FileHandler(app_log, encoding="utf-8")
    handler.setFormatter(DefaultFormatter(SERVER_LOG_FORMAT, use_colors=False))
    logger = logging.getLogger(SERVER_LOGGER)
    logger.addHandler(handler)
    try:
        yield
    finally:
        logger.removeHandler(handler)
        handler.close()


@contextmanager
def serve(router: APIRouter, app_log: Path | None = None) -> Iterator[AppLink]:
    """Serve ``router`` in the sample app on a loopback port until the block ends.

    With ``app_log``, what the server logs goes to that file too.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(
        _app_with(router),
        lifespan="off",
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
        log_level="warning",
        access_log=False,
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + APP_START_TIMEOUT_S
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "the app didn't start"
            time.sleep(APP_HEALTH_POLL_S)
        with _server_log_to(app_log):
            yield AppLink(f"http://127.0.0.1:{port}", CONTROL_TOKEN)
    finally:
        server.should_exit = True
        thread.join(APP_STOP_GRACE_S)
        sock.close()
        assert not thread.is_alive(), "the app didn't stop"


def api(fake: FakeServer) -> TestClient:
    """The console's side of the fake, with the sample app's key."""
    headers = {PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {DUMMY_KEY}"}
    return TestClient(fake.app, headers=headers)


def open_session(fake: FakeServer, size: int = 40) -> tuple[TestClient, str]:
    client = api(fake)
    body = {"size": size, "client_version": "0.0.1", "consent_version": "consent-test-1"}
    response = client.post(SESSIONS_PATH, json=body)
    assert response.status_code == 201, response.text
    return client, response.json()["session_id"]


def lease(
    fake: FakeServer, phase: str, count: int, *, seq: int = 1, **extra: Any
) -> list[dict[str, Any]]:
    client, sid = open_session(fake, size=max(count, 40))
    body = {"phase": phase, "count": count, "seq": seq, **extra}
    response = client.post(f"{SESSIONS_PATH}/{sid}/leases", json=body)
    assert response.status_code == 200, response.text
    client.delete(f"{SESSIONS_PATH}/{sid}")
    return response.json()["visits"]


def probe_lease(fake: FakeServer, chapter: int) -> list[dict[str, Any]]:
    return lease(fake, "probe", PROBE_VISITS, probe_roles=list(probe_roles(chapter)))


def code_gate(
    path: Path, fake: FakeServer, chapter: int, app_log: Path | None = None
) -> GateResult:
    """Run the code gate for ``chapter`` on the routes file at ``path``.

    The server's log goes to ``app_log``, or to a temp file when None.
    """
    visits = probe_lease(fake, chapter)
    with contextlib.ExitStack() as stack:
        if app_log is None:
            app_log = Path(stack.enter_context(tempfile.TemporaryDirectory())) / "app.log"
        link = stack.enter_context(serve(load_router(path), app_log))
        return asyncio.run(
            run_code_gate(link, visits, chapter=chapter, routes=path, root=ROOT, app_log=app_log)
        )


def rehearsal(path: Path) -> GateResult:
    """Run the rehearsal gate on the routes file at ``path``."""
    with serve(load_router(path)) as link:
        return asyncio.run(run_rehearsal(link, routes=path, root=ROOT))


__all__ = [
    "CONTROL_TOKEN",
    "DUMMY_KEY",
    "FIXTURES",
    "ROOT",
    "api",
    "code_gate",
    "lease",
    "load_router",
    "open_session",
    "probe_lease",
    "rehearsal",
    "serve",
]
