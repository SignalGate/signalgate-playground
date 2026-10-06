"""Tests for the kit transport's local canary branch: verdicts, 422s, log
acknowledgements, delays and the guard-before-canary ordering.

Every test here proves the stub is never touched: a canary answer, like a
guard refusal, never reaches the network. Every attempt record the real
transport emits here is checked against the pinned attempt shape after
each test.
"""

from __future__ import annotations

import time

import pytest
import signalgate

from app.kit import kit_transport as kit_transport_module
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.kit_transport import (
    CANARY_REJECT_CODE,
    METHOD_ONLY_CODE,
    METHOD_ONLY_STATUS,
    KitTransport,
)
from app.kit.local_constants import CANARY_DELAY_MAX_MS, CLIENT_CLOSE_TIMEOUT_S
from tests.contract.samples import ATTEMPT_RECORD, assert_same_shape
from tests.support.sdk_stub import Reply, check_verdict, install_sdk_stub

DUMMY_KEY = "dummy-test-value"
DUMMY_CIPHERTEXT = "dummy-ciphertext-value"
CHECK_URL = "https://example.test/v0/check"
LOG_URL = "https://example.test/v0/log"

# A short local pacing value, a small fraction of the app's own canary delay
# cap: long enough to measure in the real-elapsed-time test, short enough to
# keep this file fast. It paces these tests only.
_TEST_DELAY_MS = CANARY_DELAY_MAX_MS // 100

# A timeout comfortably above _TEST_DELAY_MS, derived from it, so a delay
# below it never times out in the "below the timeout" tests.
_TEST_TIMEOUT_MS = _TEST_DELAY_MS * 4

# A delay well above _TEST_TIMEOUT_MS, derived from it (and still within the
# cap), so the "over the timeout" tests can prove the wait is capped at the
# timeout rather than this longer registered delay.
_TEST_OVERLONG_DELAY_MS = _TEST_TIMEOUT_MS * 5

# The two registered delays that must time out: exactly the timeout, and over it.
AT_OR_OVER_THE_TIMEOUT = [
    pytest.param(_TEST_TIMEOUT_MS, id="equal-to-the-timeout"),
    pytest.param(_TEST_OVERLONG_DELAY_MS, id="over-the-timeout"),
]


def _seconds(milliseconds: int) -> float:
    return milliseconds / 1000.0


def _event(nonce: str = "nonce-canary") -> signalgate.Event:
    return signalgate.Event(
        user_id="user-under-test",
        ip="198.51.100.7",
        method="playground.action",
        timestamp="2026-01-02T03:04:05+00:00",
        payload=signalgate.EncryptedPayload(
            encrypted=DUMMY_CIPHERTEXT, timestamp=1_700_000_000, nonce=nonce
        ),
    )


def _body(method: str = "playground.action", nonce: str = "nonce-canary") -> dict:
    return {
        "user_id": "user-under-test",
        "ip": "198.51.100.7",
        "method": method,
        "timestamp": "2026-01-02T03:04:05+00:00",
        "payload": {"encrypted": DUMMY_CIPHERTEXT, "timestamp": 1_700_000_000, "nonce": nonce},
    }


def _new_client(**kwargs) -> signalgate.Client:
    return signalgate.Client(DUMMY_KEY, http=KitTransport(DUMMY_KEY), log_retry_base_ms=0, **kwargs)


def _fail_every_call(monkeypatch: pytest.MonkeyPatch):
    """A stub that always fails: a canary or guard answer must never reach it."""
    return install_sdk_stub(monkeypatch, Reply(raise_="connect"))


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


def _last_attempt() -> dict:
    attempts = _attempts()
    assert attempts
    return attempts[-1]


@pytest.fixture(autouse=True)
def _every_attempt_record_has_the_pinned_shape(_reset_kit_state):
    # Requesting the shared reset fixture makes this teardown run first,
    # while this test's records are still stored.
    yield
    assert calls.record_errors == 0, "a record could not be stored, so its shape went unchecked"
    for attempt in _attempts():
        assert_same_shape(attempt, ATTEMPT_RECORD)


def _patch_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Record the kit's sleeps instead of waiting them out."""
    recorded: list[float] = []
    monkeypatch.setattr(kit_transport_module.time, "sleep", recorded.append)
    return recorded


