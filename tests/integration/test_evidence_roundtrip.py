"""Every kind of SDK call the sample app can make comes back joinable and labelled.

The real sample app runs in-process behind ``httpx.ASGITransport``, with the
real facade, the real kit transport and the SDK's HTTP layer answered by
the test stub (no socket is ever opened). A small scenario router in front
of it makes the calls a user's code would make; everything else (the
action, the inbox, the control routes) is the sample app itself. The test
then reads the records back the way the console does, through its own
``ControlClient``, and checks with the test-only join helpers that:

- every scripted call is captured once and joined to exactly its attempts;
- each is classified the way it happened: a verdict, each kind of fail-open,
  the method guard, a canary answer, a timeout then a 422 (a probable
  duplicate), a plain 422 (not one), a log after the client closed, and a
  client a route built for itself;
- a visitor driven with the console's own driver code leaves a ledger in
  which the confirm joins its action by ref and run, the inbox delivery
  sits between them, and the success-side log carries the same ref and run;
- every record and every control response has the shape the contract
  samples describe.
"""

from __future__ import annotations

import asyncio
import dataclasses
import json
import time
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from pydantic import BaseModel
from signalgate import Client, SignalGateError

import app.main
from app import signalgate_client
from app.kit import settings, tokens
from app.kit.context import ContextMiddleware, get_ref
from app.signalgate_client import sg, sg_event
from playground.supervisor.control_client import ControlClient, queued
from playground.traffic.driver import _run_one, _Tally
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_STOP_GRACE_S
from playground.traffic.plan import Visit
from tests.contract.samples import (
    CALLS,
    CANARY,
    HEALTH,
    LEDGER,
    METRICS,
    assert_same_shape,
    record_spec,
)
from tests.support.evidence_joins import is_local, join_checks, join_logs, probable_duplicate
from tests.support.sdk_stub import (
    Reply,
    check_verdict,
    error_reply,
    forbid_sockets,
    install_sdk_stub,
    log_accepted,
    request_json,
    sequence,
)

CONTROL_TOKEN = "integration-control-token"
DUMMY_KEY = "integration-dummy-value"
CIPHERTEXT = "integration-dummy-ciphertext"
RUN_ID = "run-integration-1"
BASE_URL = "http://127.0.0.1"  # never dialled: every request goes in-process

# How long the scripted logs may take to leave the SDK's queue.
DRAIN_WITHIN_S = APP_STOP_GRACE_S

# Nonces the stub answers by, one per scenario.
CHECK_ANSWERS: dict[str, Any] = {
    "verdict": check_verdict("block"),
    "open-read-timeout": Reply(raise_="read_timeout"),
    "open-connect-timeout": Reply(raise_="connect_timeout"),
    "open-network": Reply(raise_="connect"),
    "open-5xx": error_reply(503),
    "open-malformed": Reply(status=200, body="not json"),
}
LOG_ANSWERS: dict[str, Any] = {
    "log-duplicate": sequence(Reply(raise_="read_timeout"), error_reply(422)),
    "log-rejected": error_reply(422),
}
GUARD_NONCE = "guarded"
CANARY_NONCE = "canary-block"
AFTER_CLOSE_NONCE = "after-close"


class Scripted(BaseModel):
    user: str
    nonce: str
    method: str = "playground.action"


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


def _envelope(nonce: str) -> dict[str, Any]:
    return {"encrypted": CIPHERTEXT, "timestamp": int(time.time()), "nonce": nonce}


scenario = APIRouter()


@scenario.post("/scenario/check")
def scripted_check(request: Request, body: Scripted) -> dict[str, Any]:
    ev = sg_event(request, body.user, _envelope(body.nonce), body.method)
    assert ev is not None
    try:
        result = sg().check(ev)
    except SignalGateError as error:
        return {"raised": type(error).__name__, "status": getattr(error, "status_code", None)}
    return {"action": result.action, "failed_open": result.failed_open}


@scenario.post("/scenario/log")
def scripted_log(request: Request, body: Scripted) -> dict[str, bool]:
    ev = sg_event(request, body.user, _envelope(body.nonce), body.method)
    assert ev is not None
    sg().log(ev)
    return {"ok": True}


@scenario.post("/scenario/close")
def scripted_close() -> dict[str, bool]:
    sg().close()
    return {"ok": True}


@scenario.post("/scenario/own-client")
def own_client() -> dict[str, bool]:
    client = Client(DUMMY_KEY)
    client.close()
    return {"ok": True}


@scenario.post("/api/confirm")
def confirm_and_log(request: Request, body: ConfirmBody) -> dict[str, bool]:
    # The sample app's own confirm, plus the success-side log a user adds.
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, _envelope(f"success-{get_ref()}"), "playground.success")
    if ev:
        sg().log(ev)
    return {"ok": True}


