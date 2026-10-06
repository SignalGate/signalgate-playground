"""Tests for the control-route client and its metric helpers."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import httpx
import pytest

from playground.supervisor.control_client import (
    KEY_DROPPED_RETRY,
    KEY_ENQUEUED,
    KEY_SENT,
    ControlClient,
    ControlError,
    counter,
    delta,
    http_errors_total,
    queued,
)
from playground.supervisor.launcher import AppProcess

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HEALTHY_ROOT = FIXTURES / "healthy"

TOKEN = "test-token"


def _client(handler, *, token: str = TOKEN) -> ControlClient:
    transport = httpx.MockTransport(handler)
    return ControlClient("http://127.0.0.1:1", token, transport=transport)


def _json_handler(status: int, body):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, json=body, request=request)

    return handler


def _non_json_handler(status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=b"not json", request=request)

    return handler


def test_health_unpacks_every_field():
    body = {
        "boot_id": "b1",
        "sdk": "signalgate",
        "sdk_version": "0.3.2",
        "methods": ["playground.action", "playground.success"],
        "clients_created": 2,
        "client_ready": True,
        "self_test": {"ok": True, "failed": [], "checks": {}},
        "key": "set",
    }
    client = _client(_json_handler(200, body))
    health = asyncio.run(client.health())
    assert health.boot_id == "b1"
    assert health.methods == ("playground.action", "playground.success")
    assert health.clients_created == 2
    assert health.client_ready is True
    assert health.key == "set"
    assert health.self_test == body["self_test"]


def test_health_raises_control_error_on_non_200():
    client = _client(_json_handler(500, {"detail": "boom"}))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.health())
    assert excinfo.value.status == 500


def test_health_raises_control_error_on_a_missing_key():
    # A 200 with a plausible but incomplete body: the missing key must never
    # surface as a raw KeyError to a caller that only expects ControlError.
    client = _client(_json_handler(200, {"boot_id": "b1"}))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.health())
    assert excinfo.value.status == 200


def test_health_raises_control_error_on_a_non_json_body():
    client = _client(_non_json_handler())
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.health())
    assert excinfo.value.status == 200


def test_metrics_returns_the_body_verbatim():
    body = {"log_enqueued_total": 3, 'log_dropped_total{reason="closed"}': 1}
    client = _client(_json_handler(200, body))
    assert asyncio.run(client.metrics()) == body


def test_metrics_empty_before_first_use():
    client = _client(_json_handler(200, {}))
    assert asyncio.run(client.metrics()) == {}


def test_metrics_raises_control_error_on_a_non_json_body():
    client = _client(_non_json_handler())
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.metrics())
    assert excinfo.value.status == 200


def test_metrics_raises_control_error_on_a_body_that_is_not_an_object():
    client = _client(_json_handler(200, [1, 2, 3]))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.metrics())
    assert excinfo.value.status == 200


# Each read route, with a body that would unpack cleanly if its status were
# 200: only the status can make these fail.
_WELL_FORMED_READS = {
    "health": (
        lambda client: client.health(),
        {
            "boot_id": "b1",
            "sdk": "signalgate",
            "sdk_version": "0.3.2",
            "methods": [],
            "clients_created": 0,
            "client_ready": False,
            "self_test": {"ok": True, "failed": [], "checks": {}},
            "key": "set",
        },
    ),
    "metrics": (lambda client: client.metrics(), {"log_enqueued_total": 0}),
    "ledger": (lambda client: client.ledger(), {"entries": [], "cursor": 0, "more": False}),
    "calls": (
        lambda client: client.calls(),
        {
            "v": 1,
            "boot_id": "b1",
            "records": [],
            "cursor": 0,
            "more": False,
            "oldest_cursor": 0,
            "truncated": False,
            "record_errors": 0,
        },
    ),
    "canary": (lambda client: client.canary([]), {"registered": 0}),
}


@pytest.mark.parametrize("route", sorted(_WELL_FORMED_READS))
@pytest.mark.parametrize("status", [401, 404, 500])
def test_every_read_route_raises_control_error_on_a_non_2xx_status(route, status):
    call, body = _WELL_FORMED_READS[route]
    client = _client(_json_handler(status, body))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(call(client))
    assert excinfo.value.status == status


@pytest.mark.parametrize("route", sorted(_WELL_FORMED_READS))
def test_every_read_route_accepts_its_well_formed_body_on_200(route):
    # The other half of the test above: the same bodies do unpack on a 200.
    call, body = _WELL_FORMED_READS[route]
    client = _client(_json_handler(200, body))
    asyncio.run(call(client))


def test_ledger_sends_run_cursor_and_limit_and_unpacks_the_page():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"entries": [{"kind": "action"}], "cursor": 5, "more": True}, request=request
        )

    client = _client(handler)
    page = asyncio.run(client.ledger(run="run-1", cursor=2, limit=10))
    assert seen["params"] == {"cursor": "2", "run": "run-1", "limit": "10"}
    assert page.entries == [{"kind": "action"}]
    assert page.cursor == 5
    assert page.more is True


def test_ledger_omits_run_and_limit_when_not_given():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["params"] = dict(request.url.params)
        return httpx.Response(
            200, json={"entries": [], "cursor": 0, "more": False}, request=request
        )

    client = _client(handler)
    asyncio.run(client.ledger())
    assert seen["params"] == {"cursor": "0"}


def test_ledger_raises_control_error_on_a_missing_key():
    client = _client(_json_handler(200, {"entries": []}))  # no cursor, no more
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.ledger())
    assert excinfo.value.status == 200


def test_ledger_raises_control_error_on_a_non_json_body():
    client = _client(_non_json_handler())
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.ledger())
    assert excinfo.value.status == 200


def test_calls_unpacks_every_field():
    body = {
        "v": 1,
        "boot_id": "b1",
        "records": [{"kind": "call"}],
        "cursor": 3,
        "more": False,
        "oldest_cursor": 0,
        "truncated": False,
        "record_errors": 0,
    }
    client = _client(_json_handler(200, body))
    page = asyncio.run(client.calls())
    assert page.records == [{"kind": "call"}]
    assert page.truncated is False
    assert page.record_errors == 0


def test_calls_raises_control_error_on_a_missing_key():
    client = _client(_json_handler(200, {"v": 1, "boot_id": "b1"}))  # missing the rest
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.calls())
    assert excinfo.value.status == 200


def test_calls_raises_control_error_on_a_non_json_body():
    client = _client(_non_json_handler())
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.calls())
    assert excinfo.value.status == 200


def test_canary_posts_items_and_returns_registered_count():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"registered": 2}, request=request)

    client = _client(handler)
    items = [{"nonce": "n1", "answer": "block", "delay_ms": 0}]
    registered = asyncio.run(client.canary(items))
    assert registered == 2
    assert seen["body"] == {"canaries": items}


def test_canary_raises_control_error_on_a_missing_key():
    client = _client(_json_handler(200, {}))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.canary([]))
    assert excinfo.value.status == 200


def test_canary_raises_control_error_on_a_non_json_body():
    client = _client(_non_json_handler())
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(client.canary([]))
    assert excinfo.value.status == 200


def test_inbox_asks_for_every_ref_and_returns_the_tokens():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["refs"] = request.url.params.get_list("ref")
        return httpx.Response(200, json={"tokens": {"r1": "t1"}}, request=request)

    tokens = asyncio.run(_client(handler).inbox(["r1", "r2"]))
    assert tokens == {"r1": "t1"}
    assert seen["refs"] == ["r1", "r2"]


@pytest.mark.parametrize("body", [{}, {"tokens": ["t1"]}, ["tokens"]])
def test_inbox_raises_control_error_on_a_malformed_body(body):
    with pytest.raises(ControlError):
        asyncio.run(_client(_json_handler(200, body)).inbox(["r1"]))
    with pytest.raises(ControlError) as excinfo:
        asyncio.run(_client(_json_handler(401, {"detail": "unauthorized"})).inbox(["r1"]))
    assert excinfo.value.status == 401


def test_shutdown_returns_the_reply_on_200():
    body = {"ok": True, "boot_id": "b1", "closed": True, "metrics": {"a": 1}}
    client = _client(_json_handler(200, body))
    reply = asyncio.run(client.shutdown())
    assert reply is not None
    assert reply.ok is True
    assert reply.closed is True
    assert reply.metrics == {"a": 1}


def test_shutdown_returns_none_on_non_2xx():
    client = _client(_json_handler(404, {"detail": "not found"}))
    assert asyncio.run(client.shutdown()) is None


def test_shutdown_returns_none_on_a_connect_error():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    client = _client(handler)
    assert asyncio.run(client.shutdown()) is None


def test_shutdown_returns_none_on_a_timeout():
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("timed out", request=request)

    client = _client(handler)
    assert asyncio.run(client.shutdown()) is None


def test_shutdown_returns_none_on_a_missing_key():
    # A launcher that only knows `ShutdownReply | None` must never see a raw
    # KeyError from an unexpected 2xx body; it falls back to a signal either
    # way, so this counts exactly like a non-2xx.
    client = _client(_json_handler(200, {"unexpected": True}))
    assert asyncio.run(client.shutdown()) is None


def test_shutdown_returns_none_on_a_non_json_body():
    client = _client(_non_json_handler())
    assert asyncio.run(client.shutdown()) is None


def test_repr_never_shows_the_token():
    client = _client(_json_handler(200, {}), token="super-secret-token")
    assert "super-secret-token" not in repr(client)


def test_aclose_is_safe_to_call():
    client = _client(_json_handler(200, {}))
    asyncio.run(client.aclose())


# -- metric helpers -----------------------------------------------------------


def test_counter_defaults_absent_keys_to_zero():
    assert counter({}, KEY_ENQUEUED) == 0
    assert counter({KEY_ENQUEUED: 5}, KEY_ENQUEUED) == 5


def test_queued_is_enqueued_minus_sent_and_dropped_retry_never_negative():
    assert queued({}) == 0
    snap = {KEY_ENQUEUED: 10, KEY_SENT: 7, KEY_DROPPED_RETRY: 1}
    assert queued(snap) == 2
    # Never negative even if the counters are (temporarily) inconsistent.
    assert queued({KEY_ENQUEUED: 1, KEY_SENT: 5}) == 0


def test_http_errors_total_sums_every_labelled_key():
    snap = {
        'log_http_error_total{status="422"}': 2,
        'log_http_error_total{status="500"}': 1,
        KEY_SENT: 9,
    }
    assert http_errors_total(snap) == 3


def test_delta_gives_one_entry_per_key_never_summed():
    before = {
        'log_http_error_total{status="422"}': 0,
        'log_dropped_total{reason="retry_exhausted"}': 0,
    }
    after = {
        'log_http_error_total{status="422"}': 1,
        'log_dropped_total{reason="retry_exhausted"}': 1,
    }
    changes = delta(before, after)
    assert changes['log_http_error_total{status="422"}'] == 1
    assert changes['log_dropped_total{reason="retry_exhausted"}'] == 1


def test_delta_covers_the_union_of_keys():
    before = {"a": 1}
    after = {"b": 2}
    assert delta(before, after) == {"a": -1, "b": 2}


# -- proxy avoidance, against a real live app ----------------------------------


def test_control_client_ignores_proxy_env_vars_on_every_route(tmp_path, monkeypatch):
    asyncio.run(_control_client_ignores_proxy_env_vars_on_every_route(tmp_path, monkeypatch))


async def _control_client_ignores_proxy_env_vars_on_every_route(
    tmp_path: Path, monkeypatch
) -> None:
    app = AppProcess(root=HEALTHY_ROOT, log_path=tmp_path / "app.log")
    base_url, token, _boot_id = await app.start()
    try:
        dead = "http://127.0.0.1:1"
        monkeypatch.setenv("HTTP_PROXY", dead)
        monkeypatch.setenv("HTTPS_PROXY", dead)
        monkeypatch.setenv("ALL_PROXY", dead)

        control = ControlClient(base_url, token)
        try:
            health = await control.health()
            assert health.boot_id

            metrics = await control.metrics()
            assert metrics == {}

            ledger_page = await control.ledger()
            assert ledger_page.entries == []

            calls_page = await control.calls()
            assert calls_page.records == []

            registered = await control.canary([{"nonce": "n1", "answer": "block", "delay_ms": 0}])
            assert registered == 1

            # Shutdown last: it ends the fixture process.
            reply = await control.shutdown()
            assert reply is not None
            assert reply.ok is True
        finally:
            await control.aclose()
    finally:
        await app.stop()
