"""The real sample app's check() and log() calls reach the fake server through forward mode.

The console side opens a session on the fake and leases one round of
protected visits. The real sample app runs in process behind
``httpx.ASGITransport``, with the real facade and kit transport; in front
of it sit the two handlers a user writes by the end of the tutorial (check
before the action, log after it, log the success). The SDK's HTTP layer is
the test stub in forward mode, pointed at the fake, so no socket is opened
and no base URL is involved.

With the workflow active and protecting, the fake blocks the attack's
checks: those actions never happen and are never logged. Every real
visitor's action is performed and logged, every success is logged, and the
fake's status counts exactly those logs.
"""

from __future__ import annotations

import asyncio
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from fastapi import APIRouter, FastAPI, HTTPException, Request
from fastapi.testclient import TestClient
from pydantic import BaseModel
from signalgate import SignalGateError

import app.main
from app import signalgate_client
from app.kit import settings, tokens
from app.kit.actions import perform_action
from app.kit.context import ContextMiddleware
from app.signalgate_client import sg, sg_event
from playground.supervisor.control_client import queued
from playground.traffic.local_constants import APP_HEALTH_POLL_S, APP_STOP_GRACE_S
from tests.contract.playground_api import (
    ACTION_METHOD,
    PROTOCOL_HEADER,
    PROTOCOL_VERSION,
    SESSIONS_PATH,
    SUCCESS_METHOD,
)
from tests.fake_server import FakeServer, Script
from tests.support.sdk_stub import forbid_sockets, forward_to, install_sdk_stub

CONTROL_TOKEN = "forward-control-token"
DUMMY_KEY = "forward-dummy-value"
BASE_URL = "http://127.0.0.1"  # never dialled: every request goes in-process
VISITS = 20


class ActionBody(BaseModel):
    user: str
    signalgate: Any = None
    signalgate_log: Any = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


handlers = APIRouter()


@handlers.post("/api/action")
def action(request: Request, body: ActionBody) -> dict[str, bool]:
    ev = sg_event(request, body.user, body.signalgate, ACTION_METHOD)
    if ev:
        try:
            if sg().check(ev).action == "block":
                raise HTTPException(status_code=403, detail="Request could not be completed")
        except SignalGateError:
            pass
    perform_action(body.user)
    ev = sg_event(request, body.user, body.signalgate_log, ACTION_METHOD)
    if ev:
        sg().log(ev)
    return {"ok": True}


@handlers.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if not tokens.verify(body.user, body.token):
        raise HTTPException(status_code=400, detail="invalid_token")
    ev = sg_event(request, body.user, body.signalgate, SUCCESS_METHOD)
    if ev:
        sg().log(ev)
    return {"ok": True}


def _tutorial_app() -> FastAPI:
    wrapper = FastAPI()
    wrapper.add_middleware(ContextMiddleware)
    wrapper.include_router(handlers)
    wrapper.mount("/", app.main.app)
    return wrapper


def _lease_one_round(fake: FakeServer) -> tuple[TestClient, str, list[dict[str, Any]]]:
    api = TestClient(
        fake.app,
        headers={PROTOCOL_HEADER: PROTOCOL_VERSION, "Authorization": f"Bearer {DUMMY_KEY}"},
    )
    session = api.post(
        SESSIONS_PATH,
        json={"size": VISITS, "client_version": "0.0.1", "consent_version": "consent-test-1"},
    )
    assert session.status_code == 201, session.text
    sid = session.json()["session_id"]
    lease = api.post(
        f"{SESSIONS_PATH}/{sid}/leases",
        json={"phase": "protect", "round": 1, "count": VISITS, "seq": 1},
    )
    assert lease.status_code == 200, lease.text
    return api, sid, lease.json()["visits"]


