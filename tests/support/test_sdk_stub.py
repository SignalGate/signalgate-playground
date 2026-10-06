"""Smoke tests for the test-only SDK stub: a plain SDK client runs against it,
with every socket forbidden, and sees exactly the scripted answers."""

from __future__ import annotations

import socket
from datetime import datetime, timezone

import pytest
import requests
import signalgate
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from tests.support.sdk_stub import (
    Reply,
    SocketBlocked,
    by_endpoint,
    check_verdict,
    endpoint_for,
    error_reply,
    forbid_sockets,
    forward_to,
    install_sdk_stub,
    log_accepted,
    request_json,
    sequence,
)

DUMMY_KEY = "dummy-test-value"
DUMMY_CIPHERTEXT = "dummy-ciphertext"


def _event(nonce: str = "nonce-stub-test") -> signalgate.Event:
    now = datetime.now(timezone.utc)
    return signalgate.Event(
        user_id="user-stubtest",
        ip="198.51.100.7",
        method="playground.action",
        timestamp=now.isoformat(),
        payload=signalgate.EncryptedPayload(
            encrypted=DUMMY_CIPHERTEXT, timestamp=int(now.timestamp()), nonce=nonce
        ),
    )


def _client(**kwargs) -> signalgate.Client:
    return signalgate.Client(DUMMY_KEY, log_retry_base_ms=0, **kwargs)


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/v0/check", "check"),
        ("http://127.0.0.1:8000/v0/log", "log"),
        ("https://example.test/v0/logs", "other"),
        ("https://example.test/v0/check/extra", "other"),
        ("https://example.test/elsewhere", "other"),
    ],
)
def test_requests_are_routed_by_path_suffix_only(url, expected):
    assert endpoint_for(url) == expected


def test_a_plain_client_check_gets_the_scripted_verdict(monkeypatch):
    guard = forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, by_endpoint({"check": check_verdict("block")}))
    client = _client()
    try:
        result = client.check(_event())
    finally:
        client.close()
    assert result.action == "block"
    assert result.failed_open is False
    assert result.request_id == "req-fixed"
    assert log.count() == log.count("check") == 1
    assert request_json(log.bodies("check")[0])["method"] == "playground.action"
    assert guard.attempts == []


def test_a_plain_client_log_is_delivered_once(monkeypatch):
    forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, by_endpoint({"log": log_accepted()}))
    client = _client()
    client.log(_event())
    client.close()
    snapshot = client.metrics.snapshot_flat()
    assert snapshot["log_enqueued_total"] == snapshot["log_sent_total"] == 1
    assert log.count("log") == 1
    assert "Idempotency-Key" in log.headers("log")[0]


def test_an_error_envelope_becomes_a_server_error(monkeypatch):
    install_sdk_stub(monkeypatch, error_reply(422))
    client = _client()
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            client.check(_event())
    finally:
        client.close()
    assert caught.value.status_code == 422
    assert caught.value.code == "TEST_CODE"
    assert caught.value.request_id == "req-fixed"


@pytest.mark.parametrize(
    ("kind", "error"),
    [
        ("read_timeout", signalgate.TimeoutError),
        ("connect_timeout", signalgate.TimeoutError),
        ("connect", signalgate.NetworkError),
    ],
)
def test_scripted_failures_reach_the_sdk_as_its_own_errors(monkeypatch, kind, error):
    install_sdk_stub(monkeypatch, Reply(raise_=kind))
    client = _client(fail_open=False)
    try:
        with pytest.raises(error):
            client.check(_event())
    finally:
        client.close()


def test_a_sequence_gives_each_reply_in_turn_then_repeats_the_last(monkeypatch):
    log = install_sdk_stub(
        monkeypatch, sequence(Reply(raise_="read_timeout"), error_reply(422), log_accepted())
    )
    client = _client()
    client.log(_event())
    client.log(_event(nonce="nonce-second"))
    client.close()
    snapshot = client.metrics.snapshot_flat()
    # The first log times out, then gets a 4xx and is dropped; the second is sent.
    assert snapshot['log_http_error_total{status="network"}'] == 1
    assert snapshot['log_http_error_total{status="422"}'] == 1
    assert snapshot['log_dropped_total{reason="retry_exhausted"}'] == 1
    assert snapshot["log_sent_total"] == 1
    first, retry, second = log.requests
    assert first[1]["Idempotency-Key"] == retry[1]["Idempotency-Key"]
    assert second[1]["Idempotency-Key"] != first[1]["Idempotency-Key"]


