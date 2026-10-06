"""Tests for ``KitTransport`` itself: the guard, records, log numbering,
exception pass-through, fault injection and the guarded private import.

The canary branch has its own file (``test_canary_branch.py``), and the full
SDK-vs-kit equality matrix has its own file (``test_parity.py``).

Every attempt record the real transport emits in this file is checked
against the pinned attempt shape after each test.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import inspect
import json
from pathlib import Path

import pytest
import signalgate

from app.kit import kit_transport as kit_transport_module
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.kit_transport import (
    CANARY_REJECT_CODE,
    METHOD_ONLY_CODE,
    METHOD_ONLY_STATUS,
    METHODS,
    KitTransport,
)
from app.kit.local_constants import CANARY_DELAY_MAX_MS
from tests.contract.samples import ATTEMPT_RECORD, assert_same_shape
from tests.support.sdk_stub import (
    Reply,
    check_verdict,
    error_reply,
    forbid_sockets,
    install_sdk_stub,
    log_accepted,
    sequence,
)

DUMMY_KEY = "dummy-test-value"
DUMMY_CIPHERTEXT = "dummy-ciphertext-value"
CHECK_URL = "https://example.test/v0/check"
LOG_URL = "https://example.test/v0/log"

# A full-width Latin small letter p, used as the guard's "full-width first
# letter" variant.
_FULLWIDTH_P = "ｐ"

# A timeout used only as a pass-through value in tests below that never
# depend on real wait time (the guard raises with no I/O; log numbering
# never sleeps): derived from the app's own canary delay cap, not a fresh guess.
_PASSTHROUGH_TIMEOUT_MS = CANARY_DELAY_MAX_MS // 2


def _payload(nonce: str = "nonce-under-test") -> dict:
    return {"encrypted": DUMMY_CIPHERTEXT, "timestamp": 1_700_000_000, "nonce": nonce}


def _body(method: str = "playground.action", nonce: str = "nonce-under-test") -> dict:
    return {
        "user_id": "user-under-test",
        "ip": "198.51.100.7",
        "method": method,
        "timestamp": "2026-01-02T03:04:05+00:00",
        "payload": _payload(nonce),
    }


def _event(nonce: str = "nonce-under-test") -> signalgate.Event:
    return signalgate.Event(
        user_id="user-under-test",
        ip="198.51.100.7",
        method="playground.action",
        timestamp="2026-01-02T03:04:05+00:00",
        payload=signalgate.EncryptedPayload(
            encrypted=DUMMY_CIPHERTEXT, timestamp=1_700_000_000, nonce=nonce
        ),
    )


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
    assert attempts, "expected at least one attempt record"
    return attempts[-1]


def _assert_every_attempt_has_the_pinned_shape() -> int:
    """Shape-check every stored attempt record; return how many were checked.

    A record that could not be stored at all never reaches the page, so it
    would slip past the shape check: that counts as a failure too.
    """
    assert calls.record_errors == 0, "a record could not be stored, so its shape went unchecked"
    attempts = _attempts()
    for attempt in attempts:
        assert_same_shape(attempt, ATTEMPT_RECORD)
    return len(attempts)


@pytest.fixture(autouse=True)
def _every_attempt_record_has_the_pinned_shape(_reset_kit_state):
    # Requesting the shared reset fixture makes this teardown run first,
    # while this test's records are still stored.
    yield
    _assert_every_attempt_has_the_pinned_shape()


def _new_client(**kwargs) -> signalgate.Client:
    return signalgate.Client(DUMMY_KEY, http=KitTransport(DUMMY_KEY), log_retry_base_ms=0, **kwargs)


# --- the shape check itself ------------------------------------------------------------


def test_the_shape_check_sees_the_attempt_a_real_transport_emits(monkeypatch):
    install_sdk_stub(monkeypatch, check_verdict())
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            CHECK_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-shape",
            idempotency_key="idem-shape",
        )
    finally:
        transport.close()
    assert _assert_every_attempt_has_the_pinned_shape() == 1


@pytest.mark.parametrize(
    "damage",
    [
        pytest.param(lambda r: {**r, "extra": None}, id="extra-key"),
        pytest.param(lambda r: {k: v for k, v in r.items() if k != "timeout_phase"}, id="missing"),
        pytest.param(lambda r: {**r, "attempt": "1"}, id="wrong-type"),
        pytest.param(lambda r: {**r, "local": "elsewhere"}, id="unknown-enum"),
    ],
)
def test_the_shape_check_rejects_a_damaged_attempt_record(monkeypatch, damage):
    install_sdk_stub(monkeypatch, check_verdict())
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            CHECK_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="req-shape",
            idempotency_key="idem-shape",
        )
    finally:
        transport.close()
    damaged = damage(_last_attempt())
    calls.reset()
    calls.append(damaged)
    try:
        with pytest.raises(AssertionError):
            _assert_every_attempt_has_the_pinned_shape()
    finally:
        calls.reset()


def test_the_shape_check_fails_when_a_record_could_not_be_stored():
    calls.append({"kind": "attempt", "t0": float("nan")})
    try:
        with pytest.raises(AssertionError):
            _assert_every_attempt_has_the_pinned_shape()
    finally:
        calls.reset()


# --- protocol conformance ------------------------------------------------------------


def test_kit_transport_is_a_signalgate_transport():
    transport = KitTransport(DUMMY_KEY)
    try:
        assert isinstance(transport, signalgate.Transport)
    finally:
        transport.close()


@pytest.mark.parametrize("name", ["post", "close"])
def test_post_and_close_match_the_protocols_signature(name):
    def parameters(function):
        return [(p.name, p.kind) for p in inspect.signature(function).parameters.values()]

    assert parameters(getattr(KitTransport, name)) == parameters(
        getattr(signalgate.Transport, name)
    )


# --- the guard -------------------------------------------------------------------------

GUARD_METHOD_VARIANTS = [
    "Playground.action",
    " playground.action",
    "playground.action ",
    "playground_action",
    _FULLWIDTH_P + "laground.action",
    "login.attempt",
    None,
    123,
]


@pytest.mark.parametrize(
    "method", GUARD_METHOD_VARIANTS, ids=[repr(m) for m in GUARD_METHOD_VARIANTS]
)
def test_the_guard_refuses_every_bad_method_value(monkeypatch, method):
    guard = forbid_sockets(monkeypatch)
    log = install_sdk_stub(monkeypatch, check_verdict())
    transport = KitTransport(DUMMY_KEY)
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            transport.post(
                CHECK_URL,
                _body(method=method),
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-guard",
                idempotency_key="idem-guard",
            )
    finally:
        transport.close()
    assert caught.value.status_code == METHOD_ONLY_STATUS
    assert caught.value.code == METHOD_ONLY_CODE
    assert caught.value.request_id == "req-guard"
    assert log.count() == 0
    assert guard.attempts == []
    attempt = _last_attempt()
    assert attempt["local"] == "guard"
    assert attempt["request_id"] == "req-guard"
    assert attempt["idempotency_key"] == "idem-guard"
    assert attempt["attempt"] == 1
    assert (attempt["status"], attempt["code"]) == (METHOD_ONLY_STATUS, METHOD_ONLY_CODE)
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_the_guard_refuses_a_body_missing_the_method_key(monkeypatch):
    guard = forbid_sockets(monkeypatch)
    transport = KitTransport(DUMMY_KEY)
    body = _body()
    del body["method"]
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            transport.post(
                CHECK_URL,
                body,
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-1",
                idempotency_key="idem-1",
            )
    finally:
        transport.close()
    assert caught.value.status_code == METHOD_ONLY_STATUS
    assert guard.attempts == []


@pytest.mark.parametrize("body", [None, "not-a-dict", 123, ["a", "list"]])
def test_the_guard_refuses_a_non_dict_body(monkeypatch, body):
    guard = forbid_sockets(monkeypatch)
    transport = KitTransport(DUMMY_KEY)
    try:
        with pytest.raises(signalgate.ServerError):
            transport.post(
                CHECK_URL,
                body,
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-1",
                idempotency_key="idem-1",
            )
    finally:
        transport.close()
    assert guard.attempts == []


@pytest.mark.parametrize("fail_open", [True, False])
def test_a_guard_refusal_raises_through_check_even_with_fail_open(monkeypatch, fail_open):
    install_sdk_stub(monkeypatch, check_verdict())
    client = _new_client(fail_open=fail_open)
    bad_event = dataclasses.replace(_event(), method="login.attempt")
    try:
        with pytest.raises(signalgate.ServerError) as caught:
            client.check(bad_event)
    finally:
        client.close()
    assert caught.value.status_code == METHOD_ONLY_STATUS
    assert caught.value.code == METHOD_ONLY_CODE
    # The error carries the request id the client generated for this check.
    assert caught.value.request_id == _last_attempt()["request_id"]
    assert caught.value.request_id


def test_a_guard_refusal_on_log_drops_once_with_no_retry(monkeypatch):
    install_sdk_stub(monkeypatch, log_accepted())
    client = _new_client()
    bad_event = dataclasses.replace(_event(), method="login.attempt")
    client.log(bad_event)
    client.close()
    snapshot = client.metrics.snapshot_flat()
    assert snapshot[f'log_http_error_total{{status="{METHOD_ONLY_STATUS}"}}'] == 1
    assert snapshot['log_dropped_total{reason="retry_exhausted"}'] == 1
    assert "log_sent_total" not in snapshot
    attempts = _attempts()
    assert len(attempts) == 1
    assert attempts[0]["local"] == "guard"


# --- records -----------------------------------------------------------------------------


def test_a_timeout_then_a_422_on_one_log_gives_two_ordered_records(monkeypatch):
    install_sdk_stub(monkeypatch, sequence(Reply(raise_="read_timeout"), error_reply(422)))
    client = _new_client()
    client.log(_event())
    client.close()
    attempts = _attempts()
    assert len(attempts) == 2
    first, second = attempts
    assert first["idempotency_key"] == second["idempotency_key"]
    assert first["log_job"] == second["log_job"] == 1
    assert (first["attempt"], second["attempt"]) == (1, 2)
    assert first["timed_out"] is True
    assert first["timeout_phase"] == "read"
    assert first["status"] is None
    assert second["timed_out"] is False
    assert second["status"] == 422
    assert second["code"] == "TEST_CODE"
    for attempt in attempts:
        assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_connect_timeout_is_phased_as_connect(monkeypatch):
    install_sdk_stub(monkeypatch, Reply(raise_="connect_timeout"))
    client = _new_client(fail_open=True)
    client.check(_event())
    client.close()
    attempt = _last_attempt()
    assert attempt["timed_out"] is True
    assert attempt["timeout_phase"] == "connect"
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_2xx_check_records_its_verdict_and_processing_time(monkeypatch):
    # A marker distinguishable from the stub's own default of 0, so a kit
    # that hard-codes that default can't pass this test by accident.
    marker_processing_time_us = len(DUMMY_CIPHERTEXT) * 1_000
    install_sdk_stub(
        monkeypatch,
        Reply(
            body={
                "ok": True,
                "request_id": "req-fixed",
                "error": None,
                "data": {
                    "action": "block",
                    "request_id": "req-fixed",
                    "processing_time_us": marker_processing_time_us,
                },
            },
            request_id="req-fixed",
        ),
    )
    client = _new_client()
    result = client.check(_event())
    client.close()
    assert result.action == "block"
    attempt = _last_attempt()
    assert attempt["endpoint"] == "check"
    assert attempt["status"] == 200
    assert attempt["verdict"] == "block"
    assert attempt["processing_time_us"] == marker_processing_time_us
    assert attempt["local"] is None
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_network_error_is_recorded_with_no_status_or_timeout(monkeypatch):
    install_sdk_stub(monkeypatch, Reply(raise_="connect"))
    client = _new_client(fail_open=True)
    client.check(_event())
    client.close()
    attempt = _last_attempt()
    assert attempt["status"] is None
    assert attempt["timed_out"] is False
    assert attempt["timeout_phase"] is None
    assert attempt["error_class"] == "NetworkError"
    assert_same_shape(attempt, ATTEMPT_RECORD)


def test_a_json_dump_of_records_holds_neither_ciphertext_nor_key(monkeypatch):
    install_sdk_stub(monkeypatch, sequence(check_verdict(), log_accepted()))
    client = _new_client()
    client.check(_event())
    client.log(_event())
    client.close()
    records, *_ = calls.read_page(0)
    dumped = json.dumps(records)
    assert DUMMY_KEY not in dumped
    assert DUMMY_CIPHERTEXT not in dumped


def test_call_seq_is_read_from_the_bound_thread_local_call(monkeypatch):
    from app.kit.calls import bind_call

    install_sdk_stub(monkeypatch, check_verdict())
    transport = KitTransport(DUMMY_KEY)
    try:
        with bind_call(42):
            transport.post(
                CHECK_URL,
                _body(),
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-1",
                idempotency_key="idem-1",
            )
    finally:
        transport.close()
    assert _last_attempt()["call_seq"] == 42


def test_a_log_attempt_never_carries_a_call_seq(monkeypatch):
    install_sdk_stub(monkeypatch, log_accepted())
    client = _new_client()
    client.log(_event())
    client.close()
    assert _last_attempt()["call_seq"] is None


# --- log numbering ------------------------------------------------------------------------


def test_a_new_idempotency_key_gets_the_next_log_job_number(monkeypatch):
    install_sdk_stub(monkeypatch, log_accepted())
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            LOG_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="r1",
            idempotency_key="idem-a",
        )
        transport.post(
            LOG_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="r2",
            idempotency_key="idem-b",
        )
    finally:
        transport.close()
    attempts = _attempts()
    assert [a["log_job"] for a in attempts] == [1, 2]
    assert [a["attempt"] for a in attempts] == [1, 1]


def test_a_repeated_idempotency_key_keeps_the_same_job_and_bumps_the_attempt(monkeypatch):
    install_sdk_stub(monkeypatch, log_accepted())
    transport = KitTransport(DUMMY_KEY)
    try:
        transport.post(
            LOG_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="r1",
            idempotency_key="idem-a",
        )
        transport.post(
            LOG_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="r2",
            idempotency_key="idem-a",
        )
        transport.post(
            LOG_URL,
            _body(),
            timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
            request_id="r3",
            idempotency_key="idem-b",
        )
    finally:
        transport.close()
    attempts = _attempts()
    assert [a["log_job"] for a in attempts] == [1, 1, 2]
    assert [a["attempt"] for a in attempts] == [1, 2, 1]


def test_driving_a_real_client_with_n_logs_matches_log_enqueued_total(monkeypatch):
    install_sdk_stub(monkeypatch, log_accepted())
    client = _new_client()
    n = 5
    enqueued_after = []
    for i in range(n):
        client.log(_event(nonce=f"nonce-{i}"))
        enqueued_after.append(client.metrics.get("log_enqueued_total"))
    client.close()
    assert enqueued_after == list(range(1, n + 1))
    # Join by nonce, not by sorting the attempts: this proves each event's own
    # log_job matches log_enqueued_total right after its own enqueue, not just
    # that the log_job values are some permutation of 1..n.
    log_job_by_nonce = {a["nonce"]: a["log_job"] for a in _attempts()}
    for i, expected_log_job in enumerate(enqueued_after):
        assert log_job_by_nonce[f"nonce-{i}"] == expected_log_job
    assert all(a["attempt"] == 1 for a in _attempts())


def test_guard_refused_and_canary_logs_are_numbered_with_forwarded_ones(monkeypatch):
    # A refused or locally answered log is still a job the SDK enqueued, so
    # it takes the next number just like a forwarded one.
    log = install_sdk_stub(monkeypatch, log_accepted())
    canaries.register([{"nonce": "nonce-canary-log", "answer": "block", "delay_ms": 0}])
    events = [
        _event(nonce="nonce-forwarded-first"),
        dataclasses.replace(_event(nonce="nonce-guard-refused"), method="login.attempt"),
        _event(nonce="nonce-canary-log"),
        _event(nonce="nonce-forwarded-last"),
    ]
    client = _new_client()
    enqueued_after = []
    for event in events:
        client.log(event)
        enqueued_after.append(client.metrics.get("log_enqueued_total"))
    client.close()
    assert enqueued_after == list(range(1, len(events) + 1))
    attempts = _attempts()
    assert len(attempts) == len(events)
    by_nonce = {a["nonce"]: a for a in attempts}
    in_event_order = [by_nonce[event.payload.nonce] for event in events]
    assert [a["log_job"] for a in in_event_order] == enqueued_after
    assert [a["attempt"] for a in in_event_order] == [1] * len(events)
    assert [a["local"] for a in in_event_order] == [None, "guard", "canary", None]
    assert log.count("log") == 2


# --- the inner transport's exception is passed through as-is ---------------------------


class _NotAnSdkError(BaseException):
    """An exception outside the SDK's own hierarchy, and outside ``Exception``."""


