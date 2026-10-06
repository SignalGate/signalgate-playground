"""Tests for the /__playground control routes."""

from __future__ import annotations

import asyncio
import os
import uuid

import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app import signalgate_client
from app.kit import control
from tests.contract.samples import (
    FORBIDDEN,
    HEALTH,
    INBOX,
    SELF_TEST_CODES,
    UNAUTHORIZED,
    assert_same_shape,
)

# Every control route, as (method, path, a request kwarg dict that would
# otherwise be a *valid* call). The auth matrix uses these to prove the
# bearer-token and X-Forwarded-For checks run before anything route-specific.
CONTROL_ROUTES = [
    ("GET", "/__playground/health", {}),
    ("GET", "/__playground/metrics", {}),
    ("GET", "/__playground/ledger", {}),
    ("GET", "/__playground/calls", {}),
    ("POST", "/__playground/canary", {"json": {"canaries": []}}),
    ("POST", "/__playground/shutdown", {}),
    ("GET", "/__playground/inbox", {}),
]


def _call(client, method: str, path: str, headers: dict[str, str], **kwargs):
    return client.request(method, path, headers=headers, **kwargs)


@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_no_token_is_401(client, method, path, kwargs):
    response = _call(client, method, path, {}, **kwargs)
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


# Wrong `Authorization` values, as the raw bytes a client puts on the wire:
# each one is built from the configured token so the "almost right" cases
# stay almost right whatever that token is.
_WRONG_AUTHORIZATIONS = [
    pytest.param(lambda token: b"Bearer wrong", id="ascii"),
    pytest.param(lambda token: b"Bearer \xe9", id="latin1_byte"),
    pytest.param(lambda token: "Bearer t\u00ebst".encode(), id="utf8_text"),
    pytest.param(lambda token: f"Bearer {token}".encode() + b"\xe9", id="right_token_then_a_byte"),
    pytest.param(lambda token: b"Bearer " + token.encode()[:-1] + b"\xff", id="last_byte_swapped"),
    pytest.param(lambda token: b"\xff\xfe\xfd", id="undecodable"),
]


@pytest.mark.parametrize("wrong", _WRONG_AUTHORIZATIONS)
@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_wrong_token_is_401(client, control_token, method, path, kwargs, wrong):
    response = _call(client, method, path, {"Authorization": wrong(control_token)}, **kwargs)
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_a_non_ascii_bearer_with_no_token_configured_is_401(
    client, monkeypatch, method, path, kwargs
):
    monkeypatch.delenv("PLAYGROUND_CONTROL_TOKEN", raising=False)
    response = _call(client, method, path, {"Authorization": b"Bearer \xe9"}, **kwargs)
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_forwarded_for_wins_over_a_non_ascii_bearer(client, control_token, method, path, kwargs):
    headers = {"Authorization": b"Bearer \xe9", "X-Forwarded-For": "203.0.113.9"}
    response = _call(client, method, path, headers, **kwargs)
    assert response.status_code == 403
    assert_same_shape(response.json(), FORBIDDEN)


def test_a_non_ascii_configured_token_still_lets_its_own_holder_in(client, monkeypatch):
    token = "t\u00f6ken-\u00e9"
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", token)
    response = client.get(
        "/__playground/health", headers={"Authorization": f"Bearer {token}".encode()}
    )
    assert response.status_code == 200
    assert_same_shape(response.json(), HEALTH)


def _authorize_raw(authorization: bytes) -> int:
    """Run `_authorize` on a request carrying exactly these header bytes (the
    test client re-encodes header values, a real server never does), and
    return the status it settles on: 200 when it lets the request through."""
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": [(b"authorization", authorization)],
    }
    try:
        asyncio.run(control._authorize(Request(scope)))
    except HTTPException as exc:
        return exc.status_code
    return 200


def test_a_utf8_configured_token_is_compared_as_its_utf8_bytes(monkeypatch):
    token = "t\u00f6ken"
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", token)
    assert _authorize_raw(f"Bearer {token}".encode()) == 200
    # The same text sent as Latin-1 is different bytes, so a different token.
    assert _authorize_raw(f"Bearer {token}".encode("latin-1")) == 401
    assert _authorize_raw(b"Bearer \xe9") == 401


