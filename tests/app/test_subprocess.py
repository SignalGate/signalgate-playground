"""Launches the sample app exactly the way the console does, as a real
subprocess bound to a real socket, and proves that the launch flags make
`request.client.host` inside a route handler equal the `X-Forwarded-For`
value a caller sends. This can't be shown through an in-process TestClient,
because that never goes through uvicorn's own proxy-header handling.

The proof reads back the app's own ledger, which it echoes to stdout as it
runs, rather than adding a debug-only route to the app.
"""

from __future__ import annotations

import json
import os
import secrets
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from tests.contract.samples import HEALTH, INBOX, assert_same_shape

REPO_ROOT = Path(__file__).resolve().parents[2]
CONTROL_TOKEN = "subprocess-test-token"
START_TIMEOUT_S = 10.0
HEALTH_POLL_INTERVAL_S = 0.05
FORWARDED_FOR = "198.51.100.7"  # RFC 5737 documentation range


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _request(url: str, headers: dict[str, str], payload: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    method = "POST" if payload is not None else "GET"
    req = urllib.request.Request(url, data=data, method=method, headers=dict(headers))
    if payload is not None:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read())


def test_client_host_matches_forwarded_for_header():
    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    env = {**os.environ, "PLAYGROUND_CONTROL_TOKEN": CONTROL_TOKEN}

    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "app.main:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--proxy-headers",
            "--forwarded-allow-ips",
            "127.0.0.1",
            "--no-access-log",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        auth = {"Authorization": f"Bearer {CONTROL_TOKEN}"}
        deadline = time.monotonic() + START_TIMEOUT_S
        health = None
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            try:
                status, body = _request(f"{base_url}/__playground/health", auth)
                if status == 200:
                    health = body
                    break
            except OSError:
                pass
            time.sleep(HEALTH_POLL_INTERVAL_S)
        assert health is not None, "the sample app did not become healthy in time"
        assert_same_shape(health, HEALTH)

        ref = f"ref-{secrets.token_hex(8)}"
        user = f"user-{secrets.token_hex(6)}"

        status, body = _request(
            f"{base_url}/api/action",
            {"X-Playground-Ref": ref, "X-Forwarded-For": FORWARDED_FOR},
            {"user": user},
        )
        assert status == 200
        assert body == {"ok": True}

        status, body = _request(f"{base_url}/__playground/inbox?ref={ref}", auth)
        assert status == 200
        assert_same_shape(body, INBOX)
        assert ref in body["tokens"]
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=START_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()

    entries = [
        json.loads(line)["ledger"] for line in output.splitlines() if line.startswith('{"ledger"')
    ]
    action_entries = [entry for entry in entries if entry.get("ref") == ref and "user" in entry]
    assert action_entries, "no action ledger entry for this ref appeared on stdout"
    assert action_entries[-1]["client_ip"] == FORWARDED_FOR
