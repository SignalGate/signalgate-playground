"""Tests for /metrics, /calls, /canary and /shutdown (auth is in test_control.py)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest
import signalgate

from app import signalgate_client
from app.kit import control
from app.kit.calls import calls
from app.kit.canary import canaries
from app.kit.local_constants import (
    CANARY_DELAY_MAX_MS,
    CANARY_NONCE_MAX_CHARS,
    CLIENT_CLOSE_TIMEOUT_S,
    PAGE_MAX,
)
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_STOP_GRACE_S
from tests.contract.samples import (
    CALLS,
    CANARY,
    METRICS,
    SAMPLE_ATTEMPT,
    SAMPLE_CONSTRUCT,
    SHUTDOWN,
    VALIDATION_ERROR,
    assert_same_shape,
)
from tests.support.sdk_stub import Reply, install_sdk_stub, log_accepted

# /metrics must answer inside one of the console's own health-poll intervals
# (at the 95th percentile), even while a real SignalGate client's background
# worker is actively sending, so reading it never holds up the console.
METRICS_P95_BUDGET_S = APP_HEALTH_POLL_S

# How long one scripted /v0/log reply is held back: enough that a few dozen
# queued events keep the worker busy well past the sampling loop below,
# without this test depending on real network timing.
_LOG_JOB_DELAY_S = METRICS_P95_BUDGET_S / 5
_EVENTS_TO_KEEP_THE_WORKER_BUSY = 64
_METRICS_SAMPLES = 40

# The app's own bound on a client close: generous for a stub that answers
# at once, or for the scripted delays above, so only a hung worker would
# ever come close to it.
_DRAIN_TIMEOUT_S = CLIENT_CLOSE_TIMEOUT_S


def _auth(control_token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {control_token}"}


def _a_log_event() -> signalgate.Event:
    payload = signalgate.EncryptedPayload(encrypted="x", timestamp=0, nonce="n")
    return signalgate.Event(
        user_id="u",
        ip="127.0.0.1",
        method="playground.action",
        timestamp=datetime.now(timezone.utc).isoformat(),
        payload=payload,
    )


# --- /metrics -----------------------------------------------------------------


def test_metrics_is_empty_before_first_use(client, control_token):
    response = client.get("/__playground/metrics", headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), METRICS)
    assert response.json() == {}


def test_metrics_is_the_snapshot_verbatim(client, control_token, monkeypatch):
    scripted = {"log_enqueued_total": 3, 'log_dropped_total{reason="retry_exhausted"}': 1}
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", lambda: dict(scripted))
    response = client.get("/__playground/metrics", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, METRICS)
    assert body == scripted


def test_metrics_equals_snapshot_flat_after_scripted_use_through_the_sdk_stub(
    client, control_token, monkeypatch
):
    """A real `signalgate.Client`, answered by the SDK stub, logs a handful
    of events; once its own worker has drained them (`close()`'s own join,
    not a hand-rolled wait), `/metrics` reads back exactly `snapshot_flat()`
    -- the SDK's own counters, verbatim."""
    install_sdk_stub(monkeypatch, log_accepted())
    real_client = signalgate.Client("dummy-key-for-the-metrics-equality-test")
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", real_client.metrics.snapshot_flat)

    events_logged = 5
    for _ in range(events_logged):
        real_client.log(_a_log_event())
    real_client.close(timeout_s=_DRAIN_TIMEOUT_S)  # blocks until the worker has drained

    response = client.get("/__playground/metrics", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, METRICS)
    assert body == real_client.metrics.snapshot_flat()
    assert body["log_sent_total"] == events_logged


def test_metrics_p95_latency_stays_under_the_bound_while_the_log_worker_is_sending(
    client, control_token, monkeypatch
):
    """Drives a real `signalgate.Client` through the SDK stub with a
    slightly delayed `/v0/log` reply, so its background worker is still
    actively sending for the whole sampling loop: proves `/metrics`'s own
    p95 stays under its budget under real load, not merely when idle."""
    install_sdk_stub(monkeypatch, Reply(delay_s=_LOG_JOB_DELAY_S))
    real_client = signalgate.Client("dummy-key-for-the-metrics-p95-test")
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", real_client.metrics.snapshot_flat)
    auth = _auth(control_token)
    try:
        for _ in range(_EVENTS_TO_KEEP_THE_WORKER_BUSY):
            real_client.log(_a_log_event())

        samples = []
        for _ in range(_METRICS_SAMPLES):
            started = time.perf_counter()
            response = client.get("/__playground/metrics", headers=auth)
            samples.append(time.perf_counter() - started)
            assert response.status_code == 200
            assert_same_shape(response.json(), METRICS)

        # The worker must still be mid-drain once the sampling loop ends, or
        # this proves nothing about "while sending".
        sent_so_far = real_client.metrics.snapshot_flat().get("log_sent_total", 0)
        assert sent_so_far < _EVENTS_TO_KEEP_THE_WORKER_BUSY

        samples.sort()
        p95 = samples[int(len(samples) * 0.95) - 1]
        assert p95 < METRICS_P95_BUDGET_S
    finally:
        real_client.close(timeout_s=_DRAIN_TIMEOUT_S)