@pytest.mark.skipif(os.name == "nt", reason="environment values are text on Windows")
def test_a_configured_token_that_is_not_utf8_is_compared_as_its_own_bytes(monkeypatch):
    # How a POSIX environment value holding a non-UTF-8 byte reads back in Python.
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", os.fsdecode(b"tok\xe9n"))
    assert _authorize_raw(b"Bearer tok\xe9n") == 200
    assert _authorize_raw(b"Bearer tok\xe8n") == 401


@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_forwarded_for_is_403(client, control_token, method, path, kwargs):
    response = _call(
        client,
        method,
        path,
        {"Authorization": f"Bearer {control_token}", "X-Forwarded-For": "203.0.113.9"},
        **kwargs,
    )
    assert response.status_code == 403
    assert_same_shape(response.json(), FORBIDDEN)


@pytest.mark.parametrize(("method", "path", "kwargs"), CONTROL_ROUTES)
def test_an_unset_token_is_401(client, monkeypatch, method, path, kwargs):
    monkeypatch.delenv("PLAYGROUND_CONTROL_TOKEN", raising=False)
    response = _call(client, method, path, {"Authorization": "Bearer anything"}, **kwargs)
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


# A canary body valid enough to reach 200, for the pre-use check below.
_VALID_CANARY_CALL = {"json": {"canaries": [{"nonce": "n", "answer": "block"}]}}


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("GET", "/__playground/health", {}),
        ("GET", "/__playground/metrics", {}),
        ("GET", "/__playground/ledger", {}),
        ("GET", "/__playground/calls", {}),
        ("POST", "/__playground/canary", _VALID_CANARY_CALL),
        ("POST", "/__playground/shutdown", {}),
        ("GET", "/__playground/inbox", {}),
    ],
)
def test_no_route_ever_constructs_a_client(
    client, control_token, monkeypatch, method, path, kwargs
):
    """A spy on Client.__init__ sees 0 calls across every control route."""
    import signalgate

    calls_seen = []
    monkeypatch.setattr(
        signalgate.Client, "__init__", lambda self, *a, **k: calls_seen.append((a, k))
    )
    response = _call(client, method, path, {"Authorization": f"Bearer {control_token}"}, **kwargs)
    assert response.status_code == 200
    assert calls_seen == []


def test_health_requires_a_bearer_token(client):
    response = client.get("/__playground/health")
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


def test_health_rejects_the_wrong_token(client, control_token):
    response = client.get("/__playground/health", headers={"Authorization": "Bearer wrong"})
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


def test_health_shape(client, control_token):
    response = client.get(
        "/__playground/health", headers={"Authorization": f"Bearer {control_token}"}
    )
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, HEALTH)
    assert body["sdk"] == "signalgate"
    assert isinstance(body["sdk_version"], str) and body["sdk_version"]
    assert isinstance(body["boot_id"], str) and body["boot_id"]
    assert body["methods"] == ["playground.action", "playground.success"]
    assert body["clients_created"] == 0
    assert body["client_ready"] is False
    # This test's own conftest fixture starts every boot with no API key set
    # anywhere: "invalid" and "set" belong to a key that was actually read.
    assert body["key"] in ("absent", "placeholder")
    assert set(body["self_test"]["checks"]) == set(SELF_TEST_CODES)


def test_health_reports_the_client_and_key_state_live(client, control_token, monkeypatch):
    """Every `/health` read asks the client module again: nothing about the
    client or the key is fixed at boot, or at the first read."""
    headers = {"Authorization": f"Bearer {control_token}"}
    readings = [((3, True), "set"), ((4, True), "invalid")]
    for (clients_created, client_ready), key in readings:
        state = (clients_created, client_ready)
        monkeypatch.setattr(signalgate_client, "client_state", lambda state=state: state)
        monkeypatch.setattr(signalgate_client, "key_state", lambda key=key: key)
        response = client.get("/__playground/health", headers=headers)
        assert response.status_code == 200
        body = response.json()
        assert_same_shape(body, HEALTH)
        assert body["clients_created"] == clients_created
        assert body["client_ready"] is client_ready
        assert body["key"] == key