class _RaisingInner:
    """A stand-in for the SDK's HTTP transport that raises one given object."""

    def __init__(self, error: BaseException) -> None:
        self.error = error
        self.posts = 0

    def post(self, url, body, *, timeout_ms, request_id, idempotency_key):
        self.posts += 1
        raise self.error

    def close(self) -> None:
        pass


PASS_THROUGH_ERRORS = [
    pytest.param(lambda: TypeError("not an SDK error"), id="TypeError"),
    pytest.param(
        lambda: signalgate.ServerError(500, "TEST_CODE", "test error", "req-inner", None),
        id="ServerError",
    ),
    pytest.param(lambda: signalgate.TimeoutError("test timeout"), id="TimeoutError"),
    pytest.param(lambda: signalgate.NetworkError("test network error"), id="NetworkError"),
    pytest.param(_NotAnSdkError, id="BaseException-subclass"),
]


@pytest.mark.parametrize("url", [CHECK_URL, LOG_URL], ids=["check", "log"])
@pytest.mark.parametrize("make_error", PASS_THROUGH_ERRORS)
def test_the_inner_transports_exception_object_is_re_raised_unchanged(url, make_error):
    sentinel = make_error()
    inner = _RaisingInner(sentinel)
    transport = KitTransport(DUMMY_KEY)
    transport._inner = inner
    try:
        with pytest.raises(type(sentinel)) as caught:
            transport.post(
                url,
                _body(),
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-pass-through",
                idempotency_key="idem-pass-through",
            )
    finally:
        transport.close()
    assert caught.value is sentinel
    assert inner.posts == 1
    attempt = _last_attempt()
    assert attempt["local"] is None
    assert attempt["error_class"] == type(sentinel).__name__
    assert attempt["request_id"] == "req-pass-through"
    if isinstance(sentinel, signalgate.ServerError):
        assert (attempt["status"], attempt["code"]) == (sentinel.status_code, sentinel.code)
    else:
        assert (attempt["status"], attempt["code"]) == (None, None)
    assert attempt["timed_out"] is isinstance(sentinel, signalgate.TimeoutError)