def test_metrics_never_builds_a_client(client, control_token, monkeypatch):
    calls_seen = []
    monkeypatch.setattr(
        signalgate.Client,
        "__init__",
        lambda self, *a, **k: calls_seen.append((a, k)),
    )
    response = client.get("/__playground/metrics", headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), METRICS)
    assert calls_seen == []


# --- /calls ---------------------------------------------------------------------


def test_calls_is_empty_before_any_record(client, control_token):
    response = client.get("/__playground/calls", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, CALLS)
    assert body == {
        "v": 1,
        "boot_id": body["boot_id"],
        "records": [],
        "cursor": 0,
        "more": False,
        "oldest_cursor": 0,
        "truncated": False,
        "record_errors": 0,
    }


def test_calls_pages_every_record_exactly_once(client, control_token):
    for seq in range(5):
        calls.append({**SAMPLE_CONSTRUCT, "seq": seq})

    seen = []
    cursor = 0
    while True:
        response = client.get(
            "/__playground/calls",
            params={"cursor": cursor, "limit": 2},
            headers=_auth(control_token),
        )
        assert response.status_code == 200
        body = response.json()
        assert_same_shape(body, CALLS)
        seen.extend(record["seq"] for record in body["records"])
        cursor = body["cursor"]
        if not body["more"]:
            break
    assert seen == [0, 1, 2, 3, 4]


def test_calls_json_round_trip_is_lossless(client, control_token):
    calls.append(SAMPLE_ATTEMPT)
    response = client.get("/__playground/calls", headers=_auth(control_token))
    body = response.json()
    assert_same_shape(body, CALLS)
    assert body["records"] == [SAMPLE_ATTEMPT]


def test_calls_reports_truncation_once_the_cap_evicts(client, control_token, monkeypatch):
    monkeypatch.setattr("app.kit.calls.CALLS_MAX_RECORDS", 2)
    for seq in range(4):
        calls.append({**SAMPLE_CONSTRUCT, "seq": seq})

    response = client.get("/__playground/calls", params={"cursor": 0}, headers=_auth(control_token))
    body = response.json()
    assert_same_shape(body, CALLS)
    assert body["truncated"] is True
    assert body["oldest_cursor"] == 2
    assert [record["seq"] for record in body["records"]] == [2, 3]


def test_calls_bad_cursor_is_400(client, control_token):
    response = client.get(
        "/__playground/calls", params={"cursor": -1}, headers=_auth(control_token)
    )
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)
    assert response.json() == {"ok": False, "error": "validation_error"}


def test_calls_bad_limit_is_400(client, control_token):
    response = client.get(
        "/__playground/calls",
        params={"limit": int(PAGE_MAX) + 1},
        headers=_auth(control_token),
    )
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)


def test_calls_reports_record_errors(client, control_token):
    calls.append({"not": "json-serialisable", "bad": object()})
    response = client.get("/__playground/calls", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, CALLS)
    assert body["record_errors"] == 1


# --- /canary ----------------------------------------------------------------------


def test_canary_registers_a_valid_body(client, control_token):
    body = {
        "canaries": [
            {"nonce": "n1", "answer": "block", "delay_ms": 0},
            {"nonce": "n2", "answer": "422"},
        ]
    }
    response = client.post("/__playground/canary", json=body, headers=_auth(control_token))
    assert response.status_code == 200
    payload = response.json()
    assert_same_shape(payload, CANARY)
    assert payload == {"registered": 2}
    assert len(canaries) == 2


def test_canary_all_or_nothing_on_one_bad_item(client, control_token):
    body = {
        "canaries": [
            {"nonce": "good", "answer": "block"},
            {"nonce": "good", "answer": "not-a-real-answer"},
        ]
    }
    response = client.post("/__playground/canary", json=body, headers=_auth(control_token))
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)
    assert response.json() == {"ok": False, "error": "validation_error"}
    assert len(canaries) == 0


# Every one of these bodies must be rejected whole, with nothing registered:
# a bad answer, a negative delay, a delay over the cap, a bool delay (Python's
# bool is an int subclass, so it needs its own check), a float delay, a
# string delay, a nonce that is too long, and an empty nonce (the 1-character
# lower bound).
_INVALID_CANARY_ITEMS = [
    pytest.param({"nonce": "n", "answer": "not-a-real-answer"}, id="bad_answer"),
    pytest.param({"nonce": "n", "answer": "block", "delay_ms": -1}, id="negative_delay"),
    pytest.param(
        {"nonce": "n", "answer": "block", "delay_ms": int(CANARY_DELAY_MAX_MS) + 1},
        id="delay_over_the_cap",
    ),
    pytest.param({"nonce": "n", "answer": "block", "delay_ms": True}, id="bool_delay"),
    pytest.param({"nonce": "n", "answer": "block", "delay_ms": 1.5}, id="float_delay"),
    pytest.param({"nonce": "n", "answer": "block", "delay_ms": "5"}, id="string_delay"),
    pytest.param(
        {"nonce": "n" * (int(CANARY_NONCE_MAX_CHARS) + 1), "answer": "block"},
        id="nonce_too_long",
    ),
    pytest.param({"nonce": "", "answer": "block"}, id="empty_nonce"),
]