def test_health_never_builds_a_client(client, control_token, monkeypatch):
    """A spy on Client.__init__ sees no constructions from a plain /health read."""
    import signalgate

    calls_seen = []
    original = signalgate.Client.__init__

    def spy(self, *args, **kwargs):
        calls_seen.append((args, kwargs))
        return original(self, *args, **kwargs)

    monkeypatch.setattr(signalgate.Client, "__init__", spy)
    response = client.get(
        "/__playground/health", headers={"Authorization": f"Bearer {control_token}"}
    )
    assert response.status_code == 200
    assert_same_shape(response.json(), HEALTH)
    assert calls_seen == []


def test_health_rejects_forwarded_for(client, control_token):
    response = client.get(
        "/__playground/health",
        headers={
            "Authorization": f"Bearer {control_token}",
            "X-Forwarded-For": "203.0.113.9",
        },
    )
    assert response.status_code == 403
    assert_same_shape(response.json(), FORBIDDEN)


def test_forwarded_for_wins_over_a_bad_token(client, control_token):
    """A hostile combination fails on the header check, not the token check."""
    response = client.get(
        "/__playground/health",
        headers={"Authorization": "Bearer wrong", "X-Forwarded-For": "203.0.113.9"},
    )
    assert response.status_code == 403
    assert_same_shape(response.json(), FORBIDDEN)


def test_inbox_requires_a_bearer_token(client, unique_ref):
    response = client.get("/__playground/inbox", params=[("ref", unique_ref)])
    assert response.status_code == 401
    assert_same_shape(response.json(), UNAUTHORIZED)


def test_inbox_rejects_forwarded_for(client, control_token, unique_ref):
    response = client.get(
        "/__playground/inbox",
        params=[("ref", unique_ref)],
        headers={
            "Authorization": f"Bearer {control_token}",
            "X-Forwarded-For": "203.0.113.9",
        },
    )
    assert response.status_code == 403
    assert_same_shape(response.json(), FORBIDDEN)


def test_inbox_omits_refs_with_no_pending_token(client, control_token, unique_ref):
    response = client.get(
        "/__playground/inbox",
        params=[("ref", unique_ref)],
        headers={"Authorization": f"Bearer {control_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, INBOX)
    assert body == {"tokens": {}}


def test_inbox_is_read_once(client, control_token, unique_ref, unique_user):
    action = client.post(
        "/api/action",
        json={"user": unique_user},
        headers={"X-Playground-Ref": unique_ref},
    )
    assert action.status_code == 200

    headers = {"Authorization": f"Bearer {control_token}"}
    first = client.get("/__playground/inbox", params=[("ref", unique_ref)], headers=headers)
    assert first.status_code == 200
    first_body = first.json()
    assert_same_shape(first_body, INBOX)
    assert unique_ref in first_body["tokens"]

    second = client.get("/__playground/inbox", params=[("ref", unique_ref)], headers=headers)
    assert second.status_code == 200
    second_body = second.json()
    assert_same_shape(second_body, INBOX)
    assert second_body == {"tokens": {}}


def test_inbox_answers_several_refs_at_once(client, control_token, unique_user):
    ref_a, ref_b = f"ref-{uuid.uuid4().hex}", f"ref-{uuid.uuid4().hex}"
    client.post("/api/action", json={"user": unique_user}, headers={"X-Playground-Ref": ref_a})
    client.post("/api/action", json={"user": unique_user}, headers={"X-Playground-Ref": ref_b})

    response = client.get(
        "/__playground/inbox",
        params=[("ref", ref_a), ("ref", ref_b)],
        headers={"Authorization": f"Bearer {control_token}"},
    )
    assert response.status_code == 200
    body = response.json()
    assert_same_shape(body, INBOX)
    assert set(body["tokens"]) == {ref_a, ref_b}


def test_control_routes_stay_closed_without_a_configured_token(client, monkeypatch):
    monkeypatch.delenv("PLAYGROUND_CONTROL_TOKEN", raising=False)
    for authorization in ("Bearer ", "Bearer"):
        response = client.get("/__playground/health", headers={"Authorization": authorization})
        assert response.status_code == 401
        assert_same_shape(response.json(), UNAUTHORIZED)