# --- fault injection ------------------------------------------------------------------------


def _raise_on_append(*_args, **_kwargs):
    raise RuntimeError("recording is broken in this test")


def test_a_fault_injected_into_recording_leaves_posts_own_behaviour_unchanged(monkeypatch):
    monkeypatch.setattr(calls, "append", _raise_on_append)
    log = install_sdk_stub(monkeypatch, sequence(log_accepted(), error_reply(400), log_accepted()))
    client = _new_client()
    client.log(_event(nonce="n1"))
    client.log(_event(nonce="n2"))
    client.log(_event(nonce="n3"))
    client.close()
    snapshot = client.metrics.snapshot_flat()
    enqueued = snapshot.get("log_enqueued_total", 0)
    sent = snapshot.get("log_sent_total", 0)
    dropped = snapshot.get('log_dropped_total{reason="retry_exhausted"}', 0)
    assert enqueued == 3
    assert sent == 2
    assert dropped == 1
    assert snapshot.get('log_http_error_total{status="400"}') == 1
    assert enqueued == sent + dropped
    assert log.count("log") == 3
    # Nothing could be stored, but nothing crashed either.
    assert calls.read_page(0)[0] == []


_FAULT_NONCE = "nonce-under-fault"


def _check_outcome(client: signalgate.Client, event: signalgate.Event) -> tuple:
    try:
        result = client.check(event)
    except signalgate.SignalGateError as exc:
        return ("raised", type(exc), getattr(exc, "status_code", None), getattr(exc, "code", None))
    return ("result", result.action, result.failed_open)


