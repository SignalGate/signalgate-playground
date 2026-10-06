"""Tests for `sg_event`: builds an `Event` from a request and an envelope,
or returns None without raising."""

from __future__ import annotations

import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

import pytest
from signalgate import EncryptedPayload

from app.signalgate_client import sg_event
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_START_TIMEOUT_S

REPO_ROOT = Path(__file__).resolve().parents[2]
# How long to wait for the fixture app to answer, and how often to poll: the
# same pacing the console itself uses to wait for the sample app on your
# laptop.
START_TIMEOUT_S = APP_START_TIMEOUT_S
POLL_INTERVAL_S = APP_HEALTH_POLL_S
FORWARDED_FOR = "203.0.113.77"  # RFC 5737 documentation range

VALID_ENVELOPE = {"encrypted": "cipher-text", "nonce": "nonce-1", "timestamp": 1_700_000_000}


class _FakeClient:
    def __init__(self, host: str) -> None:
        self.host = host


class _FakeRequest:
    def __init__(self, client_host: str | None = "198.51.100.1") -> None:
        self.client = _FakeClient(client_host) if client_host is not None else None


class _RaisingRequest:
    """A request whose `.client` blows up when read."""

    @property
    def client(self):
        raise RuntimeError("boom")


# --- returns None, never raises -----------------------------------------------


@pytest.mark.parametrize(
    "envelope",
    [
        pytest.param(None, id="none"),
        pytest.param([], id="list"),
        pytest.param("x", id="str"),
        pytest.param({}, id="empty_dict"),
        pytest.param({"encrypted": 1, "nonce": "n", "timestamp": 1}, id="encrypted_not_str"),
        pytest.param({"encrypted": "c", "timestamp": 1}, id="nonce_missing"),
        pytest.param({"encrypted": "c", "nonce": 5, "timestamp": 1}, id="nonce_not_str"),
        pytest.param({"encrypted": "c", "nonce": "n"}, id="timestamp_missing"),
        pytest.param({"encrypted": "c", "nonce": "n", "timestamp": 1.5}, id="timestamp_float"),
        pytest.param({"encrypted": "c", "nonce": "n", "timestamp": "1"}, id="timestamp_str"),
        pytest.param({"encrypted": "c", "nonce": "n", "timestamp": True}, id="timestamp_bool_true"),
        pytest.param(
            {"encrypted": "c", "nonce": "n", "timestamp": False}, id="timestamp_bool_false"
        ),
    ],
)
def test_returns_none_for_a_malformed_envelope(envelope):
    assert sg_event(_FakeRequest(), "user", envelope, "playground.action") is None


def test_returns_none_when_the_request_has_no_client():
    assert (
        sg_event(_FakeRequest(client_host=None), "user", VALID_ENVELOPE, "playground.action")
        is None
    )


def test_returns_none_when_reading_the_request_raises():
    assert sg_event(_RaisingRequest(), "user", VALID_ENVELOPE, "playground.action") is None


def test_v_is_never_validated_only_passed_through():
    # `v` is passed through unchanged: sg_event never inspects or validates
    # it, however odd a value it holds.
    marker = object()
    envelope = {**VALID_ENVELOPE, "v": marker}
    ev = sg_event(_FakeRequest(), "user", envelope, "playground.action")
    assert ev is not None
    assert ev.payload.v is marker


# --- builds the Event correctly -------------------------------------------------


def test_builds_the_event_from_a_valid_envelope():
    request = _FakeRequest(client_host="203.0.113.5")
    envelope = {**VALID_ENVELOPE, "v": 2, "unexpected": "ignored"}
    before = datetime.now(timezone.utc)

    ev = sg_event(request, "user-42", envelope, "playground.action", custom={"a": 1})

    after = datetime.now(timezone.utc)
    assert ev is not None
    assert ev.user_id == "user-42"
    assert ev.ip == "203.0.113.5"
    assert ev.method == "playground.action"
    assert ev.custom == {"a": 1}
    assert ev.payload == EncryptedPayload(
        encrypted="cipher-text", timestamp=1_700_000_000, nonce="nonce-1", v=2
    )

    parsed = datetime.fromisoformat(ev.timestamp)
    assert parsed.utcoffset() is not None
    assert abs((after - parsed).total_seconds()) < 5
    assert abs((parsed - before).total_seconds()) < 5


def test_custom_defaults_to_none():
    ev = sg_event(_FakeRequest(), "user", VALID_ENVELOPE, "playground.action")
    assert ev is not None
    assert ev.custom is None


def test_to_wire_keeps_v_when_present_and_omits_it_when_absent():
    request = _FakeRequest()
    with_v = sg_event(request, "u", {**VALID_ENVELOPE, "v": 2}, "playground.action")
    without_v = sg_event(request, "u", VALID_ENVELOPE, "playground.action")

    assert with_v is not None and without_v is not None
    assert with_v.payload.to_wire()["v"] == 2
    assert "v" not in without_v.payload.to_wire()


def test_the_timestamp_round_trips_through_fromisoformat_on_this_interpreter():
    # The offset form below must parse with `fromisoformat` on Python 3.10,
    # not only on newer interpreters.
    ev = sg_event(_FakeRequest(), "user", VALID_ENVELOPE, "playground.action")
    assert ev is not None
    reparsed = datetime.fromisoformat(ev.timestamp)
    assert reparsed.tzinfo is not None


# --- ip comes from request.client.host, resolved by uvicorn's own proxy-header
# handling, never a manual X-Forwarded-For parse ---------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.mark.e2e
def test_ip_matches_the_forwarded_for_header_once_uvicorn_rewrites_it():
    port = _free_port()
    url = f"http://127.0.0.1:{port}/echo-ip"
    process = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "tests.app.fixtures.facade.ip_probe:app",
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
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    body = None
    try:
        deadline = time.monotonic() + START_TIMEOUT_S
        while time.monotonic() < deadline:
            if process.poll() is not None:
                break
            request = urllib.request.Request(
                url, data=b"", method="POST", headers={"X-Forwarded-For": FORWARDED_FOR}
            )
            try:
                with urllib.request.urlopen(request) as response:
                    body = json.loads(response.read())
                    break
            except (urllib.error.URLError, OSError):
                time.sleep(POLL_INTERVAL_S)
    finally:
        process.terminate()
        try:
            output, _ = process.communicate(timeout=START_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            process.kill()
            output, _ = process.communicate()

    assert body is not None, f"the fixture app never answered:\n{output}"
    assert body == {"ip": FORWARDED_FOR}