# --- a registered verdict answers after its delay -------------------------------------


def test_a_registered_block_verdict_answers_after_its_real_delay(monkeypatch):
    log = _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-block", "answer": "block", "delay_ms": _TEST_DELAY_MS}])
    client = _new_client()
    started = time.perf_counter()
    try:
        result = client.check(_event(nonce="nonce-block"))
    finally:
        client.close()
    elapsed_s = time.perf_counter() - started
    assert result.action == "block"
    assert result.failed_open is False
    assert elapsed_s >= _seconds(_TEST_DELAY_MS)
    assert log.count() == 0
    attempt = _last_attempt()
    assert attempt["local"] == "canary"
    assert attempt["verdict"] == "block"
    assert attempt["status"] == 200
    # The verdict carries the request id the client generated for this check.
    assert result.request_id == attempt["request_id"]
    assert result.request_id
    assert_same_shape(attempt, ATTEMPT_RECORD)


@pytest.mark.parametrize("answer", ["block", "dry_run_block", "admin_alert"])
def test_every_verdict_answer_is_forwarded_as_the_checks_action(monkeypatch, answer):
    _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-verdict", "answer": answer, "delay_ms": 0}])
    client = _new_client()
    try:
        result = client.check(_event(nonce="nonce-verdict"))
    finally:
        client.close()
    assert result.action == answer
    assert result.failed_open is False


def test_the_sleep_duration_is_the_registered_delay_when_below_the_timeout(monkeypatch):
    _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-fast", "answer": "block", "delay_ms": _TEST_DELAY_MS}])
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            CHECK_URL,
            _body(nonce="nonce-fast"),
            timeout_ms=_TEST_TIMEOUT_MS,
            request_id="req-1",
            idempotency_key="idem-1",
        )
    finally:
        transport.close()
    assert recorded == [_seconds(_TEST_DELAY_MS)]


def test_a_canary_verdict_carries_the_callers_request_id(monkeypatch):
    log = _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-verdict-id", "answer": "block", "delay_ms": 0}])
    transport = KitTransport(DUMMY_KEY)
    try:
        response = transport.post(
            CHECK_URL,
            _body(nonce="nonce-verdict-id"),
            timeout_ms=_TEST_TIMEOUT_MS,
            request_id="req-canary-verdict",
            idempotency_key="idem-canary-verdict",
        )
    finally:
        transport.close()
    assert response.status_code == 200
    assert response.request_id == "req-canary-verdict"
    assert response.body["request_id"] == "req-canary-verdict"
    assert response.body["data"]["request_id"] == "req-canary-verdict"
    assert response.body["data"]["action"] == "block"
    assert log.count() == 0
    attempt = _last_attempt()
    assert attempt["request_id"] == "req-canary-verdict"
    assert attempt["idempotency_key"] == "idem-canary-verdict"


# --- a registered 422 answer raises ------------------------------------------------------


@pytest.mark.parametrize("fail_open", [True, False])
def test_a_registered_422_answer_raises_a_server_error_regardless_of_fail_open(
    monkeypatch, fail_open
):
    _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-reject", "answer": "422", "delay_ms": 0}])
    client = _new_client(fail_open=fail_open)
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            client.check(_event(nonce="nonce-reject"))
    finally:
        client.close()
    assert caught.value.status_code == 422
    assert caught.value.code == CANARY_REJECT_CODE
    attempt = _last_attempt()
    assert attempt["local"] == "canary"
    assert attempt["status"] == 422
    assert attempt["code"] == CANARY_REJECT_CODE
    # The rejection carries the request id the client generated for this check.
    assert caught.value.request_id == attempt["request_id"]
    assert caught.value.request_id
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_canary_422_carries_the_callers_request_id(monkeypatch):
    log = _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-reject-id", "answer": "422", "delay_ms": 0}])
    transport = KitTransport(DUMMY_KEY)
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            transport.post(
                CHECK_URL,
                _body(nonce="nonce-reject-id"),
                timeout_ms=_TEST_TIMEOUT_MS,
                request_id="req-canary-reject",
                idempotency_key="idem-canary-reject",
            )
    finally:
        transport.close()
    assert (caught.value.status_code, caught.value.code) == (422, CANARY_REJECT_CODE)
    assert caught.value.request_id == "req-canary-reject"
    assert log.count() == 0
    assert _last_attempt()["request_id"] == "req-canary-reject"