def _run_scenario(monkeypatch, endpoint: str, method: str, canary_answer: str | None):
    """Drive one check or log through a real client; return what the caller sees."""
    reply = check_verdict("block") if endpoint == "check" else log_accepted()
    log = install_sdk_stub(monkeypatch, reply)
    canaries.clear()
    if canary_answer is not None:
        canaries.register([{"nonce": _FAULT_NONCE, "answer": canary_answer, "delay_ms": 0}])
    event = dataclasses.replace(_event(nonce=_FAULT_NONCE), method=method)
    client = _new_client()
    try:
        if endpoint == "check":
            outcome = _check_outcome(client, event)
        else:
            client.log(event)
            outcome = None
    finally:
        client.close()
    return outcome, client.metrics.snapshot_flat(), log.count()


_GUARD_LOG_DROP = {
    f'log_http_error_total{{status="{METHOD_ONLY_STATUS}"}}': 1,
    'log_dropped_total{reason="retry_exhausted"}': 1,
}

FAULT_CASES = [
    pytest.param(
        "check", METHODS[0], None, ("result", "block", False), 1, id="forwarded-check-verdict"
    ),
    pytest.param(
        "check",
        "login.attempt",
        None,
        ("raised", signalgate.ServerError, METHOD_ONLY_STATUS, METHOD_ONLY_CODE),
        0,
        id="guard-refusal",
    ),
    pytest.param(
        "check", METHODS[0], "dry_run_block", ("result", "dry_run_block", False), 0, id="canary"
    ),
    pytest.param(
        "check",
        METHODS[0],
        "422",
        ("raised", signalgate.ServerError, 422, CANARY_REJECT_CODE),
        0,
        id="canary-422",
    ),
    pytest.param("log", METHODS[0], None, {"log_sent_total": 1}, 1, id="forwarded-log"),
    pytest.param("log", "login.attempt", None, _GUARD_LOG_DROP, 0, id="guard-refused-log"),
    pytest.param("log", METHODS[0], "block", {"log_sent_total": 1}, 0, id="canary-log"),
]


