"""Chapter 5 in another style: a local client name, the verdict kept, the policy spelled out."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel
from signalgate import CheckResult, SignalGateError

from app.kit import tokens
from app.kit.actions import perform_action
from app.signalgate_client import sg, sg_event

ACTION = "playground.action"
SUCCESS = "playground.success"

router = APIRouter()


class ActionBody(BaseModel):
    user: str
    signalgate: Any = None
    signalgate_log: Any = None


class ConfirmBody(BaseModel):
    user: str
    token: str
    signalgate: Any = None


@router.post("/api/action")
def action(request: Request, body: ActionBody) -> dict[str, bool]:
    client = sg()
    verdict: CheckResult | None = None
    event = sg_event(request, body.user, body.signalgate, ACTION)
    if event is not None:
        try:
            verdict = client.check(event)
        except SignalGateError:
            verdict = None  # our policy: let the request through
    if verdict is not None and verdict.action == "block":
        raise HTTPException(status_code=403, detail="Request could not be completed")
    perform_action(body.user)
    event = sg_event(request, body.user, body.signalgate_log, ACTION)
    if event is not None:
        client.log(event)
    return {"ok": True}


@router.post("/api/confirm")
def confirm(request: Request, body: ConfirmBody) -> dict[str, bool]:
    if tokens.verify(body.user, body.token):
        event = sg_event(request, body.user, body.signalgate, SUCCESS)
        if event is not None:
            sg().log(event)
        return {"ok": True}
    raise HTTPException(status_code=400, detail="invalid_token")