def _scenario_app() -> FastAPI:
    wrapper = FastAPI()
    wrapper.add_middleware(ContextMiddleware)
    wrapper.include_router(scenario)
    wrapper.mount("/", app.main.app)
    return wrapper


def _respond(endpoint: str, headers: dict[str, str], body: bytes) -> Reply:
    nonce = request_json(body)["payload"]["nonce"]
    table = CHECK_ANSWERS if endpoint == "check" else LOG_ANSWERS
    answer = table.get(nonce, check_verdict() if endpoint == "check" else log_accepted())
    return answer if isinstance(answer, Reply) else answer(endpoint, headers, body)


async def _all_records(control: ControlClient) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = await control.calls(cursor=cursor, limit=7)
        assert_same_shape(dataclasses.asdict(page), CALLS)
        assert not page.truncated
        records.extend(page.records)
        cursor = page.cursor
        if not page.more:
            return records


async def _all_ledger(control: ControlClient) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []
    cursor = 0
    while True:
        page = await control.ledger(cursor=cursor, limit=7)
        assert_same_shape(dataclasses.asdict(page), LEDGER)
        entries.extend(page.entries)
        cursor = page.cursor
        if not page.more:
            return entries


async def _wait_drained(control: ControlClient) -> dict[str, int]:
    deadline = time.monotonic() + DRAIN_WITHIN_S
    while True:
        snapshot = await control.metrics()
        if queued(snapshot) == 0 or time.monotonic() > deadline:
            return snapshot
        await asyncio.sleep(APP_HEALTH_POLL_S)


async def _drive(stub_log) -> dict[str, Any]:
    transport = httpx.ASGITransport(app=_scenario_app())
    control = ControlClient(BASE_URL, CONTROL_TOKEN, transport=transport)
    results: dict[str, Any] = {}
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as visitor:

        async def post(path: str, nonce: str, method: str = "playground.action") -> Any:
            response = await visitor.post(
                path,
                json={"user": "user-integration", "nonce": nonce, "method": method},
                headers={"X-Playground-Ref": f"ref-{nonce}", "X-Playground-Run": RUN_ID},
            )
            assert response.status_code == 200, response.text
            return response.json()

        health = await control.health()
        assert health.clients_created == 0

        assert await control.canary([{"nonce": CANARY_NONCE, "answer": "block"}]) == 1
        assert_same_shape({"registered": 1}, CANARY)

        for nonce in CHECK_ANSWERS:
            results[nonce] = await post("/scenario/check", nonce)
        results[GUARD_NONCE] = await post("/scenario/check", GUARD_NONCE, "login.attempt")
        results[CANARY_NONCE] = await post("/scenario/check", CANARY_NONCE)
        for nonce in LOG_ANSWERS:
            await post("/scenario/log", nonce)

        # One visitor, driven by the console's own driver code with its headers.
        visit = Visit(
            ref="ref-visitor",
            cls="real",
            user_id="user-visitor",
            client_ip="198.51.100.20",
            success=True,
            success_after_ms=0,
        )
        tally = _Tally(1)
        await _run_one(
            visit,
            client=visitor,
            control_token=CONTROL_TOKEN,
            semaphore=asyncio.Semaphore(1),
            stop=asyncio.Event(),
            tally=tally,
            on_progress=lambda _progress: None,
            run_id=RUN_ID,
        )
        assert tally.successes == 1

        results["own-client"] = (await visitor.post("/scenario/own-client")).status_code
        results["metrics"] = await _wait_drained(control)
        await visitor.post("/scenario/close")
        await post("/scenario/log", AFTER_CLOSE_NONCE)

        raw_health = await visitor.get(
            "/__playground/health", headers={"Authorization": f"Bearer {CONTROL_TOKEN}"}
        )
        assert_same_shape(raw_health.json(), HEALTH)
        results["records"] = await _all_records(control)
        results["ledger"] = await _all_ledger(control)
    await control.aclose()
    return results


