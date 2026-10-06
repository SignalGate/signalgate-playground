"""The parity test: a plain ``Client(key)`` and a
``Client(key, http=KitTransport(key))`` must behave identically from the
caller's side, for every combination of network outcome, endpoint, fail-open
setting and playground method.

The kit only ever adds a guard, a canary branch and recording; none of the
scenarios below touch either, so every difference the matrix could reveal
would be a parity bug. Every attempt record the kit emits here is checked
against the pinned attempt shape after each test.
"""

from __future__ import annotations

import pytest
import signalgate

from app.kit import facade
from app.kit import kit_transport as kit_transport_module
from app.kit.calls import calls
from app.kit.kit_transport import KitTransport
from app.kit.local_constants import CANARY_DELAY_MAX_MS
from tests.contract.samples import ATTEMPT_RECORD, assert_same_shape
from tests.support.sdk_stub import (
    Reply,
    check_verdict,
    error_reply,
    forbid_sockets,
    install_sdk_stub,
    log_accepted,
)

DUMMY_KEY = "dummy-test-value"
DUMMY_CIPHERTEXT = "dummy-ciphertext-value"

REPLY_KINDS = (
    "success",
    "client_error",
    "server_error",
    "malformed_non_json",
    "malformed_list",
    "read_timeout",
    "connect_timeout",
    "connection_error",
)
ENDPOINTS = ("check", "log")
FAIL_OPEN_VALUES = (True, False)
METHODS_UNDER_TEST = kit_transport_module.METHODS

_VOLATILE_HEADERS = ("X-Request-Id", "Idempotency-Key")

# A timeout used only as a pass-through value below, never actually waited
# on: derived from the app's own canary delay cap, not a fresh guess.
_PASSTHROUGH_TIMEOUT_MS = CANARY_DELAY_MAX_MS // 2


@pytest.fixture(autouse=True)
def _construction_hook_active():
    # Every client this test builds, kit-wrapped or bare, is expected to pass
    # through the same construction hook in the integrated app. Installing it
    # here proves the hook never changes what this test observes.
    facade.install_client_hook()


def _all_records() -> list[dict]:
    """Every stored record, read page by page."""
    records: list[dict] = []
    cursor = 0
    while True:
        page, cursor, more, *_ = calls.read_page(cursor)
        records.extend(page)
        if not more:
            return records


def _attempts() -> list[dict]:
    return [r for r in _all_records() if r["kind"] == "attempt"]


@pytest.fixture(autouse=True)
def _every_attempt_record_has_the_pinned_shape(_reset_kit_state):
    # Requesting the shared reset fixture makes this teardown run first,
    # while this test's records are still stored.
    yield
    assert calls.record_errors == 0, "a record could not be stored, so its shape went unchecked"
    for attempt in _attempts():
        assert_same_shape(attempt, ATTEMPT_RECORD)


def _event(method: str, nonce: str = "nonce-parity") -> signalgate.Event:
    return signalgate.Event(
        user_id="user-parity",
        ip="198.51.100.7",
        method=method,
        timestamp="2026-01-02T03:04:05+00:00",
        payload=signalgate.EncryptedPayload(
            encrypted=DUMMY_CIPHERTEXT, timestamp=1_700_000_000, nonce=nonce
        ),
    )


def _reply(kind: str, endpoint: str) -> Reply:
    if kind == "success":
        return check_verdict("allow") if endpoint == "check" else log_accepted()
    if kind == "client_error":
        return error_reply(400)
    if kind == "server_error":
        return error_reply(500)
    if kind == "malformed_non_json":
        return Reply(status=200, body="not-json-at-all")
    if kind == "malformed_list":
        return Reply(status=200, body=["not", "a", "dict"])
    if kind == "read_timeout":
        return Reply(raise_="read_timeout")
    if kind == "connect_timeout":
        return Reply(raise_="connect_timeout")
    if kind == "connection_error":
        return Reply(raise_="connect")
    raise AssertionError(f"unknown reply kind: {kind}")  # pragma: no cover


def _check_outcome(client: signalgate.Client, method: str):
    try:
        return ("result", client.check(_event(method)))
    except signalgate.SignalGateError as exc:
        return (
            "raised",
            type(exc),
            getattr(exc, "status_code", None),
            getattr(exc, "code", None),
            getattr(exc, "request_id", None),
            str(exc),
        )


def _drive_check(transport, method: str, fail_open: bool, monkeypatch, reply: Reply):
    log = install_sdk_stub(monkeypatch, reply)
    client = signalgate.Client(DUMMY_KEY, http=transport, fail_open=fail_open, log_retry_base_ms=0)
    try:
        outcome = _check_outcome(client, method)
    finally:
        client.close()
    return outcome, client.metrics.snapshot_flat(), log


