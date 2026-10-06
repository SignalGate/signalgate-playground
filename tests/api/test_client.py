"""The playground API client against the test-only fake, and its whitelisting decoders.

The client reaches the fake in process through an httpx transport: the
same seam every test uses, and the only way to point the client anywhere
but its fixed address.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone

import httpx
import pytest

from playground.api import (
    BASE_URL,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    ApiError,
    ApiUnreachable,
    PlaygroundApi,
    ProtocolOutdated,
    decode,
)
from playground.api.client import CONSENT_VERSION
from tests.fake_server import FakeServer, Script, name_only, playground_flow
from tests.fake_server.script import PREDICTION

KEY = "client-test-value"
CONSENT = "consent-test-1"


def _api(fake: FakeServer, key: str = KEY) -> PlaygroundApi:
    return PlaygroundApi(key, transport=httpx.ASGITransport(app=fake.app))


def _run(coro):
    return asyncio.run(coro)


def test_the_address_is_fixed_and_the_key_never_shows():
    api = PlaygroundApi(KEY)
    try:
        assert BASE_URL == "https://api.signalgate.ai"
        assert KEY not in repr(api)
    finally:
        _run(api.aclose())


def test_every_request_carries_the_protocol_header_and_the_key():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(204)

    async def scenario() -> None:
        async with PlaygroundApi(KEY, transport=httpx.MockTransport(handler)) as api:
            await api.telemetry("sid", 3, ["LOG_MISSING"])
            await api.end_session("sid")

    _run(scenario())
    assert [r.url.host for r in seen] == ["api.signalgate.ai"] * 2
    assert all(r.headers[PROTOCOL_HEADER] == PROTOCOL_VERSION for r in seen)
    assert all(r.headers["Authorization"] == f"Bearer {KEY}" for r in seen)
    assert json.loads(seen[0].content) == {"chapter": 3, "finding_ids": ["LOG_MISSING"]}


def test_a_session_is_opened_with_the_consent_version_the_console_shows():
    fake = FakeServer()
    upstream = httpx.ASGITransport(app=fake.app)
    sent: list[dict] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return await upstream.handle_async_request(request)

    async def scenario():
        transport = httpx.MockTransport(handler)
        async with PlaygroundApi(KEY, transport=transport, client_version="9.9.9") as api:
            return await api.create_session(1100, CONSENT_VERSION)

    assert _run(scenario())["session_id"]
    assert sent == [{"size": 1100, "client_version": "9.9.9", "consent_version": CONSENT_VERSION}]


def test_a_session_decodes_its_limits_and_the_server_clock():
    fake = FakeServer()

    async def scenario():
        async with _api(fake) as api:
            return await api.create_session(1100, CONSENT)

    session = _run(scenario())
    assert set(session) == {"session_id", "methods", "workflow_name", "limits", "server_time"}
    assert session["methods"] == {"action": "playground.action", "target": "playground.success"}
    assert session["limits"] == {k: float(v) for k, v in fake.script.limits.items()}
    assert session["server_time"].tzinfo is not None


@pytest.mark.parametrize(
    ("status", "code", "details", "retry_after"),
    [
        (401, "REQUEST_REJECTED", None, None),
        (
            409,
            "SESSION_ACTIVE",
            {"started_at": "2026-01-02T03:04:05+00:00", "key_prefix": "k1"},
            None,
        ),
        (412, "NO_ACTIVE_PUBLIC_KEY", None, None),
        (429, "REQUEST_REJECTED", None, 7),
    ],
)
def test_session_errors_carry_their_code_and_details(status, code, details, retry_after):
    fake = FakeServer()
    fake.script.fail_next(
        "create_session", status, code, details=details, retry_after_s=retry_after
    )

    async def scenario():
        async with _api(fake) as api:
            await api.create_session(1100, CONSENT)

    with pytest.raises(ApiError) as caught:
        _run(scenario())
    assert (caught.value.status, caught.value.code) == (status, code)
    assert caught.value.details == (details or {})
    assert caught.value.retry_after_s == retry_after


def test_a_426_means_the_checkout_is_out_of_date():
    fake = FakeServer()
    fake.script.fail_next("create_session", 426, "REQUEST_REJECTED")

    async def scenario():
        async with _api(fake) as api:
            await api.create_session(1100, CONSENT)

    with pytest.raises(ProtocolOutdated):
        _run(scenario())


def test_a_5xx_or_a_network_error_is_unreachable():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async def scenario(transport):
        async with PlaygroundApi(KEY, transport=transport) as api:
            await api.status("sid")

    with pytest.raises(ApiUnreachable):
        _run(scenario(httpx.MockTransport(boom)))
    with pytest.raises(ApiUnreachable):
        _run(scenario(httpx.MockTransport(lambda request: httpx.Response(503))))


def test_leases_decode_visits_with_their_envelopes_and_seq():
    fake = FakeServer()

    async def scenario():
        async with _api(fake) as api:
            session = await api.create_session(1100, CONSENT)
            return await api.lease(session["session_id"], phase="protect", count=20, seq=4, round=1)

    lease = _run(scenario())
    assert lease["seq"] == 4
    assert len(lease["visits"]) == 20
    for visit in lease["visits"]:
        assert set(visit) == {"ref", "class", "user_id", "client_ip", "script", "envelopes"}
        assert "action_check" in visit["envelopes"] and "action_log" in visit["envelopes"]
        for envelope in visit["envelopes"].values():
            assert set(envelope) == {"encrypted", "timestamp", "nonce", "v"}


def test_probe_leases_name_their_roles():
    fake = FakeServer()

    async def scenario():
        async with _api(fake) as api:
            session = await api.create_session(1100, CONSENT)
            return await api.lease(
                session["session_id"],
                phase="probe",
                count=3,
                seq=1,
                probe_roles=["action_log", "success_log"],
            )

    visits = _run(scenario())["visits"]
    assert {tuple(sorted(v["envelopes"])) for v in visits} == {("action_log", "success_log")}


def test_status_decodes_the_workflow_and_the_stored_counts():
    script = Script([playground_flow("ready", prediction=PREDICTION)])
    fake = FakeServer(script)

    async def scenario():
        async with _api(fake) as api:
            session = await api.create_session(1100, CONSENT)
            since = datetime.now(timezone.utc)
            return await api.status(session["session_id"], since=since)

    status = _run(scenario())
    assert status["workflow"]["state"] == "ready"
    assert status["workflow"]["prediction"] == PREDICTION
    assert status["window"] == {"action_received": 0, "success_received": 0}
    assert status["stored_since"] == {"action": 0, "success": 0}


def test_a_name_only_match_decodes_with_every_other_field_null():
    fake = FakeServer(Script([name_only("active")]))

    async def scenario():
        async with _api(fake) as api:
            session = await api.create_session(1100, CONSENT)
            return await api.status(session["session_id"])

    workflow = _run(scenario())["workflow"]
    assert workflow["methods_match"] is False
    assert workflow["prediction"] is None and workflow["protection_stage"] is None


def test_complete_sends_the_results_only_when_given():
    fake = FakeServer()

    async def scenario():
        async with _api(fake) as api:
            first = await api.create_session(1100, CONSENT)
            await api.complete(first["session_id"], keep_workflow=True)
            second = await api.create_session(1100, CONSENT)
            await api.complete(
                second["session_id"],
                keep_workflow=False,
                results={"conv_before": 0.2, "conv_after": 0.7},
            )

    _run(scenario())
    assert [c.get("results") for c in fake.completions] == [
        None,
        {"conv_before": 0.2, "conv_after": 0.7},
    ]
    assert [c["keep_workflow"] for c in fake.completions] == [True, False]


# -- the decoders ------------------------------------------------------------------


def _with_extras(value):
    """``value`` with an unknown field added to every object inside it."""
    if isinstance(value, dict):
        return {**{k: _with_extras(v) for k, v in value.items()}, "unlisted_extra": "x"}
    if isinstance(value, list):
        return [_with_extras(v) for v in value]
    return value


def _keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_keys(v) for v in value.values()))
    if isinstance(value, list):
        return set().union(*(_keys(v) for v in value))
    return set()


def test_unknown_fields_never_get_past_the_decoders():
    workflow = playground_flow("active", prediction=PREDICTION, protecting=True, protection_stage=2)
    status = {
        "workflow": workflow,
        "window": {"action_received": 1, "success_received": 2},
        "stored_since": {"action": 1, "success": 0},
    }
    envelope = {"encrypted": "e", "timestamp": 1, "nonce": "n", "v": 0}
    lease = {
        "seq": 1,
        "expires_at": "2026-01-02T03:04:05+00:00",
        "visits": [
            {
                "ref": "r",
                "class": "real",
                "user_id": "u",
                "client_ip": "192.0.2.1",
                "script": {"success": True, "success_after_ms": 10},
                "envelopes": {"action_log": envelope, "success_log": envelope},
            }
        ],
    }
    for decoder, raw in ((decode.status, status), (decode.lease, lease)):
        decoded = decoder(_with_extras(raw))
        assert "unlisted_extra" not in _keys(decoded)
        assert decoded == decoder(raw)


@pytest.mark.parametrize(
    "broken",
    [
        {"state": "somewhere"},
        {"methods_match": "yes"},
        {"protection_stage": 4},
        {"prediction": {"conv_now": "high"}},
    ],
)
def test_a_wrong_type_is_a_decode_error(broken):
    workflow = {**playground_flow("active"), **broken}
    with pytest.raises(decode.DecodeError):
        decode.workflow(workflow)


def test_error_details_keep_only_the_fields_the_console_shows():
    body = {
        "ok": False,
        "data": None,
        "error": {
            "code": "SESSION_ACTIVE",
            "message": "m",
            "request_id": "r",
            "details": {"started_at": "t", "key_prefix": "k", "unlisted_extra": "x"},
        },
    }
    assert decode.error(body) == ("SESSION_ACTIVE", {"started_at": "t", "key_prefix": "k"})
    assert decode.error("not json") == (None, {})