def test_every_scripted_call_comes_back_joined_and_classified(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", CONTROL_TOKEN)
    settings.ENV_PATH.write_text(f"SIGNALGATE_API_KEY={DUMMY_KEY}\n")
    signalgate_client._reset()
    stub_log = install_sdk_stub(monkeypatch, _respond)
    guard = forbid_sockets(monkeypatch)

    results = asyncio.run(_drive(stub_log))
    records = results["records"]
    for record in records:
        assert_same_shape(record, record_spec(record))
    assert_same_shape(results["metrics"], METRICS)
    assert guard.attempts == []

    calls = [r for r in records if r["kind"] == "call"]
    attempts = [r for r in records if r["kind"] == "attempt"]
    by_nonce = {call["nonce"]: call for call in calls if call["nonce"] is not None}

    # Every attempt belongs to exactly one call.
    checks = join_checks(records)
    logs = join_logs(records)
    joined = [a for bucket in (*checks.values(), *logs.values()) for a in bucket]
    assert len(joined) == len(attempts)
    assert {id(a) for a in joined} == {id(a) for a in attempts}

    def check_attempts(nonce: str) -> list[dict[str, Any]]:
        return checks[by_nonce[nonce]["seq"]]

    def log_attempts(nonce: str) -> list[dict[str, Any]]:
        return logs[by_nonce[nonce]["log_job"]]

    # A real verdict.
    assert results["verdict"] == {"action": "block", "failed_open": False}
    assert by_nonce["verdict"]["outcome"] == "verdict"
    (verdict,) = check_attempts("verdict")
    assert (verdict["status"], verdict["verdict"], verdict["local"]) == (200, "block", None)

    # Each kind of fail-open, told apart by its attempt.
    for nonce in ("open-read-timeout", "open-connect-timeout", "open-network", "open-5xx"):
        assert results[nonce] == {"action": "allow", "failed_open": True}, nonce
        assert by_nonce[nonce]["outcome"] == "failed_open", nonce
    (read_timeout,) = check_attempts("open-read-timeout")
    assert read_timeout["timed_out"] is True
    assert read_timeout["timeout_phase"] == "read"
    assert read_timeout["status"] is None
    (connect_timeout,) = check_attempts("open-connect-timeout")
    assert connect_timeout["timeout_phase"] == "connect"
    (network,) = check_attempts("open-network")
    assert network["timed_out"] is False
    assert network["status"] is None
    assert network["error_class"] == "NetworkError"
    (server_error,) = check_attempts("open-5xx")
    assert server_error["status"] == 503
    assert results["open-malformed"]["failed_open"] is True
    (malformed,) = check_attempts("open-malformed")
    assert malformed["status"] == 200
    assert malformed["verdict"] is None

    # The method guard answers locally and raises into the user's code.
    assert results[GUARD_NONCE] == {"raised": "ServerError", "status": 400}
    assert by_nonce[GUARD_NONCE]["outcome"] == "raised"
    assert by_nonce[GUARD_NONCE]["error"]["code"] == "PLAYGROUND_METHOD_ONLY"
    (guarded,) = check_attempts(GUARD_NONCE)
    assert guarded["local"] == "guard"
    assert is_local(guarded)

    # A canary is answered on the laptop.
    assert results[CANARY_NONCE] == {"action": "block", "failed_open": False}
    (canary,) = check_attempts(CANARY_NONCE)
    assert canary["local"] == "canary"
    assert is_local(canary)
    assert not is_local(verdict)

    # A timeout then a 422 on one log is a probable duplicate; a plain 422 isn't.
    duplicate = log_attempts("log-duplicate")
    assert [a["attempt"] for a in duplicate] == [1, 2]
    assert duplicate[0]["timed_out"] is True
    assert duplicate[1]["status"] == 422
    assert len({a["idempotency_key"] for a in duplicate}) == 1
    assert probable_duplicate(duplicate)
    rejected = log_attempts("log-rejected")
    assert [a["status"] for a in rejected] == [422]
    assert not probable_duplicate(rejected)

    # A log after the client closed is recorded, and never enqueued.
    after_close = by_nonce[AFTER_CLOSE_NONCE]
    assert (after_close["op"], after_close["outcome"], after_close["log_job"]) == (
        "log",
        "not_enqueued",
        None,
    )
    assert any(call["op"] == "close" for call in calls)

    # The shared client and the one a route built for itself.
    constructs = [r for r in records if r["kind"] == "construct"]
    assert [(c["singleton"], c["kit_transport"]) for c in constructs] == [
        (True, True),
        (False, False),
    ]
    assert results["own-client"] == 200

    # The stub never saw the guarded or the canary call.
    sent_nonces = {request_json(body)["payload"]["nonce"] for _, _, body in stub_log.requests}
    assert GUARD_NONCE not in sent_nonces
    assert CANARY_NONCE not in sent_nonces
    assert AFTER_CLOSE_NONCE not in sent_nonces

    # The visitor: action, inbox delivery and confirm join by ref and run,
    # and the success-side log carries the same ref and run.
    ledger = [e for e in results["ledger"] if e["ref"] == "ref-visitor"]
    assert [e["kind"] for e in ledger] == ["action", "inbox", "confirm"]
    assert [e["seq"] for e in ledger] == sorted(e["seq"] for e in ledger)
    assert {e["run"] for e in ledger} == {RUN_ID}
    assert ledger[2]["ok"] is True
    (success,) = [c for c in calls if c["method"] == "playground.success"]
    assert (success["ref"], success["run"]) == ("ref-visitor", RUN_ID)
    assert success["outcome"] == "enqueued"
    assert ledger[2]["seq"] < success["seq"]

    # No envelope contents and no key in any record.
    dumped = json.dumps(records)
    assert CIPHERTEXT not in dumped
    assert DUMMY_KEY not in dumped
