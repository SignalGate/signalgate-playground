"""Serve the test-only fake of the playground API on a loopback port, for the end-to-end tests.

The console and the sample app run as subprocesses, so the fake has to be
reachable over loopback: uvicorn serves it from a thread of the test
process, and the start-up hook (``_sitecustomize.py``) points the console's
API client and the app's SDK at it. The test keeps the ``FakeServer`` in
hand, so it can steer the fake's script and read back what it received.
"""

from __future__ import annotations

import socket
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn

from playground.api.key import KEY_PREFIX
from playground.traffic.local_constants import (
    APP_HEALTH_POLL_S,
    APP_START_TIMEOUT_S,
    APP_STOP_GRACE_S,
)
from tests.fake_server import FakeServer

# A key-shaped value for ``app/.env``, built here so no file holds one. It
# only ever reaches the fake, which keeps a digest of it and nothing else.
E2E_KEY = KEY_PREFIX + "e2eT" * 8


@contextmanager
def serve_fake(fake: FakeServer) -> Iterator[str]:
    """Serve ``fake`` on a free loopback port until the block ends; yield its base URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    config = uvicorn.Config(fake.app, lifespan="off", log_level="warning", access_log=False)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + APP_START_TIMEOUT_S
        while not server.started:
            assert thread.is_alive() and time.monotonic() < deadline, "the fake didn't start"
            time.sleep(APP_HEALTH_POLL_S)
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(APP_STOP_GRACE_S)
        sock.close()
        assert not thread.is_alive(), "the fake didn't stop"


__all__ = ["E2E_KEY", "serve_fake"]