# --- a registered nonce on /v0/log is acknowledged locally --------------------------------


@pytest.mark.parametrize("answer", ["block", "dry_run_block", "admin_alert", "422"])
def test_a_registered_nonce_on_log_gives_a_local_acknowledgement(monkeypatch, answer):
    log = _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-log", "answer": answer, "delay_ms": 0}])
    client = _new_client()
    client.log(_event(nonce="nonce-log"))
    client.close()
    snapshot = client.metrics.snapshot_flat()
    assert snapshot.get("log_sent_total") == 1
    assert log.count() == 0
    attempt = _last_attempt()
    assert attempt["local"] == "canary"
    assert attempt["status"] == 200
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_log_acknowledgement_carries_the_callers_request_id(monkeypatch):
    log = _fail_every_call(monkeypatch)
    canaries.register([{"nonce": "nonce-log-id", "answer": "block", "delay_ms": 0}])
    transport = KitTransport(DUMMY_KEY)
    try:
        response = transport.post(
            LOG_URL,
            _body(nonce="nonce-log-id"),
            timeout_ms=_TEST_TIMEOUT_MS,
            request_id="req-canary-log",
            idempotency_key="idem-canary-log",
        )
    finally:
        transport.close()
    assert response.status_code == 200
    assert response.request_id == "req-canary-log"
    assert response.body == {
        "ok": True,
        "request_id": "req-canary-log",
        "error": None,
        "data": None,
    }
    assert log.count() == 0
    assert _last_attempt()["request_id"] == "req-canary-log"


def test_a_log_canary_waits_its_delay_before_acknowledging(monkeypatch):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-log-wait", "answer": "block", "delay_ms": _TEST_DELAY_MS}])
    client = signalgate.Client(
        DUMMY_KEY,
        http=KitTransport(DUMMY_KEY),
        log_timeout_ms=_TEST_TIMEOUT_MS,
        log_retry_base_ms=0,
    )
    client.log(_event(nonce="nonce-log-wait"))
    client.close(timeout_s=CLIENT_CLOSE_TIMEOUT_S)
    assert recorded == [_seconds(_TEST_DELAY_MS)]
    assert client.metrics.snapshot_flat().get("log_sent_total") == 1
    assert log.count() == 0


# --- a delay at or over the timeout gives a timeout, which fails open --------------------


@pytest.mark.parametrize("delay_ms", AT_OR_OVER_THE_TIMEOUT)
def test_a_delay_at_or_over_the_timeout_fails_open(monkeypatch, delay_ms):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-slow", "answer": "block", "delay_ms": delay_ms}])
    client = signalgate.Client(
        DUMMY_KEY,
        http=KitTransport(DUMMY_KEY),
        check_timeout_ms=_TEST_TIMEOUT_MS,
        log_retry_base_ms=0,
    )
    try:
        result = client.check(_event(nonce="nonce-slow"))
    finally:
        client.close()
    assert result.failed_open is True
    assert result.action == "allow"
    assert client.metrics.snapshot_flat().get('check_error_total{type="TimeoutError"}') == 1
    # The wait is capped at the timeout, never the registered delay past it.
    assert recorded == [_seconds(_TEST_TIMEOUT_MS)]
    assert log.count() == 0
    attempt = _last_attempt()
    assert attempt["timed_out"] is True
    assert attempt["local"] == "canary"
    assert attempt["status"] is None
    assert attempt["verdict"] is None
    assert_same_shape(attempt, ATTEMPT_RECORD)


@pytest.mark.parametrize("delay_ms", AT_OR_OVER_THE_TIMEOUT)
def test_a_delay_at_or_over_the_timeout_raises_without_fail_open(monkeypatch, delay_ms):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-slow2", "answer": "block", "delay_ms": delay_ms}])
    client = signalgate.Client(
        DUMMY_KEY,
        http=KitTransport(DUMMY_KEY),
        check_timeout_ms=_TEST_TIMEOUT_MS,
        fail_open=False,
        log_retry_base_ms=0,
    )
    try:
        with pytest.raises(signalgate.TimeoutError):
            client.check(_event(nonce="nonce-slow2"))
    finally:
        client.close()
    # The wait is capped at the timeout, never the registered delay past it.
    assert recorded == [_seconds(_TEST_TIMEOUT_MS)]
    assert log.count() == 0


