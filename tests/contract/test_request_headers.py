"""The console's requests to the sample app carry exactly the headers its contract lists.

The driver and the control client run against a mock transport that
records each request. Only the headers the contract defines are compared
(the playground's own, X-Forwarded-For and Authorization); transport
headers such as Host and User-Agent are not part of the contract.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from playground.supervisor.control_client import ControlClient
from playground.traffic.driver import _run_one, _Tally
from playground.traffic.plan import Visit
from tests.contract.samples import (
    CONTROL_HEADERS,
    SAMPLE_HEALTH,
    VISIT_HEADERS,
    Obj,
    assert_same_shape,
)

TOKEN = "header-test-token"
RUN_ID = "run-headers-1"
REF = "ref-headers"
BASE_URL = "http://127.0.0.1"  # never dialled: the mock transport answers


def _contract_headers(request: httpx.Request, spec: Obj) -> dict[str, str]:
    """The request's headers that the contract speaks about, keyed as the spec spells them."""
    spelled = {key.lower(): key for key in spec.fields}
    picked: dict[str, str] = {}
    for name, value in request.headers.items():
        lowered = name.lower()
        relevant = lowered.startswith("x-playground-") or lowered in (
            "x-forwarded-for",
            "authorization",
        )
        if relevant:
            picked[spelled.get(lowered, name)] = value
    return picked


def _visit() -> Visit:
    return Visit(
        ref=REF,
        cls="real",
        user_id="user-headers",
        client_ip="198.51.100.30",
        success=True,
        success_after_ms=0,
    )


def _drive(run_id: str | None) -> list[httpx.Request]:
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/__playground/inbox":
            return httpx.Response(200, json={"tokens": {REF: "token-headers"}})
        return httpx.Response(200, json={"ok": True})

    async def go() -> None:
        async with httpx.AsyncClient(
            transport=httpx.MockTransport(answer), base_url=BASE_URL
        ) as client:
            tally = _Tally(1)
            await _run_one(
                _visit(),
                client=client,
                control_token=TOKEN,
                semaphore=asyncio.Semaphore(1),
                stop=asyncio.Event(),
                tally=tally,
                on_progress=lambda _progress: None,
                run_id=run_id,
            )
            assert tally.successes == 1

    asyncio.run(go())
    return seen


@pytest.mark.parametrize("run_id", [RUN_ID, None])
def test_every_driver_request_carries_the_contract_headers(run_id):
    action, inbox, confirm = _drive(run_id)
    assert (action.url.path, inbox.url.path, confirm.url.path) == (
        "/api/action",
        "/__playground/inbox",
        "/api/confirm",
    )
    for request in (action, confirm):
        headers = _contract_headers(request, VISIT_HEADERS)
        assert_same_shape(headers, VISIT_HEADERS)
        assert ("X-Playground-Run" in headers) is (run_id is not None)
    control = _contract_headers(inbox, CONTROL_HEADERS)
    assert_same_shape(control, CONTROL_HEADERS)
    assert control["Authorization"] == f"Bearer {TOKEN}"


def test_every_control_client_request_carries_the_contract_headers():
    seen: list[httpx.Request] = []

    def answer(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=SAMPLE_HEALTH)

    async def go() -> None:
        control = ControlClient(BASE_URL, TOKEN, transport=httpx.MockTransport(answer))
        try:
            await control.health()
        finally:
            await control.aclose()

    asyncio.run(go())
    (request,) = seen
    headers = _contract_headers(request, CONTROL_HEADERS)
    assert_same_shape(headers, CONTROL_HEADERS)
    assert "X-Forwarded-For" not in headers