async def _drive(visits: list[dict[str, Any]]) -> dict[str, int]:
    """Send every visit through the app; return each ref's action status."""
    statuses: dict[str, int] = {}
    transport = httpx.ASGITransport(app=_tutorial_app())
    async with httpx.AsyncClient(transport=transport, base_url=BASE_URL) as client:
        for visit in visits:
            headers = {"X-Playground-Ref": visit["ref"], "X-Forwarded-For": visit["client_ip"]}
            envelopes = visit["envelopes"]
            response = await client.post(
                "/api/action",
                headers=headers,
                json={
                    "user": visit["user_id"],
                    "signalgate": envelopes["action_check"],
                    "signalgate_log": envelopes["action_log"],
                },
            )
            statuses[visit["ref"]] = response.status_code
            if response.status_code != 200 or not visit["script"]["success"]:
                continue
            inbox = await client.get(
                "/__playground/inbox",
                params={"ref": visit["ref"]},
                headers={"Authorization": f"Bearer {CONTROL_TOKEN}"},
            )
            token = inbox.json()["tokens"][visit["ref"]]
            confirmed = await client.post(
                "/api/confirm",
                headers=headers,
                json={
                    "user": visit["user_id"],
                    "token": token,
                    "signalgate": envelopes["success_log"],
                },
            )
            assert confirmed.status_code == 200, confirmed.text
    return statuses


def _wait_drained() -> dict[str, int]:
    deadline = time.monotonic() + APP_STOP_GRACE_S
    while True:
        snapshot = signalgate_client.metrics_snapshot()
        if queued(snapshot) == 0 or time.monotonic() > deadline:
            return snapshot
        time.sleep(APP_HEALTH_POLL_S)


def test_the_sample_apps_checks_and_logs_reach_the_fake(monkeypatch):
    monkeypatch.setenv("PLAYGROUND_CONTROL_TOKEN", CONTROL_TOKEN)
    settings.ENV_PATH.write_text(f"SIGNALGATE_API_KEY={DUMMY_KEY}\n")
    signalgate_client._reset()

    script = Script()
    script.go_to("active", protection_stage=1)
    fake = FakeServer(script)
    started = datetime.now(timezone.utc) - timedelta(seconds=1)
    api, sid, visits = _lease_one_round(fake)
    real = [v for v in visits if v["class"] == "real"]
    attack = [v for v in visits if v["class"] == "attack"]
    successes = [v for v in real if v["script"]["success"]]
    assert real and attack and successes

    install_sdk_stub(monkeypatch, forward_to(fake.app))
    guard = forbid_sockets(monkeypatch)
    statuses = asyncio.run(_drive(visits))
    metrics = _wait_drained()

    assert guard.attempts == []
    assert metrics["log_sent_total"] == len(real) + len(successes)
    assert {statuses[v["ref"]] for v in attack} == {403}
    assert {statuses[v["ref"]] for v in real} == {200}

    checks = fake.delivered("check")
    assert sorted(d.ref for d in checks) == sorted(v["ref"] for v in visits)
    assert {(d.cls, d.verdict) for d in checks} == {("attack", "block"), ("real", "allow")}
    assert all(d.status == 200 and d.role == "action_check" for d in checks)

    logs = fake.delivered("log")
    assert all(d.status == 200 for d in logs)
    action_logs = [d for d in logs if d.method == ACTION_METHOD]
    success_logs = [d for d in logs if d.method == SUCCESS_METHOD]
    assert sorted(d.ref for d in action_logs) == sorted(v["ref"] for v in real)
    assert {d.role for d in action_logs} == {"action_log"}
    assert sorted(d.ref for d in success_logs) == sorted(v["ref"] for v in successes)
    assert {d.role for d in success_logs} == {"success_log"}

    status = api.get(f"{SESSIONS_PATH}/{sid}", params={"since": started.isoformat()}).json()
    assert status["window"] == {
        "action_received": len(real),
        "success_received": len(successes),
    }
    assert status["stored_since"] == {"action": len(real), "success": len(successes)}