def test_bodies_of_every_kind_are_answered_as_given(monkeypatch):
    install_sdk_stub(
        monkeypatch,
        by_endpoint(
            {
                "other": sequence(
                    Reply(body=["listed"]),
                    Reply(body=b"raw-bytes"),
                    Reply(body="plain text"),
                    Reply(body=None, request_id=None, headers={"X-Extra": "yes"}),
                )
            }
        ),
    )
    session = requests.Session()
    try:
        url = "http://127.0.0.1/elsewhere"
        assert session.post(url).json() == ["listed"]
        assert session.post(url).content == b"raw-bytes"
        assert session.post(url).text == "plain text"
        last = session.post(url)
    finally:
        session.close()
    assert last.content == b""
    assert "X-Request-Id" not in last.headers
    assert last.headers["X-Extra"] == "yes"


def test_an_endpoint_missing_from_by_endpoint_fails_like_a_refused_connection(monkeypatch):
    install_sdk_stub(monkeypatch, by_endpoint({"log": log_accepted()}))
    client = _client(fail_open=False)
    try:
        with pytest.raises(signalgate.NetworkError):
            client.check(_event())
    finally:
        client.close()


def test_forbid_sockets_blocks_and_records_every_connect(monkeypatch):
    guard = forbid_sockets(monkeypatch)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock, pytest.raises(SocketBlocked):
        sock.connect(("127.0.0.1", 9))
    with pytest.raises(OSError):
        socket.create_connection(("127.0.0.1", 9))
    assert len(guard.attempts) == 2


def test_the_stub_needs_no_socket_even_when_sockets_are_forbidden(monkeypatch):
    guard = forbid_sockets(monkeypatch)
    install_sdk_stub(monkeypatch, check_verdict())
    client = _client()
    try:
        assert client.check(_event()).action == "allow"
    finally:
        client.close()
    assert guard.attempts == []


def _forward_target() -> tuple[FastAPI, list[tuple[str, dict[str, str], bytes]]]:
    """A small ASGI app that records what reaches it and answers like the public API."""
    app = FastAPI()
    seen: list[tuple[str, dict[str, str], bytes]] = []

    @app.post("/v0/check")
    async def check(request: Request) -> JSONResponse:
        seen.append(("check", dict(request.headers), await request.body()))
        data = {"action": "block", "request_id": "req-forwarded", "processing_time_us": 0}
        return JSONResponse(
            {"ok": True, "data": data, "error": None}, headers={"X-Request-Id": "req-forwarded"}
        )

    @app.post("/v0/log")
    async def log(request: Request) -> JSONResponse:
        seen.append(("log", dict(request.headers), await request.body()))
        error = {"code": "TEST_CODE", "message": "test error", "request_id": "req-refused"}
        return JSONResponse({"ok": False, "data": None, "error": error}, status_code=422)

    return app, seen


def test_forward_mode_hands_each_check_and_log_to_the_app(monkeypatch):
    guard = forbid_sockets(monkeypatch)
    app, seen = _forward_target()
    log = install_sdk_stub(monkeypatch, forward_to(app))
    client = _client()
    try:
        result = client.check(_event())
        client.log(_event(nonce="nonce-forwarded-log"))
    finally:
        client.close()
    assert (result.action, result.request_id, result.failed_open) == (
        "block",
        "req-forwarded",
        False,
    )
    assert [endpoint for endpoint, _, _ in seen] == ["check", "log"]
    assert [body for _, _, body in seen] == [body for _, _, body in log.requests]
    check_headers = seen[0][1]
    assert check_headers["authorization"] == f"Bearer {DUMMY_KEY}"
    assert check_headers["idempotency-key"] == log.headers("check")[0]["Idempotency-Key"]
    assert request_json(seen[1][2])["payload"]["nonce"] == "nonce-forwarded-log"
    assert client.metrics.snapshot_flat()['log_http_error_total{status="422"}'] == 1
    assert guard.attempts == []


def test_forward_mode_refuses_anything_but_check_and_log(monkeypatch):
    app, seen = _forward_target()
    install_sdk_stub(monkeypatch, forward_to(app))
    session = requests.Session()
    try:
        with pytest.raises(requests.exceptions.ConnectionError):
            session.post("http://127.0.0.1/v0/other")
    finally:
        session.close()
    assert seen == []