@pytest.mark.parametrize("item", _INVALID_CANARY_ITEMS)
def test_canary_rejects_every_invalid_item_and_registers_nothing(client, control_token, item):
    response = client.post(
        "/__playground/canary", json={"canaries": [item]}, headers=_auth(control_token)
    )
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)
    assert response.json() == {"ok": False, "error": "validation_error"}
    assert len(canaries) == 0


def test_canary_rejects_a_missing_canaries_key(client, control_token):
    response = client.post("/__playground/canary", json={}, headers=_auth(control_token))
    assert response.status_code == 400
    assert_same_shape(response.json(), VALIDATION_ERROR)
    assert len(canaries) == 0


def test_canary_re_registering_a_nonce_replaces_its_answer(client, control_token):
    first = {"canaries": [{"nonce": "n", "answer": "block"}]}
    second = {"canaries": [{"nonce": "n", "answer": "422"}]}
    client.post("/__playground/canary", json=first, headers=_auth(control_token))
    response = client.post("/__playground/canary", json=second, headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), CANARY)
    assert response.json() == {"registered": 1}
    assert len(canaries) == 1


# --- /shutdown (under TestClient) ------------------------------------------------


def test_shutdown_shape_with_no_client_ever_created(client, control_token):
    response = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, SHUTDOWN)
    assert body == {
        "ok": True,
        "boot_id": body["boot_id"],
        "closed": False,
        "metrics": None,
    }


def test_shutdown_is_harmless_the_second_time(client, control_token):
    first = client.post("/__playground/shutdown", headers=_auth(control_token))
    second = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert first.status_code == second.status_code == 200
    assert_same_shape(first.json(), SHUTDOWN)
    assert_same_shape(second.json(), SHUTDOWN)
    assert first.json() == second.json()


def test_shutdown_reports_closed_true_once_a_client_existed(client, control_token, monkeypatch):
    monkeypatch.setattr(signalgate_client, "client_state", lambda: (1, True))
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", lambda: {"log_sent_total": 1})
    response = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, SHUTDOWN)
    assert body["closed"] is True
    assert body["metrics"] == {"log_sent_total": 1}


def _wait_for_the_shutdown_executor() -> None:
    """Block until every close handed to `/shutdown`'s executor has finished,
    so a close a test left running never delays the next test's own."""
    control._SHUTDOWN_EXECUTOR.submit(lambda: None).result()


def test_shutdown_waits_for_the_close_no_longer_than_its_bound(client, control_token, monkeypatch):
    """A close that outlasts the bound is reported as not closed, and
    `/shutdown` answers without waiting for it to finish."""
    bound_s = CLIENT_CLOSE_TIMEOUT_S / 10
    close_takes_s = bound_s * 3 / 2
    close_finished = threading.Event()

    def slow_close() -> None:
        time.sleep(close_takes_s)
        close_finished.set()

    monkeypatch.setattr(control, "CLIENT_CLOSE_TIMEOUT_S", bound_s)
    monkeypatch.setattr(signalgate_client, "close_client", slow_close)
    monkeypatch.setattr(signalgate_client, "client_state", lambda: (1, True))
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", lambda: {"log_sent_total": 1})
    try:
        response = client.post("/__playground/shutdown", headers=_auth(control_token))
        answered_before_the_close_finished = not close_finished.is_set()
    finally:
        _wait_for_the_shutdown_executor()

    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, SHUTDOWN)
    assert answered_before_the_close_finished
    assert body["closed"] is False
    assert body["metrics"] is None


def test_shutdown_reports_not_closed_when_the_close_fails(client, control_token, monkeypatch):
    def failing_close() -> None:
        raise RuntimeError("the close failed")

    monkeypatch.setattr(signalgate_client, "close_client", failing_close)
    monkeypatch.setattr(signalgate_client, "client_state", lambda: (1, True))
    monkeypatch.setattr(signalgate_client, "metrics_snapshot", lambda: {"log_sent_total": 1})
    response = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, SHUTDOWN)
    assert body["closed"] is False
    assert body["metrics"] is None


def test_shutdown_close_bound_fits_inside_the_console_stop_grace():
    """The console stops the app the hard way once its grace runs out, so the
    longest `/shutdown` waits for a close must leave room inside it."""
    assert CLIENT_CLOSE_TIMEOUT_S < APP_STOP_GRACE_S


def test_shutdown_calls_close_client(client, control_token, monkeypatch):
    calls_seen = []
    monkeypatch.setattr(signalgate_client, "close_client", lambda: calls_seen.append(True))
    response = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), SHUTDOWN)
    assert calls_seen == [True]


def test_shutdown_under_testclient_never_raises_and_request_exit_is_a_no_op(client, control_token):
    """`request_exit` finds no real uvicorn server under TestClient, and there
    is no stdin lifeline configured here, so it quietly does nothing."""
    response = client.post("/__playground/shutdown", headers=_auth(control_token))
    assert response.status_code == 200
    assert_same_shape(response.json(), SHUTDOWN)