@pytest.mark.parametrize(
    ("endpoint", "method", "canary_answer", "expected", "expected_stub_calls"), FAULT_CASES
)
def test_every_branch_behaves_the_same_while_recording_raises(
    monkeypatch, endpoint, method, canary_answer, expected, expected_stub_calls
):
    clean = _run_scenario(monkeypatch, endpoint, method, canary_answer)
    assert len(_attempts()) == 1, "the fault-free run records its one attempt"
    calls.reset()
    with monkeypatch.context() as fault:
        fault.setattr(calls, "append", _raise_on_append)
        faulted = _run_scenario(monkeypatch, endpoint, method, canary_answer)
    assert calls.read_page(0)[0] == []
    assert faulted == clean
    outcome, snapshot, stub_calls = faulted
    assert stub_calls == expected_stub_calls
    if endpoint == "check":
        assert outcome == expected
    else:
        assert {key: snapshot.get(key) for key in expected} == expected


def test_a_malformed_url_still_lets_the_inner_transport_raise_its_own_error(monkeypatch):
    # The kit's own endpoint routing (urlsplit) can raise on an unusual URL.
    # That bookkeeping must never surface as the kit's own error: the request
    # still reaches the inner transport, which raises whatever a plain client
    # would raise for the same malformed URL.
    guard = forbid_sockets(monkeypatch)
    bad_url = "http://[::1/v0/check"
    transport = KitTransport(DUMMY_KEY)
    try:
        with pytest.raises(signalgate.NetworkError):
            transport.post(
                bad_url,
                _body(),
                timeout_ms=_PASSTHROUGH_TIMEOUT_MS,
                request_id="req-bad-url",
                idempotency_key="idem-bad-url",
            )
    finally:
        transport.close()
    assert guard.attempts == []
    attempt = _last_attempt()
    assert attempt["endpoint"] == "other"
    assert attempt["local"] is None
    assert attempt["error_class"] == "NetworkError"
    assert_same_shape(attempt, ATTEMPT_RECORD)