def _drive_log(transport, method: str, fail_open: bool, monkeypatch, reply: Reply):
    log = install_sdk_stub(monkeypatch, reply)
    client = signalgate.Client(DUMMY_KEY, http=transport, fail_open=fail_open, log_retry_base_ms=0)
    client.log(_event(method))
    client.close()
    return client.metrics.snapshot_flat(), log


def _stripped_headers(headers_list: list[dict[str, str]]) -> list[dict[str, str]]:
    return [
        {k: v for k, v in headers.items() if k not in _VOLATILE_HEADERS} for headers in headers_list
    ]


@pytest.mark.parametrize("method", METHODS_UNDER_TEST)
@pytest.mark.parametrize("fail_open", FAIL_OPEN_VALUES)
@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("kind", REPLY_KINDS)
def test_kit_and_bare_client_are_equal(monkeypatch, kind, endpoint, fail_open, method):
    guard = forbid_sockets(monkeypatch)
    reply = _reply(kind, endpoint)

    if endpoint == "check":
        kit_outcome, kit_snapshot, kit_log = _drive_check(
            KitTransport(DUMMY_KEY), method, fail_open, monkeypatch, reply
        )
        bare_outcome, bare_snapshot, bare_log = _drive_check(
            None, method, fail_open, monkeypatch, reply
        )
        assert kit_outcome == bare_outcome
    else:
        kit_snapshot, kit_log = _drive_log(
            KitTransport(DUMMY_KEY), method, fail_open, monkeypatch, reply
        )
        bare_snapshot, bare_log = _drive_log(None, method, fail_open, monkeypatch, reply)

    assert kit_snapshot == bare_snapshot

    kit_bodies, bare_bodies = kit_log.bodies(endpoint), bare_log.bodies(endpoint)
    assert len(kit_bodies) == len(bare_bodies) > 0
    assert kit_bodies == bare_bodies

    assert _stripped_headers(kit_log.headers(endpoint)) == _stripped_headers(
        bare_log.headers(endpoint)
    )
    assert guard.attempts == []

    # Only the kit records attempts: exactly one per request it sent, each
    # forwarded (never answered locally) and on the endpoint it went to.
    attempts = _attempts()
    assert len(attempts) == kit_log.count(endpoint) == kit_log.count()
    assert all(a["endpoint"] == endpoint and a["local"] is None for a in attempts)
    assert all(a["method"] == method for a in attempts)


@pytest.mark.parametrize("method", METHODS_UNDER_TEST)
@pytest.mark.parametrize("fail_open", FAIL_OPEN_VALUES)
@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_construction_hook_stays_active_throughout_the_matrix(
    monkeypatch, endpoint, fail_open, method
):
    """Every client built anywhere in this matrix -- kit-wrapped or bare --
    goes through the same construction hook, and it stays installed for the
    whole run, not just at the first construction. The reply kind plays no
    part in whether the hook stays installed, so this mirrors the main
    matrix's other three axes rather than all four."""
    forbid_sockets(monkeypatch)
    assert facade.client_hook_installed() is True
    reply = check_verdict("allow") if endpoint == "check" else log_accepted()
    if endpoint == "check":
        _drive_check(KitTransport(DUMMY_KEY), method, fail_open, monkeypatch, reply)
        _drive_check(None, method, fail_open, monkeypatch, reply)
    else:
        _drive_log(KitTransport(DUMMY_KEY), method, fail_open, monkeypatch, reply)
        _drive_log(None, method, fail_open, monkeypatch, reply)
    assert facade.client_hook_installed() is True


def test_the_user_agent_header_matches_a_bare_clients_exactly(monkeypatch):
    forbid_sockets(monkeypatch)
    kit_log = install_sdk_stub(monkeypatch, check_verdict())
    kit_client = signalgate.Client(DUMMY_KEY, http=KitTransport(DUMMY_KEY))
    kit_client.check(_event("playground.action"))
    kit_client.close()

    bare_log = install_sdk_stub(monkeypatch, check_verdict())
    bare_client = signalgate.Client(DUMMY_KEY)
    bare_client.check(_event("playground.action"))
    bare_client.close()

    kit_agent = kit_log.headers("check")[0]["User-Agent"]
    bare_agent = bare_log.headers("check")[0]["User-Agent"]
    assert kit_agent == bare_agent == kit_transport_module.user_agent()