def test_a_delay_just_below_the_timeout_still_answers(monkeypatch):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    just_below = _TEST_TIMEOUT_MS - 1
    canaries.register([{"nonce": "nonce-edge", "answer": "block", "delay_ms": just_below}])
    client = signalgate.Client(
        DUMMY_KEY,
        http=KitTransport(DUMMY_KEY),
        check_timeout_ms=_TEST_TIMEOUT_MS,
        fail_open=False,
        log_retry_base_ms=0,
    )
    try:
        result = client.check(_event(nonce="nonce-edge"))
    finally:
        client.close()
    assert (result.action, result.failed_open) == ("block", False)
    assert recorded == [_seconds(just_below)]
    assert log.count() == 0


@pytest.mark.parametrize("delay_ms", AT_OR_OVER_THE_TIMEOUT)
def test_a_log_canary_at_or_over_the_log_timeout_times_out_on_every_retry(monkeypatch, delay_ms):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-slow-log", "answer": "block", "delay_ms": delay_ms}])
    client = signalgate.Client(
        DUMMY_KEY,
        http=KitTransport(DUMMY_KEY),
        log_timeout_ms=_TEST_TIMEOUT_MS,
        log_retry_base_ms=0,
    )
    client.log(_event(nonce="nonce-slow-log"))
    client.close(timeout_s=CLIENT_CLOSE_TIMEOUT_S)
    snapshot = client.metrics.snapshot_flat()
    attempts = _attempts()
    # The SDK retries a timed-out delivery; every retry is answered locally too.
    assert len(attempts) > 1
    assert log.count() == 0
    assert "log_sent_total" not in snapshot
    assert snapshot['log_http_error_total{status="network"}'] == len(attempts)
    assert snapshot['log_dropped_total{reason="retry_exhausted"}'] == 1
    assert len({a["idempotency_key"] for a in attempts}) == 1
    assert [a["log_job"] for a in attempts] == [1] * len(attempts)
    assert [a["attempt"] for a in attempts] == list(range(1, len(attempts) + 1))
    assert all(a["local"] == "canary" for a in attempts)
    assert all(a["timed_out"] is True and a["status"] is None for a in attempts)
    # Each attempt waits out the log timeout, never the registered delay past it.
    assert recorded == [_seconds(_TEST_TIMEOUT_MS)] * len(attempts)


# --- the guard runs before the canary ---------------------------------------------------


def test_the_guard_refuses_a_bad_method_even_with_a_matching_canary(monkeypatch):
    log = _fail_every_call(monkeypatch)
    recorded = _patch_sleep(monkeypatch)
    canaries.register([{"nonce": "nonce-guarded", "answer": "block", "delay_ms": _TEST_DELAY_MS}])
    transport = KitTransport(DUMMY_KEY)
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            transport.post(
                CHECK_URL,
                _body(method="login.attempt", nonce="nonce-guarded"),
                timeout_ms=_TEST_TIMEOUT_MS,
                request_id="req-guarded",
                idempotency_key="idem-guarded",
            )
    finally:
        transport.close()
    assert caught.value.status_code == METHOD_ONLY_STATUS
    assert caught.value.code == METHOD_ONLY_CODE
    assert caught.value.request_id == "req-guarded"
    assert log.count() == 0
    # A guard refusal never waits, even when a canary with a delay matches.
    assert recorded == []
    attempt = _last_attempt()
    assert attempt["local"] == "guard"
    assert attempt["request_id"] == "req-guarded"


def test_an_unregistered_nonce_is_forwarded_not_answered_locally(monkeypatch):
    log = install_sdk_stub(monkeypatch, check_verdict("allow"))
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            CHECK_URL,
            _body(nonce="nonce-not-registered"),
            timeout_ms=_TEST_TIMEOUT_MS,
            request_id="req-1",
            idempotency_key="idem-1",
        )
    finally:
        transport.close()
    assert log.count() == 1
    assert _last_attempt()["local"] is None