# --- the guarded private import -------------------------------------------------------------


def _load_kit_transport_with_broken_private_import():
    """Load a throwaway copy of ``kit_transport`` with the private import
    forced to fail, without touching the module every other test shares."""
    import builtins

    real_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "signalgate._http":
            raise ImportError("simulated for this test")
        return real_import(name, *args, **kwargs)

    source_path = Path(kit_transport_module.__file__)
    throwaway_name = f"{kit_transport_module.__name__}__import_failure_probe"
    spec = importlib.util.spec_from_file_location(throwaway_name, source_path)
    module = importlib.util.module_from_spec(spec)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(builtins, "__import__", broken_import)
        spec.loader.exec_module(module)
    return module


def test_import_failure_is_reported_without_raising_and_disables_construction():
    module = _load_kit_transport_with_broken_private_import()
    assert module.IMPORT_ERROR == "ImportError"
    assert module.transport_class() is None
    with pytest.raises(signalgate.ConfigError):
        module.KitTransport(DUMMY_KEY)
    # The shared module, used by every other test in this session, is untouched.
    assert kit_transport_module.IMPORT_ERROR is None
    assert kit_transport_module.transport_class() is kit_transport_module.KitTransport


# --- close() and repr() ----------------------------------------------------------------------


def test_close_is_idempotent_and_closes_the_inner_session_once():
    transport = KitTransport(DUMMY_KEY)
    closes: list[int] = []
    transport._inner.close = lambda: closes.append(1)  # type: ignore[method-assign]
    transport.close()
    transport.close()
    assert closes == [1]


def test_repr_and_str_never_show_the_key():
    transport = KitTransport("a-very-secret-value")
    try:
        assert "a-very-secret-value" not in repr(transport)
        assert "a-very-secret-value" not in str(transport)
    finally:
        transport.close()


def test_the_kits_own_codes_and_methods_are_fixed():
    assert METHOD_ONLY_CODE == "PLAYGROUND_METHOD_ONLY"
    assert CANARY_REJECT_CODE == "REQUEST_REJECTED"
    assert METHODS == ("playground.action", "playground.success")