def test_exactly_one_stub_call_per_sdk_attempt_on_a_retried_log(monkeypatch):
    forbid_sockets(monkeypatch)
    reply = error_reply(500)
    kit_snapshot, kit_log = _drive_log(
        KitTransport(DUMMY_KEY), "playground.action", True, monkeypatch, reply
    )
    bare_snapshot, bare_log = _drive_log(None, "playground.action", True, monkeypatch, reply)
    # One initial attempt plus every retry, exactly as the plain client made.
    assert kit_log.count("log") == bare_log.count("log") > 1
    assert kit_snapshot == bare_snapshot


# --- unchanged forwarding: no retry, dedup, cache or rewrite ------------------------------


class _RecordingInner:
    """A minimal fake standing in for the SDK's own HTTP transport, so a test
    can see exactly what ``KitTransport`` forwards to it."""

    def __init__(self, reply: signalgate.HttpResponse) -> None:
        self._reply = reply
        self.calls: list[dict[str, object]] = []

    def post(self, url, body, *, timeout_ms, request_id, idempotency_key):
        self.calls.append(
            {
                "url": url,
                "body": body,
                "timeout_ms": timeout_ms,
                "request_id": request_id,
                "idempotency_key": idempotency_key,
            }
        )
        return self._reply

    def close(self) -> None:
        pass


def test_forward_passes_the_same_url_body_and_metadata_through_unchanged():
    reply = signalgate.HttpResponse(
        200, {"ok": True, "request_id": "req-fwd", "error": None, "data": None}, "req-fwd"
    )
    fake_inner = _RecordingInner(reply)
    transport = KitTransport(DUMMY_KEY)
    transport._inner = fake_inner
    body = {
        "user_id": "user-parity",
        "ip": "198.51.100.7",
        "method": "playground.action",
        "timestamp": "2026-01-02T03:04:05+00:00",
        "payload": {
            "encrypted": DUMMY_CIPHERTEXT,
            "timestamp": 1_700_000_000,
            "nonce": "nonce-forward",
        },
    }
    try:
        transport.post(
            "https://example.test/v0/log",
            body,
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-forward",
            idempotency_key="idem-forward",
        )
    finally:
        transport.close()
    assert len(fake_inner.calls) == 1
    forwarded = fake_inner.calls[0]
    assert forwarded["url"] == "https://example.test/v0/log"
    assert forwarded["body"] is body
    assert forwarded["timeout_ms"] == _PASSTHROUGH_TIMEOUT_MS
    assert forwarded["request_id"] == "req-forward"
    assert forwarded["idempotency_key"] == "idem-forward"


def test_the_forwarded_headers_carry_the_exact_request_id_and_idempotency_key(monkeypatch):
    forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, log_accepted())
    transport = KitTransport(DUMMY_KEY)
    body = {
        "user_id": "user-parity",
        "ip": "198.51.100.7",
        "method": "playground.action",
        "timestamp": "2026-01-02T03:04:05+00:00",
        "payload": {
            "encrypted": DUMMY_CIPHERTEXT,
            "timestamp": 1_700_000_000,
            "nonce": "nonce-headers",
        },
    }
    try:
        transport.post(
            "https://example.test/v0/log",
            body,
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-exact",
            idempotency_key="idem-exact",
        )
    finally:
        transport.close()
    headers = log.headers("log")[0]
    assert headers["X-Request-Id"] == "req-exact"
    assert headers["Idempotency-Key"] == "idem-exact"


def test_log_retries_share_the_same_idempotency_key(monkeypatch):
    forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, error_reply(500))
    client = signalgate.Client(
        DUMMY_KEY, http=KitTransport(DUMMY_KEY), fail_open=True, log_retry_base_ms=0
    )
    client.log(_event("playground.action"))
    client.close()
    headers = log.headers("log")
    assert len(headers) > 1
    idempotency_keys = {h["Idempotency-Key"] for h in headers}
    assert len(idempotency_keys) == 1


def test_a_reused_nonce_is_forwarded_every_time_with_no_dedup_or_cache(monkeypatch):
    forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, check_verdict("allow"))
    transport = KitTransport(DUMMY_KEY)
    body = {
        "user_id": "user-parity",
        "ip": "198.51.100.7",
        "method": "playground.action",
        "timestamp": "2026-01-02T03:04:05+00:00",
        "payload": {
            "encrypted": DUMMY_CIPHERTEXT,
            "timestamp": 1_700_000_000,
            "nonce": "nonce-reused",
        },
    }
    try:
        transport.post(
            "https://example.test/v0/check",
            body,
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-reused-1",
            idempotency_key="idem-reused-1",
        )
        transport.post(
            "https://example.test/v0/check",
            body,
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-reused-2",
            idempotency_key="idem-reused-2",
        )
    finally:
        transport.close()
    assert log.count("check") == 2
    attempts = _attempts()
    assert len(attempts) == 2
    assert all(attempt["local"] is None for attempt in attempts)
